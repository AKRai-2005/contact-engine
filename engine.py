"""
engine.py - the contact engine: harvest public-web data into the store, then enrich.

Two operations:
  * harvest(domain)      -> crawl the org's own public site, learn its email pattern,
                            and write every published email/person into the store.
  * enrich(name, domain) -> return the best email for a person, preferring a real
                            published address in the store, falling back to a pattern guess.

Everything here uses only data an organisation publishes about itself (contact_finder)
or patterns inferred from it (email_guesser). No logins, no third-party profiles.
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlparse

import requests

from contact_finder import (
    TIMEOUT,
    USER_AGENT,
    find_contacts,
    normalize_url,
    site_root,
)
from email_guesser import (
    guess_email,
    learn_pattern,
    names_on_page,
    split_name,
    verify_email,
)
from store import Store


def _dominant_pattern(learned: dict) -> tuple[str | None, float]:
    shares = learned.get("pattern_shares", {})
    if not shares:
        return None, 0.0
    pattern, share = max(shares.items(), key=lambda kv: kv[1])
    return pattern, round(share, 3)


def harvest(store: Store, domain_or_url: str, max_pages: int = 15, check_mx: bool = True,
            render: bool = False) -> dict:
    """Crawl an organisation's public site and load what it publishes into the store."""
    host = urlparse(normalize_url(domain_or_url)).netloc
    domain = site_root(host)

    # one shared headless browser for both the crawl and the name-scraping pass
    renderer = None
    if render:
        from renderer import Renderer
        renderer = Renderer(USER_AGENT)
    try:
        contacts = find_contacts(domain_or_url, max_pages=max_pages, check_mx=check_mx, renderer=renderer)
        learned = learn_pattern(domain, max_pages=max_pages, renderer=renderer)
    finally:
        if renderer is not None:
            renderer.close()
    pattern, pattern_conf = _dominant_pattern(learned)
    store.upsert_org(domain, name=domain, pattern=pattern, pattern_confidence=pattern_conf)

    # map page URL -> names seen on it, so a published email can be tied to a person
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    page_names: dict[str, set[tuple[str, str]]] = {}

    def names_on(url: str) -> set[tuple[str, str]]:
        if url not in page_names:
            try:
                page_names[url] = names_on_page(session.get(url, timeout=TIMEOUT).text)
            except requests.RequestException:
                page_names[url] = set()
        return page_names[url]

    stored = 0
    for e in contacts["emails"]:
        email, local = e["email"], e["email"].split("@")[0]
        person_id = None

        if e["type"] == "personal":
            # try to attach the email to a name found on the same page
            for src in e["sources"]:
                match = next((fl for fl in names_on(src)
                              if _local_fits_name(local, fl[0], fl[1])), None)
                if match:
                    first, last = match
                    person_id = store.upsert_person(domain, first, last, full_name=f"{first} {last}")
                    break

        # a published address is strong evidence; on-site + verified lifts it further
        conf = 0.75
        if e["on_site_domain"]:
            conf += 0.1
        if e.get("mx_valid"):
            conf += 0.05
        store.upsert_email(
            email, domain, kind=e["type"], confidence=min(conf, 0.95),
            on_site=e["on_site_domain"], mx_valid=e.get("mx_valid"),
            person_id=person_id, sources=e["sources"],
        )
        stored += 1

    return {
        "domain": domain,
        "pattern": pattern,
        "pattern_confidence": pattern_conf,
        "emails_stored": stored,
        "pages_crawled": contacts["pages_crawled"],
    }


def _local_fits_name(local: str, first: str, last: str) -> bool:
    from email_guesser import PATTERNS, rendered_locals
    local = local.split("+")[0]
    return any(local in rendered_locals(p, first, last) for p in PATTERNS)


