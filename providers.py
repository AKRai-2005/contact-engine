"""
providers.py - optional licensed-data providers, the legitimate path to coverage the
public-web engine can't reach on its own (personal emails, people not on the company site).

Each provider wraps a paid API you bring your own key for (via an environment variable).
Nothing here scrapes anyone; it queries a service you pay for and that handles its own
compliance. With no key set, a provider reports itself unavailable and the engine simply
falls back to its pattern guess - so the code runs the same whether or not you have a key.

    export HUNTER_API_KEY=...      # https://hunter.io  (email-finder endpoint)
    export PDL_API_KEY=...         # https://peopledatalabs.com (person enrich endpoint)
"""

from __future__ import annotations

import os

import requests

from email_guesser import split_name

TIMEOUT = 12


class LicensedProvider:
    """Base class: reads its API key from `env_key`; `available()` is True once a key is set."""
    name = "base"
    env_key = ""

    def __init__(self, api_key: str | None = None, timeout: int = TIMEOUT):
        self.api_key = api_key or os.environ.get(self.env_key, "")
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    def find_email(self, full_name: str, domain: str) -> dict | None:
        """Return {"email", "confidence", "source"} or None. Never raises on network/API error."""
        raise NotImplementedError

    def verify(self, email: str) -> str | None:
        """Verify an address over HTTPS (no port 25). Returns a verdict
        ('valid' / 'invalid' / 'accept_all' / 'unknown ...') or None if unsupported/unavailable."""
        return None


class HunterProvider(LicensedProvider):
    name = "hunter"
    env_key = "HUNTER_API_KEY"

    def find_email(self, full_name: str, domain: str) -> dict | None:
        if not self.available():
            return None
        first, last = split_name(full_name)
        try:
            resp = requests.get(
                "https://api.hunter.io/v2/email-finder",
                params={"domain": domain, "first_name": first, "last_name": last,
                        "api_key": self.api_key},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = (resp.json() or {}).get("data") or {}
        except (requests.RequestException, ValueError):
            return None
        if not data.get("email"):
            return None
        return {"email": data["email"].lower(),
                "confidence": round((data.get("score") or 0) / 100, 3),   # Hunter score is 0-100
                "source": f"licensed:{self.name}"}

    def verify(self, email: str) -> str | None:
        """Hunter's email-verifier endpoint - HTTPS, so it works without outbound port 25."""
        if not self.available():
            return None
        try:
            resp = requests.get(
                "https://api.hunter.io/v2/email-verifier",
                params={"email": email, "api_key": self.api_key},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = (resp.json() or {}).get("data") or {}
        except (requests.RequestException, ValueError):
            return None
        if data.get("accept_all"):
            return "accept_all"
        # Hunter 'status' is one of: deliverable | undeliverable | risky | unknown
        return {"deliverable": "valid", "undeliverable": "invalid"}.get(
            data.get("status"), f"unknown ({data.get('status')})")


class PeopleDataLabsProvider(LicensedProvider):
    name = "pdl"
    env_key = "PDL_API_KEY"

    def find_email(self, full_name: str, domain: str) -> dict | None:
        if not self.available():
            return None
        try:
            resp = requests.get(
                "https://api.peopledatalabs.com/v5/person/enrich",
                params={"name": full_name, "company": domain, "min_likelihood": 6},
                headers={"X-Api-Key": self.api_key},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            body = resp.json() or {}
        except (requests.RequestException, ValueError):
            return None
        if body.get("status") != 200:
            return None
        data = body.get("data") or {}
        emails = data.get("work_email") or (data.get("emails") or [{}])[0].get("address")
        if not emails:
            return None
        email = emails if isinstance(emails, str) else emails[0]
        return {"email": email.lower(),
                "confidence": round((body.get("likelihood") or 0) / 10, 3),   # PDL likelihood 0-10
                "source": f"licensed:{self.name}"}


PROVIDERS = {"hunter": HunterProvider, "pdl": PeopleDataLabsProvider}


def get_provider(name: str | None = None) -> LicensedProvider | None:
    """A named provider, or the first one with a key set, or None if none are configured."""
    if name:
        cls = PROVIDERS.get(name.lower())
        return cls() if cls else None
    for cls in (PeopleDataLabsProvider, HunterProvider):
        p = cls()
        if p.available():
            return p
    return None
