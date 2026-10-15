"""Store entity-resolution tests and the enrich store-hit bug fix (no network)."""

import time

import pytest

import engine
from store import Store, decayed


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def test_confidence_decays_with_age():
    now = time.time()
    assert decayed(1.0, now, now) == pytest.approx(1.0, abs=0.01)
    one_year_ago = now - 365 * 86400
    assert decayed(1.0, one_year_ago, now) == pytest.approx(0.5, abs=0.02)


def test_email_upsert_is_idempotent_and_merges(store):
    store.upsert_email("a@acme.com", "acme.com", "role", 0.8, on_site=True, sources=["u1"])
    store.upsert_email("a@acme.com", "acme.com", "role", 0.8, mx_valid=True, sources=["u2"])
    rec = store.get_email("a@acme.com")
    assert rec is not None
    assert rec["mx_valid"] == 1
    assert set(rec["sources"]) == {"u1", "u2"}        # merged, not duplicated
    assert store.stats()["emails"] == 1               # still one row


def test_person_upsert_unique_per_name(store):
    id1 = store.upsert_person("acme.com", "jane", "doe", "Jane Doe")
    id2 = store.upsert_person("acme.com", "jane", "doe", "Jane Doe", title="CTO")
    assert id1 == id2
    assert store.stats()["people"] == 1


def _seed(store):
    """A company that publishes jdoe@ tied to Jane Doe, and a role address."""
    pid = store.upsert_person("acme.com", "jane", "doe", "Jane Doe")
    store.upsert_email("jdoe@acme.com", "acme.com", "personal", 0.85,
                       on_site=True, mx_valid=True, person_id=pid, sources=["acme.com/team"])
    store.upsert_email("info@acme.com", "acme.com", "role", 0.8, on_site=True)


def test_enrich_returns_linked_person_address(store, monkeypatch):
    _seed(store)
    # guesser must not run for a direct person hit; make it fail loudly if called
    monkeypatch.setattr(engine, "guess_email", lambda *a, **k: pytest.fail("should not guess"))
    res = engine.enrich(store, "Jane Doe", "acme.com")
    assert res["email"] == "jdoe@acme.com"
    assert "linked to person" in res["source"]


def test_enrich_upgrades_guess_that_matches_published_address(store, monkeypatch):
    """The bug fix: a guess that is actually a published address is reported as published."""
    # a published address exists but is NOT linked to any person
    store.upsert_email("rsingh@acme.com", "acme.com", "personal", 0.85, on_site=True, mx_valid=True)

    def fake_guess(name, company, **k):
        return {"best_guess": "rsingh@acme.com", "basis": "learned",
                "candidates": [{"email": "rsingh@acme.com", "pattern": "{f}{last}", "confidence": 0.4},
                               {"email": "raj.singh@acme.com", "pattern": "{first}.{last}", "confidence": 0.3}]}

    monkeypatch.setattr(engine, "guess_email", fake_guess)
    res = engine.enrich(store, "Raj Singh", "acme.com")
    assert res["email"] == "rsingh@acme.com"
    assert res["source"] == "store (published address)"
    assert res["confidence"] >= 0.85            # upgraded from the stored evidence


def test_enrich_falls_back_to_guess_when_unknown(store, monkeypatch):
    _seed(store)

    def fake_guess(name, company, **k):
        return {"best_guess": "nobody@acme.com", "basis": "patterns only",
                "candidates": [{"email": "nobody@acme.com", "pattern": "{first}", "confidence": 0.3}]}

    monkeypatch.setattr(engine, "guess_email", fake_guess)
    res = engine.enrich(store, "Nobody Here", "acme.com")
    assert res["source"] == "pattern guess"
    assert res["email"] == "nobody@acme.com"
