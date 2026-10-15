"""
email_guesser.py - guess someone's work email from their name and company domain.

How it works (the same idea Hunter.io / Apollo use for their "email finder"):
  1. Generate every common address pattern for the name (john.smith@, jsmith@, john@ ...).
  2. Learn the company's real pattern: crawl the company site with contact_finder, take the
     personal-looking emails it publishes, and match them against names on the same pages
     (e.g. "jdoe@acme.com" next to "Jane Doe" -> the company uses {f}{last}).
  3. Optionally verify candidates over SMTP (RCPT TO without sending any mail), including
     catch-all detection.
  4. Rank candidates and return a best guess with a confidence score.

Usage:
    python email_guesser.py "Jane Doe" acme.com
    python email_guesser.py "Jane Doe" https://acme.com --max-pages 20
    python email_guesser.py "Jane Doe" acme.com --no-learn --smtp
"""

from __future__ import annotations

import argparse
import json
import re
import smtplib
import sys
import unicodedata
import uuid
from collections import Counter
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from contact_finder import (
    ROLE_PREFIXES,
    TIMEOUT,
    USER_AGENT,
    dns,
    find_contacts,
    has_mx,
    is_blocked,
    normalize_url,
    site_root,
)

# Rough share of companies using each pattern - a heuristic prior, used only when the
# company's own pattern can't be learned.
PATTERNS: dict[str, float] = {
    "{first}.{last}": 0.30,
    "{first}": 0.15,
    "{f}{last}": 0.15,
    "{first}{last}": 0.08,
    "{f}.{last}": 0.05,
    "{first}_{last}": 0.04,
    "{first}{l}": 0.04,
    "{last}": 0.03,
    "{last}{f}": 0.03,
    "{last}.{first}": 0.03,
    "{first}-{last}": 0.02,
    "{last}{first}": 0.02,
    "{f}{l}": 0.02,
    "{first}.{l}": 0.02,
}

SMTP_TIMEOUT = 10


# --------------------------------------------------------------------------- names

def clean_part(part: str) -> str:
    """'José' -> 'jose', "O'Brien" -> 'obrien', 'Garcia-Roberts' -> 'garcia-roberts'.
    Internal hyphens are kept because many orgs keep them in the address."""
    part = unicodedata.normalize("NFKD", part).encode("ascii", "ignore").decode().lower()
    part = re.sub(r"[^a-z-]", "", part)
    return re.sub(r"-{2,}", "-", part).strip("-")


def split_name(full_name: str) -> tuple[str, str]:
    parts = [clean_part(p) for p in full_name.split()]
    parts = [p for p in parts if p]
    if not parts:
        raise ValueError(f"Could not read a name from {full_name!r}")
    return parts[0], (parts[-1] if len(parts) > 1 else "")


def _variants(part: str) -> set[str]:
    """A hyphenated part can appear with or without the hyphen in an address."""
    return {part, part.replace("-", "")} if "-" in part else {part}


def render(pattern: str, first: str, last: str) -> str | None:
    """Canonical rendering (hyphens preserved). Use rendered_locals() to match/generate
    both the hyphenated and de-hyphenated forms."""
    if not last and ("{last}" in pattern or "{l}" in pattern):
        return None
    return pattern.format(first=first, last=last, f=first[:1], l=last[:1])


def rendered_locals(pattern: str, first: str, last: str) -> set[str]:
    """All local-parts a name could produce under a pattern, across hyphen variants."""
    out = set()
    for f in _variants(first):
        for l in _variants(last):
            r = render(pattern, f, l)
            if r:
                out.add(r)
    return out


# --------------------------------------------------------------------------- pattern learning

