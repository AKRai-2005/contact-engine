"""Offline unit tests for the extraction and parsing logic (no network)."""

from bs4 import BeautifulSoup

from contact_finder import (
    clean_email,
    decode_cfemail,
    extract_emails,
    extract_phones,
    extract_structured,
    guess_region,
    is_blocked,
    site_root,
)


def soup(html):
    return BeautifulSoup(html, "html.parser")


def test_clean_email_accepts_real_and_rejects_junk():
    assert clean_email("Jane.Doe@Acme.COM") == "jane.doe@acme.com"
    assert clean_email("hi@site.com.") == "hi@site.com"
    assert clean_email("logo@2x.png") is None          # asset, not email
    assert clean_email("noreply@acme.com") == "noreply@acme.com"
    assert clean_email("hi@example.com") is None        # example.com is a placeholder domain
    assert clean_email("a" * 50 + "@x.com") is None     # absurd local part
    assert clean_email("deadbeefdeadbeef@x.com") is None  # hex tracking id


def test_extract_emails_from_mailto_text_and_obfuscation():
    html = """
      <a href="mailto:sales@acme.com">mail</a>
      reach us at info (at) acme (dot) com or support[at]acme.com
      <p>press@acme.com</p>
    """
    found = extract_emails(html, soup(html))
    assert {"sales@acme.com", "info@acme.com", "support@acme.com", "press@acme.com"} <= found


def test_decode_cloudflare_email():
    # "test@a.com" encoded with key 0x7a (standard cfemail scheme)
    key = 0x7a
    plain = "test@a.com"
    enc = f"{key:02x}" + "".join(f"{ord(ch) ^ key:02x}" for ch in plain)
    assert decode_cfemail(enc) == plain


def test_extract_phones_tel_and_text():
    html = '<a href="tel:+1-202-555-0143">call</a><p>or +1 202 555 0188</p>'
    phones = extract_phones(soup(html), "US")
    assert "+12025550143" in phones
    assert "+12025550188" in phones


def test_structured_jsonld():
    html = """<script type="application/ld+json">
      {"@type":"Organization","email":"mailto:hello@acme.com","telephone":"+1-202-555-0100"}
    </script>"""
    emails, phones = extract_structured(soup(html))
    assert "hello@acme.com" in {e.replace("mailto:", "") for e in emails}
    assert "+1-202-555-0100" in phones


def test_site_root_and_region():
    assert site_root("www.Shop.Example.co.uk") == "shop.example.co.uk"
    assert guess_region("acme.in", "US") == "IN"
    assert guess_region("acme.com", "US") == "US"


def test_blocked_domains():
    assert is_blocked("linkedin.com")
    assert is_blocked("www.facebook.com")
    assert not is_blocked("acme.com")
