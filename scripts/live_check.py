#!/usr/bin/env python3
"""
live_check.py - one command that proves the paths which can't run in a dev sandbox.

Run it on a host that allows outbound port 25 (for SMTP) with Playwright installed and,
optionally, a provider API key. Each check prints [PASS] / [FAIL] / [SKIP] and the script
exits non-zero if anything actually FAILED (a SKIP is not a failure).

Configure via environment variables (all optional - unset checks just SKIP):

    VERIFY_EMAIL        a real address you control, expected to verify as 'valid'
    VERIFY_BAD_EMAIL    an address on the same domain that does NOT exist ('invalid')
    PROVIDER            'hunter' or 'pdl' (else the first provider with a key set is used)
    PROVIDER_TEST_NAME  a person to look up via the provider, e.g. "Jane Doe"
    PROVIDER_TEST_DOMAIN that person's company domain, e.g. "acme.com"
    HUNTER_API_KEY / PDL_API_KEY   the provider key(s)

    python scripts/live_check.py
"""

from __future__ import annotations

import os
import socket
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from contact_finder import extract_emails

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
results: list[tuple[str, str]] = []


def report(name: str, status: str, detail: str = "") -> None:
    color = {"PASS": GREEN, "FAIL": RED, "SKIP": YELLOW}[status]
    print(f"  {color}[{status}]{RESET} {name}" + (f" - {detail}" if detail else ""))
    results.append((name, status))


# --------------------------------------------------------------------------- 1. port 25

def check_port_25() -> None:
    host = "gmail-smtp-in.l.google.com"
    try:
        socket.create_connection((host, 25), timeout=10).close()
        report("outbound port 25", "PASS", "open - raw SMTP verification can run")
    except OSError:
        # not a failure: the HTTPS verifier (--provider) doesn't need port 25
        report("outbound port 25", "SKIP",
               "blocked here; use HTTPS verify (--provider) which needs no port 25")


# --------------------------------------------------------------------------- 2. SMTP verify

def check_smtp() -> None:
    good = os.environ.get("VERIFY_EMAIL")
    if not good:
        report("SMTP verify", "SKIP", "set VERIFY_EMAIL to a real address you control")
        return
    from email_guesser import verify_email
    status = verify_email(good)
    if status == "valid":
        report("SMTP verify (good address)", "PASS", f"{good} -> valid")
    else:
        report("SMTP verify (good address)", "FAIL", f"{good} -> {status}")

    bad = os.environ.get("VERIFY_BAD_EMAIL")
    if bad:
        bstatus = verify_email(bad)
        ok = bstatus in ("invalid", "catch_all", "accept_all")
        report("SMTP verify (bad address)", "PASS" if ok else "FAIL", f"{bad} -> {bstatus}")


# --------------------------------------------------------------------------- 3. render

RENDER_PAGE = (
    "<!doctype html><html><body><div id=root></div><script>"
    "var a=['jane','doe'].join('.')+String.fromCharCode(64)+['acme','com'].join('.');"
    "document.getElementById('root').innerHTML='<a href=\"mailto:'+a+'\">'+a+'</a>';"
    "</script></body></html>"
)


def check_render() -> None:
    from renderer import Renderer, looks_thin, playwright_available
    if not playwright_available():
        report("Playwright render", "SKIP",
               "pip install -r requirements-optional.txt && playwright install chromium")
        return
    data_url = "data:text/html," + urllib.parse.quote(RENDER_PAGE)
    static_emails = extract_emails(RENDER_PAGE, BeautifulSoup(RENDER_PAGE, "html.parser"))
    if static_emails or not looks_thin(RENDER_PAGE):
        report("Playwright render", "FAIL", "self-test page leaked its email statically")
        return
    with Renderer("LiveCheck/1.0") as r:
        if not r.available:
            report("Playwright render", "FAIL", "browser could not launch")
            return
        html = r.html(data_url)
    found = extract_emails(html, BeautifulSoup(html, "html.parser")) if html else set()
    if "jane.doe@acme.com" in found:
        report("Playwright render", "PASS", "recovered a JS-injected email static missed")
    else:
        report("Playwright render", "FAIL", f"rendered but email not found ({found or 'none'})")


# --------------------------------------------------------------------------- 4. provider

def check_provider() -> None:
    from providers import get_provider
    provider = get_provider(os.environ.get("PROVIDER"))
    if not provider or not provider.available():
        report("licensed provider", "SKIP", "set HUNTER_API_KEY or PDL_API_KEY (+ PROVIDER)")
        return
    name = os.environ.get("PROVIDER_TEST_NAME")
    domain = os.environ.get("PROVIDER_TEST_DOMAIN")
    if not (name and domain):
        report("licensed provider", "SKIP",
               f"{provider.name} key set; also set PROVIDER_TEST_NAME and PROVIDER_TEST_DOMAIN")
        return
    hit = provider.find_email(name, domain)
    if hit and hit.get("email"):
        report("licensed provider", "PASS", f"{provider.name} -> {hit['email']} ({hit['confidence']})")
    else:
        report("licensed provider", "FAIL", f"{provider.name} returned no email for {name} @ {domain}")


def check_https_verify() -> None:
    """Verification over HTTPS via a provider - works without outbound port 25."""
    from providers import get_provider
    provider = get_provider(os.environ.get("PROVIDER"))
    email = os.environ.get("VERIFY_EMAIL")
    if not provider or not provider.available():
        report("HTTPS verify (no port 25)", "SKIP", "set a provider key to verify without port 25")
        return
    if not email:
        report("HTTPS verify (no port 25)", "SKIP", "set VERIFY_EMAIL")
        return
    verdict = provider.verify(email)
    if verdict in ("valid", "invalid", "accept_all"):
        report("HTTPS verify (no port 25)", "PASS", f"{provider.name}: {email} -> {verdict}")
    elif verdict is None:
        report("HTTPS verify (no port 25)", "SKIP", f"{provider.name} has no verify endpoint")
    else:
        report("HTTPS verify (no port 25)", "FAIL", f"{provider.name}: {email} -> {verdict}")


def main() -> int:
    print("Live checks (SKIP just means not configured here):\n")
    for check in (check_port_25, check_smtp, check_https_verify, check_render, check_provider):
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - a crashing check is a FAIL, not a traceback
            report(check.__name__.replace("check_", ""), "FAIL", f"{exc.__class__.__name__}: {exc}")

    passed = sum(s == "PASS" for _, s in results)
    failed = sum(s == "FAIL" for _, s in results)
    skipped = sum(s == "SKIP" for _, s in results)
    print(f"\n  {passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