def enrich(store: Store, full_name: str, domain_or_url: str, smtp: bool = False,
           render: bool = False, provider=None) -> dict:
    """
    Best email for a person, in order of trust:
      1. an address harvest tied directly to this person,
      2. a guessed address that turns out to be a real published address in the store,
      3. a licensed-data provider (if configured) - the legitimate path to coverage
         the public web doesn't give us,
      4. otherwise the top pattern guess.
    Only harvested (published) emails are ever in the store, so any store hit is evidence.
    """
    host = urlparse(normalize_url(domain_or_url)).netloc
    domain = site_root(host)
    first, last = split_name(full_name)

    # pick the address via the trust tiers (guessing without SMTP; we verify once below)
    result = {"name": full_name, "domain": domain}
    hit = store.person_email(domain, first, last)
    if hit:
        result.update(email=hit["email"], confidence=hit["confidence"],
                      source="store (published, linked to person)",
                      status=hit["smtp_status"] or "unverified")
    else:
        guess = guess_email(full_name, domain, learn=True, smtp=False, render=render)
        candidates = guess["candidates"]
        result["candidates"] = candidates[:3]
        published = next((store.get_email(c["email"]) for c in candidates if store.get_email(c["email"])), None)
        provider_hit = provider.find_email(full_name, domain) if provider and provider.available() else None
        if published:
            top = next(c for c in candidates if c["email"] == published["email"])
            result.update(email=published["email"], confidence=max(published["confidence"], top["confidence"]),
                          source="store (published address)", status=published["smtp_status"] or "unverified")
        elif provider_hit:
            result.update(email=provider_hit["email"], confidence=provider_hit["confidence"],
                          source=provider_hit["source"], status="unverified")
        else:
            best = candidates[0] if candidates else None
            result.update(email=guess["best_guess"], confidence=best["confidence"] if best else 0.0,
                          source="pattern guess", basis=guess["basis"], status="unverified")

    if smtp and result.get("email"):
        result.update(_verify_and_persist(store, result["email"], result["confidence"], provider))
    return result


def verify_address(email: str, provider=None) -> str:
    """Verify one address. Prefers a provider's HTTPS verifier (no port 25 needed); falls
    back to a raw SMTP handshake. Returns a verdict / status string."""
    if provider is not None and provider.available():
        verdict = provider.verify(email)
        if verdict is not None:
            return verdict
    return verify_email(email)


def _verify_and_persist(store: Store, email: str, confidence: float, provider=None) -> dict:
    """Verify one address, persist the verdict, and reflect it in confidence."""
    status = verify_address(email, provider)
    if status in ("valid", "invalid", "accept_all", "catch_all"):
        store.set_smtp_status(email, status)
    if status == "valid":
        confidence = 0.97            # a live mailbox beats any pattern evidence
    elif status == "invalid":
        confidence = 0.02            # the server says it doesn't exist
    return {"status": status, "confidence": round(confidence, 3)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Contact engine: harvest public data, then enrich.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("harvest", help="crawl an org's public site into the store")
    h.add_argument("domain")
    h.add_argument("--max-pages", type=int, default=15)
    h.add_argument("--render", action="store_true", help="render JS-heavy pages with a headless browser")
    h.add_argument("--db", default="contacts.db")

    smtp_help = "verify over SMTP (needs OUTBOUND PORT 25, blocked on most home/cloud networks)"
    e = sub.add_parser("enrich", help="find the best email for a person")
    e.add_argument("name")
    e.add_argument("domain")
    e.add_argument("--smtp", action="store_true", help=smtp_help)
    e.add_argument("--render", action="store_true", help="render JS-heavy pages with a headless browser")
    e.add_argument("--provider", choices=["hunter", "pdl"],
                   help="use a licensed-data API as a fallback (needs HUNTER_API_KEY / PDL_API_KEY)")
    e.add_argument("--db", default="contacts.db")

    v = sub.add_parser("verify", help="verify one address (HTTPS provider if set, else SMTP) and record it")
    v.add_argument("email")
    v.add_argument("--provider", choices=["hunter", "pdl"],
                   help="verify over HTTPS via this provider instead of SMTP (no port 25 needed)")
    v.add_argument("--db", default="contacts.db")

    s = sub.add_parser("stats", help="show what's in the store")
    s.add_argument("--db", default="contacts.db")

    args = parser.parse_args()
    store = Store(args.db)
    try:
        if args.cmd == "harvest":
            result = harvest(store, args.domain, args.max_pages, render=args.render)
        elif args.cmd == "enrich":
            from providers import get_provider
            provider = get_provider(args.provider) if args.provider else None
            result = enrich(store, args.name, args.domain, args.smtp, render=args.render, provider=provider)
        elif args.cmd == "verify":
            from providers import get_provider
            provider = get_provider(args.provider) if args.provider else None
            status = verify_address(args.email, provider)
            if status in ("valid", "invalid", "accept_all", "catch_all"):
                store.set_smtp_status(args.email, status)
            result = {"email": args.email, "status": status}
        else:
            result = store.stats()
    except (ValueError, PermissionError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 2
    finally:
        store.close()

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
