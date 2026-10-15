"""SMTP verification logic, tested offline against a fake mail server (no network)."""


import email_guesser
import engine
from store import Store


def test_classify_rcpt():
    assert email_guesser.classify_rcpt(250, catch_all=False) == "valid"
    assert email_guesser.classify_rcpt(251, catch_all=False) == "valid"
    assert email_guesser.classify_rcpt(550, catch_all=False) == "invalid"
    assert email_guesser.classify_rcpt(250, catch_all=True) == "accept_all"   # can't trust it
    assert email_guesser.classify_rcpt(450, catch_all=False).startswith("unknown")  # greylist


def _fake_smtp(valid: set, catch_all: bool = False):
    class Fake:
        def __init__(self, host, port, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo_or_helo_if_needed(self):
            pass

        def mail(self, sender):
            return (250, b"ok")

        def rcpt(self, addr):
            if catch_all:
                return (250, b"ok")
            if "noexist" in addr:            # the catch-all probe
                return (550, b"no such user")
            return (250, b"ok") if addr in valid else (550, b"no such user")

    return Fake


def test_smtp_verify_distinguishes_valid_and_invalid(monkeypatch):
    monkeypatch.setattr(email_guesser, "mx_hosts", lambda d: ["mx.acme.com"])
    monkeypatch.setattr(email_guesser.smtplib, "SMTP", _fake_smtp({"real@acme.com"}))
    report = email_guesser.smtp_verify("acme.com", ["real@acme.com", "ghost@acme.com"])
    assert report["status"] == "checked"
    assert report["results"]["real@acme.com"] == "valid"
    assert report["results"]["ghost@acme.com"] == "invalid"


def test_smtp_verify_flags_catch_all(monkeypatch):
    monkeypatch.setattr(email_guesser, "mx_hosts", lambda d: ["mx.acme.com"])
    monkeypatch.setattr(email_guesser.smtplib, "SMTP", _fake_smtp(set(), catch_all=True))
    report = email_guesser.smtp_verify("acme.com", ["anyone@acme.com"])
    assert report["status"] == "catch_all"
    assert report["results"]["anyone@acme.com"] == "accept_all"


def test_smtp_verify_reports_no_mx(monkeypatch):
    monkeypatch.setattr(email_guesser, "mx_hosts", lambda d: [])
    assert email_guesser.smtp_verify("acme.com", ["x@acme.com"])["status"] == "no_mx"


def test_enrich_persists_valid_verdict_and_boosts(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "t.db"))
    store.upsert_email("rsingh@acme.com", "acme.com", "personal", 0.6, on_site=True)
    # guesser points at the published address; SMTP confirms it
    monkeypatch.setattr(engine, "guess_email", lambda *a, **k: {
        "best_guess": "rsingh@acme.com", "basis": "x",
        "candidates": [{"email": "rsingh@acme.com", "pattern": "{f}{last}", "confidence": 0.5}]})
    monkeypatch.setattr(engine, "verify_email", lambda e: "valid")

    res = engine.enrich(store, "Raj Singh", "acme.com", smtp=True)
    assert res["status"] == "valid"
    assert res["confidence"] == 0.97
    assert store.get_email("rsingh@acme.com")["smtp_status"] == "valid"   # persisted
    store.close()


def test_enrich_marks_invalid_guess(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "t.db"))
    monkeypatch.setattr(engine, "guess_email", lambda *a, **k: {
        "best_guess": "wrong@acme.com", "basis": "x",
        "candidates": [{"email": "wrong@acme.com", "pattern": "{first}", "confidence": 0.4}]})
    monkeypatch.setattr(engine, "verify_email", lambda e: "invalid")

    res = engine.enrich(store, "Nobody Here", "acme.com", smtp=True)
    assert res["status"] == "invalid"
    assert res["confidence"] == 0.02
    store.close()
