"""Licensed-provider adapters and the enrich fallback tier, tested offline (mocked HTTP)."""

import engine
import providers
from store import Store


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_provider_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    assert providers.HunterProvider().available() is False
    assert providers.HunterProvider().find_email("Jane Doe", "acme.com") is None


def test_hunter_parses_response(monkeypatch):
    monkeypatch.setattr(providers.requests, "get",
                        lambda *a, **k: _Resp({"data": {"email": "Jane.Doe@acme.com", "score": 92}}))
    p = providers.HunterProvider(api_key="k")
    hit = p.find_email("Jane Doe", "acme.com")
    assert hit == {"email": "jane.doe@acme.com", "confidence": 0.92, "source": "licensed:hunter"}


def test_pdl_parses_response(monkeypatch):
    monkeypatch.setattr(providers.requests, "get",
                        lambda *a, **k: _Resp({"status": 200, "likelihood": 8,
                                               "data": {"work_email": "j.doe@acme.com"}}))
    p = providers.PeopleDataLabsProvider(api_key="k")
    hit = p.find_email("Jane Doe", "acme.com")
    assert hit["email"] == "j.doe@acme.com"
    assert hit["confidence"] == 0.8
    assert hit["source"] == "licensed:pdl"


def test_hunter_verify_maps_statuses(monkeypatch):
    p = providers.HunterProvider(api_key="k")
    cases = [
        ({"status": "deliverable", "accept_all": False}, "valid"),
        ({"status": "undeliverable", "accept_all": False}, "invalid"),
        ({"status": "risky", "accept_all": True}, "accept_all"),
        ({"status": "unknown", "accept_all": False}, "unknown (unknown)"),
    ]
    for payload, expected in cases:
        monkeypatch.setattr(providers.requests, "get",
                            lambda *a, _p=payload, **k: _Resp({"data": _p}))
        assert p.verify("x@acme.com") == expected


def test_verify_prefers_https_provider_over_smtp(monkeypatch):
    # provider HTTPS verify should be used; raw SMTP must NOT be called
    import email_guesser
    monkeypatch.setattr(email_guesser, "smtp_verify",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("SMTP used")))

    class _P:
        name = "fake"
        def available(self):
            return True
        def verify(self, email):
            return "valid"

    assert engine.verify_address("x@acme.com", _P()) == "valid"


def test_verify_falls_back_to_smtp_without_provider(monkeypatch):
    import email_guesser
    monkeypatch.setattr(email_guesser, "smtp_verify",
                        lambda domain, addrs: {"status": "checked", "results": {addrs[0]: "valid"}})
    monkeypatch.setattr(email_guesser, "mx_hosts", lambda d: ["mx"])
    assert engine.verify_address("x@acme.com", None) == "valid"


def test_provider_network_error_returns_none(monkeypatch):
    def boom(*a, **k):
        raise providers.requests.RequestException("down")
    monkeypatch.setattr(providers.requests, "get", boom)
    assert providers.HunterProvider(api_key="k").find_email("Jane Doe", "acme.com") is None


class _FakeProvider:
    def available(self):
        return True

    def find_email(self, name, domain):
        return {"email": "found@acme.com", "confidence": 0.88, "source": "licensed:fake"}


def test_enrich_uses_provider_when_guess_would_be_pure(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "t.db"))   # empty store -> no published hit
    monkeypatch.setattr(engine, "guess_email", lambda *a, **k: {
        "best_guess": "guess@acme.com", "basis": "patterns",
        "candidates": [{"email": "guess@acme.com", "pattern": "{first}", "confidence": 0.3}]})

    res = engine.enrich(store, "Jane Doe", "acme.com", provider=_FakeProvider())
    assert res["email"] == "found@acme.com"
    assert res["source"] == "licensed:fake"
    store.close()


def test_store_hit_beats_provider(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "t.db"))
    pid = store.upsert_person("acme.com", "jane", "doe", "Jane Doe")
    store.upsert_email("jane.doe@acme.com", "acme.com", "personal", 0.85, on_site=True, person_id=pid)
    # a linked store hit should win without the provider ever being consulted
    res = engine.enrich(store, "Jane Doe", "acme.com", provider=_FakeProvider())
    assert res["email"] == "jane.doe@acme.com"
    assert res["source"].startswith("store")
    store.close()
