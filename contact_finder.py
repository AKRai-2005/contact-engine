"""
contact_finder.py - find the emails and phone numbers a website publishes about itself.

Give it a URL (company site, personal site, portfolio, etc.). It crawls a handful of
pages on that same site - prioritising contact/about/team pages - and returns every
email address and phone number it finds, with the pages they came from.

Only publicly published information is collected. robots.txt is respected, requests
are rate-limited, and sites that forbid scraping (LinkedIn, Facebook, ...) are refused.

Usage:
    python contact_finder.py https://example.com
    python contact_finder.py example.com --max-pages 20 --region IN
    python contact_finder.py example.com --no-mx --out result.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import phonenumbers
import requests
from bs4 import BeautifulSoup

try:
    import dns.resolver  # optional: used for MX validation
except ImportError:  # pragma: no cover
    dns = None

USER_AGENT = "ContactFinder/1.0 (+public contact discovery; respects robots.txt)"
TIMEOUT = 12
DELAY_SECONDS = 0.6

# Sites whose terms forbid scraping and which don't publish members' contact details.
BLOCKED_DOMAINS = (
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
    "tiktok.com", "threads.net",
)

# Links whose URL or anchor text contains these words are crawled first.
PRIORITY_WORDS = (
    "contact", "about", "team", "people", "staff", "leadership", "support",
    "impressum", "imprint", "legal", "press", "media", "careers", "office",
    "location", "help", "reach", "connect", "founder",
)

SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".zip",
    ".mp4", ".mp3", ".css", ".js", ".xml", ".json", ".woff", ".woff2", ".ttf",
)

ROLE_PREFIXES = {
    "info", "contact", "hello", "hi", "support", "help", "sales", "admin", "office",
    "team", "careers", "jobs", "hr", "press", "media", "marketing", "billing",
    "accounts", "enquiries", "inquiries", "service", "privacy", "legal", "security",
    "noreply", "no-reply", "webmaster", "feedback", "partners", "business",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")

# "name [at] domain [dot] com", "name(at)domain(dot)com", "name {at} domain.com"
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*[\[\(\{]\s*at\s*[\]\)\}]\s*([A-Za-z0-9\-]+(?:\s*(?:[\[\(\{]\s*dot\s*[\]\)\}]|\.)\s*[A-Za-z0-9\-]+)+)",
    re.IGNORECASE,
)
OBFUSCATED_DOT_RE = re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.IGNORECASE)

# Things that look like emails but aren't (asset names, tracking ids, placeholders).
JUNK_EMAIL_RE = re.compile(
    r"(\.(png|jpe?g|gif|svg|webp|css|js)$)|(@\dx\.)|(sentry|wixpress|example\.(com|org)"
    r"|domain\.com|email\.com|yourdomain|yoursite|godaddy|sentry\.io)",
    re.IGNORECASE,
)

COUNTRY_TLDS = {
    "in": "IN", "uk": "GB", "us": "US", "ca": "CA", "au": "AU", "de": "DE", "fr": "FR",
    "es": "ES", "it": "IT", "nl": "NL", "sg": "SG", "ae": "AE", "jp": "JP", "br": "BR",
    "ie": "IE", "nz": "NZ", "za": "ZA", "ch": "CH", "se": "SE", "mx": "MX",
}


# --------------------------------------------------------------------------- helpers

def normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    return urldefrag(raw)[0]


def site_root(host: str) -> str:
    """'www.shop.example.co.uk' -> 'shop.example.co.uk' (good enough for same-site checks)."""
    host = host.lower().split(":")[0]
    return host.removeprefix("www.")


def is_blocked(host: str) -> bool:
    host = site_root(host)
    return any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS)


def guess_region(host: str, fallback: str) -> str:
    tld = host.rsplit(".", 1)[-1].lower()
    return COUNTRY_TLDS.get(tld, fallback)


def decode_cfemail(encoded: str) -> str | None:
    """Decode Cloudflare's on-page email obfuscation (data-cfemail attribute)."""
    try:
        key = int(encoded[:2], 16)
        return "".join(chr(int(encoded[i:i + 2], 16) ^ key) for i in range(2, len(encoded), 2))
    except ValueError:
        return None


def clean_email(email: str) -> str | None:
    email = email.strip().strip(".,;:'\"<>()[]").lower()
    email = email.split("?")[0]
    if not EMAIL_RE.fullmatch(email) or JUNK_EMAIL_RE.search(email):
        return None
    local = email.split("@")[0]
    # long hex/uuid-like local parts are tracking ids, not mailboxes
    if len(local) > 40 or re.fullmatch(r"[0-9a-f]{16,}", local):
        return None
    return email


# --------------------------------------------------------------------------- extraction