# Capitalised words that sit next to names on team pages but are never names themselves.
NOT_NAMES = {
    "the", "our", "and", "for", "with", "when", "about", "contact", "email", "phone", "meet",
    "team", "staff", "lead", "head", "chief", "director", "senior", "junior", "manager",
    "officer", "executive", "president", "vice", "founder", "cofounder", "partner", "associate",
    "assistant", "intern", "fellow", "attorney", "counsel", "general", "legal", "engineer",
    "engineering", "developer", "development", "designer", "design", "analyst", "researcher",
    "research", "scientist", "technologist", "activist", "activism", "advocacy", "policy",
    "program", "programs", "project", "projects", "product", "marketing", "sales", "security",
    "cybersecurity", "privacy", "communications", "coordinator", "specialist", "consultant",
    "operations", "finance", "fund", "membership", "major", "gifts", "board", "member", "advisor",
    "ceo", "cto", "cfo", "coo", "vp", "hr", "it", "pr", "mr", "mrs", "ms", "dr", "prof",
}

NAME_RUN_RE = re.compile(r"\b[A-Z][a-zA-Z'’\-]*\.?(?:\s+[A-Z][a-zA-Z'’\-]*\.?)+")


def names_on_page(html: str) -> set[tuple[str, str]]:
    """Candidate (first, last) pairs from capitalised word runs like 'Jane A. Doe'."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    pairs = set()
    for run in NAME_RUN_RE.findall(text):
        # None marks a middle initial, so 'Jane A. Doe' still pairs jane+doe;
        # title words are dropped, so 'Director Jane Doe' doesn't pair director+jane
        words = [None if len(w.rstrip(".")) == 1 else clean_part(w) for w in run.split()]
        words = [w for w in words if w not in NOT_NAMES]
        for i, first in enumerate(words):
            if not first:
                continue
            nxt = words[i + 1: i + 3]
            if nxt and nxt[0]:
                pairs.add((first, nxt[0]))
            elif len(nxt) == 2 and nxt[0] is None and nxt[1]:
                pairs.add((first, nxt[1]))
    return pairs


def infer_from_shape(local: str) -> str | None:
    """Fallback when no matching name is on the page: only separators are unambiguous."""
    for sep in (".", "_"):  # '-' is too often a team alias (extension-devs@)
        if local.count(sep) == 1:
            a, b = local.split(sep)
            if len(a) > 1 and len(b) > 1:
                return f"{{first}}{sep}{{last}}"
            if len(a) == 1 and len(b) > 1 and sep == ".":
                return "{f}.{last}"
            if len(a) > 1 and len(b) == 1 and sep == ".":
                return "{first}.{l}"
    return None


def learn_pattern(domain: str, max_pages: int, renderer=None) -> dict:
    """Crawl the company site and work out which pattern its published emails follow.
    Pass a Renderer to also run JS on thin pages when scraping names."""
    contacts = find_contacts(domain, max_pages=max_pages, check_mx=False, renderer=renderer)
    def looks_personal(email: str) -> bool:
        words = set(re.split(r"[^a-z]+", email.split("@")[0]))
        return not words & (ROLE_PREFIXES | NOT_NAMES | {"devs", "donations", "donate", "events", "news"})

    samples = [
        e for e in contacts["emails"]
        if e["on_site_domain"] and e["type"] == "personal" and looks_personal(e["email"])
    ]

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    page_names: dict[str, set[tuple[str, str]]] = {}

    def fetch_page(src: str) -> str:
        html = None
        try:
            resp = session.get(src, timeout=TIMEOUT)
            if resp.status_code == 200 and "html" in resp.headers.get("Content-Type", ""):
                html = resp.text
        except requests.RequestException:
            pass
        if renderer is not None:
            from renderer import looks_thin
            if looks_thin(html):
                rendered = renderer.html(src)
                if rendered:
                    return rendered
        return html or ""

    votes: Counter[str] = Counter()
    observations: list[tuple[str, set[str]]] = []   # (local, matched patterns)
    occupied: set[str] = set()
    evidence = []
    for sample in samples:
        local = sample["email"].split("@")[0].split("+")[0]
        occupied.add(local)
        matched: set[str] = set()
        matched_name = None

        for src in sample["sources"][:3]:
            if src not in page_names:
                page_names[src] = names_on_page(fetch_page(src))
            for first, last in page_names[src]:
                hits = {p for p in PATTERNS if local in rendered_locals(p, first, last)}
                # single-letter patterns like {f}{l} match too easily to count as evidence
                hits = {p for p in hits if len(local) > 2}
                if hits:
                    matched |= hits
                    matched_name = matched_name or f"{first} {last}"

        if not matched and (shape := infer_from_shape(local)):
            matched = {shape}

        # an ambiguous email ('mitch@' = {first} or {last}?) splits its vote by how
        # common each pattern is, so {first} wins most of it
        weight = sum(PATTERNS[p] for p in matched)
        for p in matched:
            votes[p] += PATTERNS[p] / weight
        if matched:
            observations.append((local, matched))
            evidence.append({
                "email": sample["email"],
                "matched_name": matched_name,
                "patterns": sorted(matched),
            })

    total = sum(votes.values())
    shares = {p: round(v / total, 3) for p, v in votes.most_common()} if total else {}
    primary = max(shares, key=shares.get) if shares else None

    # fallback = pattern distribution among emails that DON'T use the primary pattern,
    # i.e. how this org resolves collisions (EFF: {first}@ normally, {f}{last} when taken)
    fb_votes: Counter[str] = Counter()
    for local, matched in observations:
        if primary in matched:
            continue
        weight = sum(PATTERNS[p] for p in matched) or 1
        for p in matched:
            fb_votes[p] += PATTERNS[p] / weight
    fb_total = sum(fb_votes.values())

    return {
        "emails_examined": len(samples),
        "pattern_shares": shares,
        "fallback_shares": {p: round(v / fb_total, 3) for p, v in fb_votes.most_common()} if fb_total else {},
        "occupied": sorted(occupied),
        "evidence_sample": evidence[:10],
    }


# --------------------------------------------------------------------------- SMTP verification

def mx_hosts(domain: str) -> list[str]:
    if dns is None:
        return []
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=6)
    except Exception:  # noqa: BLE001 - any DNS failure just means "no usable MX"
        return []
    return [str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda r: r.preference)]


def classify_rcpt(code: int, catch_all: bool) -> str:
    """Turn an SMTP RCPT TO reply code into a verdict (pure, so it's unit-testable)."""
    if catch_all and code in (250, 251):
        return "accept_all"      # server takes everything -> can't confirm this mailbox
    if code in (250, 251):
        return "valid"
    if code in (550, 551, 553):
        return "invalid"         # no such mailbox
    return f"unknown ({code})"   # greylisted / rate-limited / policy block


def smtp_verify(domain: str, addresses: list[str]) -> dict:
    """
    Ask the domain's mail server whether each address exists (RCPT TO), then QUIT.
    No email is ever sent. Caveats, all reported in "status":
      * Most home/cloud networks BLOCK outbound port 25 -> "port_25_blocked_or_timeout".
        Run this only from a host that allows port 25 (a suitable VPS).
      * Catch-all servers accept every address -> "catch_all"; individual results read
        "accept_all" and cannot be trusted as confirmation.
      * Big providers (Google/Microsoft) often greylist or rate-limit -> "unknown (code)".
    Keep volume low: aggressive probing gets an IP blocklisted.
    """
    if not addresses:
        return {"status": "no_addresses", "results": {}}
    hosts = mx_hosts(domain)
    if not hosts:
        return {"status": "no_mx", "results": {}}

    last_error = "no MX host answered"
    for host in hosts[:2]:
        try:
            with smtplib.SMTP(host, 25, timeout=SMTP_TIMEOUT) as smtp:
                smtp.ehlo_or_helo_if_needed()
                code, _ = smtp.mail("")  # null sender, as bounce-checkers do
                if code >= 400:
                    last_error = f"MAIL FROM rejected ({code})"
                    continue

                probe = f"{uuid.uuid4().hex[:12]}-noexist@{domain}"
                catch_all = smtp.rcpt(probe)[0] in (250, 251)
                results = {addr: classify_rcpt(smtp.rcpt(addr)[0], catch_all) for addr in addresses}
                return {"status": "catch_all" if catch_all else "checked",
                        "mx_host": host, "results": results}
        except TimeoutError:
            return {"status": "port_25_blocked_or_timeout", "mx_host": host, "results": {}}
        except (OSError, smtplib.SMTPException) as exc:
            last_error = f"{exc.__class__.__name__}: {exc}"
            continue
    return {"status": "error", "detail": last_error, "results": {}}


def verify_email(email: str) -> str:
    """Verify a single address. Returns a verdict string, or the probe status if it
    couldn't be run (e.g. 'port_25_blocked_or_timeout', 'no_mx', 'catch_all')."""
    domain = email.split("@")[-1]
    report = smtp_verify(domain, [email])
    return report["results"].get(email, report["status"])


# --------------------------------------------------------------------------- scoring

def build_candidates(first: str, last: str, domain: str, shares: dict[str, float]) -> list[dict]:
    """Raw-scored, deduplicated candidate list. Shared by the guesser and the accuracy harness."""
    candidates = []
    for pattern, prior in PATTERNS.items():
        # learned evidence dominates; the prior breaks ties and covers unseen patterns
        score = 0.8 * shares.get(pattern, 0) + 0.2 * prior if shares else prior
        if shares and pattern not in shares:
            score *= 0.5
        for local in rendered_locals(pattern, first, last):
            candidates.append({"email": f"{local}@{domain}", "pattern": pattern, "score": score})

    # identical addresses from different patterns (e.g. single names) - keep the best
    best: dict[str, dict] = {}
    for c in candidates:
        if c["email"] not in best or c["score"] > best[c["email"]]["score"]:
            best[c["email"]] = c
    return list(best.values())


def normalize_candidates(candidates: list[dict]) -> list[dict]:
    """Turn raw scores into confidences that sum to ~1, best first."""
    total = sum(c["score"] for c in candidates) or 1
    for c in candidates:
        c["confidence"] = round(min(c.pop("score") / total, 0.99), 3)
    candidates.sort(key=lambda c: -c["confidence"])
    return candidates


def shares_from_pairs(pairs: list[tuple[str, str, str]]) -> dict[str, float]:
    """Supervised pattern shares from known (first, last, local) triples."""
    votes: Counter[str] = Counter()
    for first, last, local in pairs:
        matched = [p for p in PATTERNS if local in rendered_locals(p, first, last)]
        if not matched:
            continue
        weight = sum(PATTERNS[p] for p in matched)   # split toward the commoner pattern
        for p in matched:
            votes[p] += PATTERNS[p] / weight
    total = sum(votes.values())
    return {p: v / total for p, v in votes.items()} if total else {}


def distributions_from_pairs(pairs):
    """
    From known pairs, derive what the ranker needs to handle mixed schemes:
      * shares         - the org's primary pattern distribution
      * fallback_shares - the distribution among people who DON'T use the primary pattern
                          (i.e. how the org resolves first-name collisions)
      * occupied        - mailboxes already taken, so we never propose someone else's
    """
    shares = shares_from_pairs(pairs)
    primary = max(shares, key=shares.get) if shares else None
    non_primary = [(f, l, lc) for (f, l, lc) in pairs
                   if not primary or lc not in rendered_locals(primary, f, l)]
    fallback_shares = shares_from_pairs(non_primary)
    occupied = {lc for (_, _, lc) in pairs}
    return shares, fallback_shares, occupied


def rank_candidates(first: str, last: str, domain: str, shares: dict[str, float],
                    occupied=frozenset(), fallback_shares: dict | None = None) -> list[dict]:
    """
    Rank candidates, aware of mixed schemes:
      * if the person's primary-pattern address is already taken (a collision), score
        by the org's fallback distribution instead of the primary one;
      * never propose a mailbox someone else already holds.
    With no occupied set / fallback, this is identical to the plain primary-pattern ranking.
    """
    primary = max(shares, key=shares.get) if shares else None
    primary_local = render(primary, first, last) if primary else None
    dist = shares
    if fallback_shares and primary_local and primary_local in occupied:
        dist = fallback_shares

    candidates = build_candidates(first, last, domain, dist)
    # drop any mailbox already held by a different person - we never propose it
    candidates = [c for c in candidates if c["email"].split("@")[0] not in occupied]
    return normalize_candidates(candidates)


# --------------------------------------------------------------------------- public API

def guess_email(full_name: str, company: str, learn: bool = True, smtp: bool = False,
                max_pages: int = 15, render: bool = False, renderer=None) -> dict:
    host = urlparse(normalize_url(company)).netloc
    if not host:
        raise ValueError(f"Not a valid domain or URL: {company!r}")
    if is_blocked(host):
        raise PermissionError(f"{site_root(host)} is not a company email domain.")
    domain = site_root(host)
    first, last = split_name(full_name)

    own_renderer = None
    if learn and render and renderer is None:
        from renderer import Renderer
        renderer = own_renderer = Renderer(USER_AGENT)

    mx_ok = has_mx(domain)
    try:
        learned = learn_pattern(domain, max_pages, renderer=renderer) if learn else None
    finally:
        if own_renderer is not None:
            own_renderer.close()
    shares = (learned or {}).get("pattern_shares", {})
    occupied = set((learned or {}).get("occupied", []))
    fallback_shares = (learned or {}).get("fallback_shares", {})

    # don't treat the person's own (yet-unknown) mailbox as taken by someone else
    own_locals = {render(p, first, last) for p in PATTERNS if render(p, first, last)}
    occupied -= own_locals

    candidates = rank_candidates(first, last, domain, shares, occupied, fallback_shares)
    # rank_candidates already normalised; re-extract raw weight for the SMTP step below
    for c in candidates:
        c["score"] = c["confidence"]

    smtp_report = None
    if smtp and mx_ok:
        smtp_report = smtp_verify(domain, [c["email"] for c in candidates])
        for c in candidates:
            status = smtp_report["results"].get(c["email"])
            c["smtp"] = status
            if status == "valid":
                c["score"] = c["score"] + 10  # a confirmed mailbox beats any pattern guess
            elif status == "invalid":
                c["score"] = 0

    normalize_candidates(candidates)

    if mx_ok is False:
        basis = "domain has no mail server (no MX record) - these addresses cannot receive mail"
    elif smtp_report and any(v == "valid" for v in smtp_report["results"].values()):
        basis = "confirmed by the mail server (SMTP)"
    elif shares:
        basis = f"learned from {learned['emails_examined']} email(s) published on {domain}"
    else:
        basis = "common industry patterns only (no published emails to learn from)"

    return {
        "name": full_name,
        "domain": domain,
        "mx_valid": mx_ok,
        "best_guess": candidates[0]["email"] if candidates and mx_ok is not False else None,
        "basis": basis,
        "candidates": candidates,
        "learned_pattern": learned,
        "smtp": smtp_report,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Guess a work email from a person's name and company domain.")
    parser.add_argument("name", help='full name in quotes, e.g. "Jane Doe"')
    parser.add_argument("company", help="company domain or website, e.g. acme.com")
    parser.add_argument("--no-learn", action="store_true", help="don't crawl the company site to learn its pattern")
    parser.add_argument("--smtp", action="store_true", help="verify candidates with the mail server (needs outbound port 25)")
    parser.add_argument("--max-pages", type=int, default=15, help="pages to crawl when learning (default 15)")
    parser.add_argument("--top", type=int, default=5, help="how many candidates to show (default 5, 0 = all)")
    parser.add_argument("--out", help="also write the JSON result to this file")
    args = parser.parse_args()

    try:
        result = guess_email(args.name, args.company, not args.no_learn, args.smtp, args.max_pages)
    except (ValueError, PermissionError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 2

    if args.top:
        result["candidates"] = result["candidates"][: args.top]
    output = json.dumps(result, indent=2)
    print(output)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
