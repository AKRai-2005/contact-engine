"""Renderer fallback logic, tested with a fake browser (no Playwright / network needed)."""

import contact_finder
import email_guesser
from renderer import looks_thin


def test_looks_thin_detects_empty_js_shell():
    # a JS app shell: almost no text, no links
    assert looks_thin('<html><body><div id="root"></div></body></html>')
    assert looks_thin(None)
    # a real contact page is not thin
    rich = "<html><body>" + "<p>Our team works on policy. </p>" * 40 + \
           '<a href="/about">About</a><a href="mailto:a@acme.com">mail</a></body></html>'
    assert not looks_thin(rich)
    # a short page is fine as long as it already exposes a mailto
    assert not looks_thin('<a href="mailto:hi@acme.com">hi</a>')


class _FakeRenderer:
    """Stands in for Renderer: serves rendered HTML for URLs, records what it rendered."""
    def __init__(self, pages):
        self.pages = pages
        self.rendered = []

    def html(self, url):
        self.rendered.append(url)
        return self.pages.get(url)


def test_crawler_uses_renderer_only_when_static_is_thin(monkeypatch):
    thin_url = "https://acme.com/"
    rendered_html = ('<html><body><a href="mailto:jane@acme.com">Jane</a>'
                     '<a href="/team">Team</a></body></html>')
    fake = _FakeRenderer({thin_url: rendered_html})

    c = contact_finder.Crawler(thin_url, max_pages=1, region="US", renderer=fake)
    # static fetch returns an empty JS shell -> renderer should kick in
    monkeypatch.setattr(c, "session", type("S", (), {
        "get": lambda self, *a, **k: type("R", (), {
            "status_code": 200, "headers": {"Content-Type": "text/html"},
            "text": '<html><body><div id="root"></div></body></html>'})()})())

    html = c.fetch(thin_url)
    assert fake.rendered == [thin_url]          # renderer was used
    assert "jane@acme.com" in html              # we got the rendered DOM


def test_crawler_skips_renderer_when_static_is_rich(monkeypatch):
    url = "https://acme.com/contact"
    fake = _FakeRenderer({url: "<should not be used>"})
    rich = ("<html><body>" + "<p>lots of real content here. </p>" * 40 +
            '<a href="mailto:info@acme.com">info</a></body></html>')

    c = contact_finder.Crawler(url, max_pages=1, region="US", renderer=fake)
    monkeypatch.setattr(c, "session", type("S", (), {
        "get": lambda self, *a, **k: type("R", (), {
            "status_code": 200, "headers": {"Content-Type": "text/html"}, "text": rich})()})())

    html = c.fetch(url)
    assert fake.rendered == []                  # renderer NOT used - static was good enough
    assert "info@acme.com" in html


def test_learn_pattern_renders_thin_name_pages(monkeypatch):
    # one published personal email, whose source page is a thin JS shell
    monkeypatch.setattr(email_guesser, "find_contacts", lambda *a, **k: {
        "emails": [{"email": "jdoe@acme.com", "type": "personal",
                    "on_site_domain": True, "sources": ["https://acme.com/team"]}]})
    # static fetch of the team page yields an empty shell
    class _Resp:
        status_code, headers, text = 200, {"Content-Type": "text/html"}, '<div id="root"></div>'
    monkeypatch.setattr(email_guesser.requests, "Session",
                        lambda: type("S", (), {"headers": {}, "get": lambda self, *a, **k: _Resp()})())
    # the renderer supplies the real DOM, where "Jane Doe" appears next to jdoe@
    fake = _FakeRenderer({"https://acme.com/team": "<p>Our reporter Jane Doe covers courts.</p>"})

    learned = email_guesser.learn_pattern("acme.com", max_pages=5, renderer=fake)
    assert fake.rendered == ["https://acme.com/team"]        # rendered the thin name page
    assert learned["pattern_shares"].get("{f}{last}")        # jdoe <- Jane Doe learned