def extract_emails(html: str, soup: BeautifulSoup) -> set[str]:
    found: set[str] = set()

    for a in soup.select('a[href^="mailto:"]'):
        for part in a["href"][7:].split(","):
            if (e := clean_email(part)):
                found.add(e)

    for tag in soup.select("[data-cfemail]"):
        if (decoded := decode_cfemail(tag["data-cfemail"])) and (e := clean_email(decoded)):
            found.add(e)

    text = soup.get_text(" ", strip=True)
    for source in (text, html):
        for m in EMAIL_RE.findall(source):
            if (e := clean_email(m)):
                found.add(e)

    for user, domain in OBFUSCATED_RE.findall(text):
        domain = OBFUSCATED_DOT_RE.sub(".", domain).replace(" ", "")
        if (e := clean_email(f"{user}@{domain}")):
            found.add(e)

    return found


def extract_phones(soup: BeautifulSoup, region: str) -> dict[str, str]:
    """Returns {E.164 number: international formatted}."""
    found: dict[str, str] = {}

    def add(raw: str, strict: bool) -> None:
        try:
            num = phonenumbers.parse(raw, region)
        except phonenumbers.NumberParseException:
            return
        if not phonenumbers.is_valid_number(num) and strict:
            return
        if not phonenumbers.is_possible_number(num):
            return
        e164 = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
        found[e164] = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL)

    # tel: links are explicit, so trust them even if the number metadata is unusual
    for a in soup.select('a[href^="tel:"], a[href^="callto:"]'):
        add(a["href"].split(":", 1)[1], strict=False)

    text = soup.get_text(" ", strip=True)
    for match in phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.VALID):
        e164 = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
        found[e164] = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.INTERNATIONAL)

    return found


def extract_structured(soup: BeautifulSoup) -> tuple[set[str], set[str]]:
    """Pull email/telephone fields from schema.org JSON-LD blocks."""
    emails, phones = set(), set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.lower() == "email" and isinstance(v, str):
                    emails.add(v.replace("mailto:", ""))
                elif k.lower() in ("telephone", "faxnumber") and isinstance(v, str):
                    phones.add(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.string or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    return emails, phones


# --------------------------------------------------------------------------- crawling

class Crawler:
    def __init__(self, start_url: str, max_pages: int, region: str, renderer=None):
        self.start_url = start_url
        self.root = site_root(urlparse(start_url).netloc)
        self.max_pages = max_pages
        self.region = region
        self.renderer = renderer          # optional headless-browser fallback for JS sites
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})
        self.robots = self._load_robots()

    def _load_robots(self) -> RobotFileParser | None:
        parsed = urlparse(self.start_url)
        rp = RobotFileParser()
        try:
            resp = self.session.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt", timeout=TIMEOUT)
            if resp.status_code >= 400:
                return None  # no robots.txt -> everything allowed
            rp.parse(resp.text.splitlines())
            return rp
        except requests.RequestException:
            return None

    def allowed(self, url: str) -> bool:
        return self.robots is None or self.robots.can_fetch(USER_AGENT, url)

    def same_site(self, url: str) -> bool:
        host = site_root(urlparse(url).netloc)
        return host == self.root or host.endswith("." + self.root) or self.root.endswith("." + host)

    def link_score(self, url: str, text: str) -> int:
        hay = (url + " " + text).lower()
        return sum(10 for w in PRIORITY_WORDS if w in hay) - url.count("/")

    def fetch(self, url: str) -> str | None:
        html = None
        try:
            resp = self.session.get(url, timeout=TIMEOUT, allow_redirects=True)
            if resp.status_code == 200 and "html" in resp.headers.get("Content-Type", ""):
                html = resp.text
        except requests.RequestException as exc:
            print(f"  ! {url}: {exc.__class__.__name__}", file=sys.stderr)

        # JS-heavy page? re-fetch the rendered DOM. render_html returns None on any failure,
        # so we always keep the static html as the fallback.
        if self.renderer is not None:
            from renderer import looks_thin
            if looks_thin(html):
                rendered = self.renderer.html(url)
                if rendered:
                    print(f"    (rendered {url})", file=sys.stderr)
                    return rendered
        return html

    def run(self) -> dict:
        queue: list[tuple[int, str]] = [(10_000, self.start_url)]
        seen: set[str] = {self.start_url}
        crawled: list[str] = []
        skipped_robots: list[str] = []
        emails: dict[str, set[str]] = defaultdict(set)
        phones: dict[str, dict] = {}

        while queue and len(crawled) < self.max_pages:
            queue.sort(key=lambda x: -x[0])
            _, url = queue.pop(0)

            if not self.allowed(url):
                skipped_robots.append(url)
                continue

            print(f"  > {url}", file=sys.stderr)
            html = self.fetch(url)
            crawled.append(url)
            if html is None:
                continue

            soup = BeautifulSoup(html, "html.parser")
            ld_emails, ld_phones = extract_structured(soup)

            for e in extract_emails(html, soup) | {x for x in map(clean_email, ld_emails) if x}:
                emails[e].add(url)

            page_phones = extract_phones(soup, self.region)
            for raw in ld_phones:
                page_phones.update(extract_phones(BeautifulSoup(f"<p>{raw}</p>", "html.parser"), self.region))
            for e164, pretty in page_phones.items():
                phones.setdefault(e164, {"number": e164, "formatted": pretty, "sources": set()})
                phones[e164]["sources"].add(url)

            for a in soup.find_all("a", href=True):
                link = urldefrag(urljoin(url, a["href"]))[0]
                if (
                    link in seen
                    or not link.startswith(("http://", "https://"))
                    or not self.same_site(link)
                    or link.lower().split("?")[0].endswith(SKIP_EXTENSIONS)
                ):
                    continue
                seen.add(link)
                queue.append((self.link_score(link, a.get_text(" ", strip=True)), link))

            time.sleep(DELAY_SECONDS)

        return {
            "pages_crawled": crawled,
            "skipped_by_robots_txt": skipped_robots,
            "emails": emails,
            "phones": phones,
        }


# --------------------------------------------------------------------------- validation

_mx_cache: dict[str, bool | None] = {}


def has_mx(domain: str) -> bool | None:
    """True/False if the domain can receive mail, None if DNS lookups aren't available."""
    if dns is None:
        return None
    if domain not in _mx_cache:
        try:
            _mx_cache[domain] = len(dns.resolver.resolve(domain, "MX", lifetime=6)) > 0
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            _mx_cache[domain] = False
        except Exception:  # noqa: BLE001 - treat any other DNS error as "unknown"
            _mx_cache[domain] = None
    return _mx_cache[domain]


# --------------------------------------------------------------------------- public API

def find_contacts(url: str, max_pages: int = 12, region: str = "US", check_mx: bool = True,
                  render: bool = False, renderer=None) -> dict:
    """`render=True` creates and owns a headless browser for this call. Alternatively pass an
    existing `renderer` (caller-owned, reused across calls) - then this won't close it."""
    start = normalize_url(url)
    host = urlparse(start).netloc
    if not host:
        raise ValueError(f"Not a valid URL: {url!r}")
    if is_blocked(host):
        raise PermissionError(
            f"{site_root(host)} prohibits automated scraping and does not publish members' "
            "emails or phone numbers. Use the person's own website, portfolio or company site instead."
        )

    region = guess_region(site_root(host), region)
    own_renderer = None
    if render and renderer is None:
        from renderer import Renderer
        renderer = own_renderer = Renderer(USER_AGENT)
    try:
        raw = Crawler(start, max_pages, region, renderer=renderer).run()
    finally:
        if own_renderer is not None:
            own_renderer.close()
    root = site_root(host)

    emails = []
    for email, sources in raw["emails"].items():
        local, domain = email.split("@", 1)
        emails.append({
            "email": email,
            "type": "role" if local.split("+")[0] in ROLE_PREFIXES else "personal",
            "on_site_domain": domain == root or domain.endswith("." + root) or root.endswith("." + domain),
            "mx_valid": has_mx(domain) if check_mx else None,
            "sources": sorted(sources),
        })
    # best first: on the site's own domain, seen on more pages, personal before role
    emails.sort(key=lambda e: (not e["on_site_domain"], -len(e["sources"]), e["type"] == "role", e["email"]))

    phones = sorted(
        ({**p, "sources": sorted(p["sources"])} for p in raw["phones"].values()),
        key=lambda p: -len(p["sources"]),
    )

    return {
        "input_url": url,
        "site": root,
        "region_used_for_phones": region,
        "pages_crawled": len(raw["pages_crawled"]),
        "emails": emails,
        "phones": phones,
        "crawled_urls": raw["pages_crawled"],
        "skipped_by_robots_txt": raw["skipped_by_robots_txt"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Find publicly listed emails and phone numbers on a website.")
    parser.add_argument("url", help="website URL, e.g. https://example.com or example.com")
    parser.add_argument("--max-pages", type=int, default=12, help="pages to crawl on the site (default 12)")
    parser.add_argument("--region", default="US",
                        help="default country for numbers without +country code (default US; auto from TLD like .in/.uk)")
    parser.add_argument("--no-mx", action="store_true", help="skip DNS MX validation of email domains")
    parser.add_argument("--render", action="store_true",
                        help="render JS-heavy pages with a headless browser (needs: pip install playwright && playwright install chromium)")
    parser.add_argument("--out", help="also write the JSON result to this file")
    args = parser.parse_args()

    try:
        result = find_contacts(args.url, args.max_pages, args.region.upper(), not args.no_mx, args.render)
    except (ValueError, PermissionError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 2

    output = json.dumps(result, indent=2)
    print(output)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
