"""
store.py - SQLite-backed store with entity resolution for the contact engine.

Holds three kinds of record - organisations, people, and emails - and merges new
observations into existing ones instead of duplicating them. Every field carries a
confidence score and the source it came from; confidence decays with age so stale data
ranks below fresh data.

This is the "database" layer the commercial tools have; here it's a single local file.
"""

from __future__ import annotations

import math
import sqlite3
import time
from contextlib import contextmanager

# A record loses half its confidence after this many days with no fresh sighting.
HALF_LIFE_DAYS = 365.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    domain      TEXT PRIMARY KEY,
    name        TEXT,
    email_pattern TEXT,          -- learned pattern, e.g. "{first}.{last}"
    pattern_confidence REAL DEFAULT 0,
    first_seen  REAL,
    last_seen   REAL
);

CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT,
    first_name  TEXT,
    last_name   TEXT,
    full_name   TEXT,
    title       TEXT,
    first_seen  REAL,
    last_seen   REAL,
    UNIQUE(domain, first_name, last_name)
);

CREATE TABLE IF NOT EXISTS emails (
    email       TEXT PRIMARY KEY,
    domain      TEXT,
    person_id   INTEGER,
    kind        TEXT,            -- 'personal' | 'role'
    on_site     INTEGER DEFAULT 0,
    mx_valid    INTEGER,         -- 1 / 0 / NULL (unknown)
    smtp_status TEXT,            -- 'valid' | 'invalid' | 'catch_all' | NULL
    base_confidence REAL DEFAULT 0,
    first_seen  REAL,
    last_seen   REAL
);

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject     TEXT,            -- the email / domain the source backs
    url         TEXT,
    seen_at     REAL,
    UNIQUE(subject, url)
);

CREATE INDEX IF NOT EXISTS idx_emails_domain ON emails(domain);
CREATE INDEX IF NOT EXISTS idx_people_domain ON people(domain);
"""


def decayed(confidence: float, last_seen: float, now: float | None = None) -> float:
    """Confidence after exponential time-decay since it was last confirmed."""
    now = now or time.time()
    age_days = max(0.0, (now - last_seen) / 86400.0)
    return round(confidence * math.pow(0.5, age_days / HALF_LIFE_DAYS), 4)


class Store:
    def __init__(self, path: str = "contacts.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @contextmanager
    def _tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ----------------------------------------------------------------- writes

    def upsert_org(self, domain: str, name: str | None = None,
                   pattern: str | None = None, pattern_confidence: float = 0.0) -> None:
        now = time.time()
        with self._tx() as c:
            row = c.execute("SELECT pattern_confidence FROM orgs WHERE domain=?", (domain,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO orgs(domain,name,email_pattern,pattern_confidence,first_seen,last_seen)"
                    " VALUES(?,?,?,?,?,?)",
                    (domain, name, pattern, pattern_confidence, now, now),
                )
            else:
                # keep the higher-confidence pattern; always refresh last_seen and any new name
                keep_pattern = pattern if pattern_confidence >= (row["pattern_confidence"] or 0) else None
                c.execute(
                    "UPDATE orgs SET name=COALESCE(?,name), last_seen=?,"
                    " email_pattern=COALESCE(?,email_pattern),"
                    " pattern_confidence=MAX(pattern_confidence,?) WHERE domain=?",
                    (name, now, keep_pattern, pattern_confidence, domain),
                )

    def upsert_person(self, domain: str, first: str, last: str,
                      full_name: str | None = None, title: str | None = None) -> int:
        now = time.time()
        with self._tx() as c:
            c.execute(
                "INSERT INTO people(domain,first_name,last_name,full_name,title,first_seen,last_seen)"
                " VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(domain,first_name,last_name) DO UPDATE SET"
                " last_seen=excluded.last_seen,"
                " full_name=COALESCE(people.full_name,excluded.full_name),"
                " title=COALESCE(excluded.title,people.title)",
                (domain, first, last, full_name, title, now, now),
            )
            return c.execute(
                "SELECT id FROM people WHERE domain=? AND first_name=? AND last_name=?",
                (domain, first, last),
            ).fetchone()["id"]

    def upsert_email(self, email: str, domain: str, kind: str, confidence: float,
                     on_site: bool = False, mx_valid: bool | None = None,
                     smtp_status: str | None = None, person_id: int | None = None,
                     sources: list[str] | None = None) -> None:
        now = time.time()
        with self._tx() as c:
            c.execute(
                "INSERT INTO emails(email,domain,person_id,kind,on_site,mx_valid,smtp_status,"
                "base_confidence,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(email) DO UPDATE SET last_seen=excluded.last_seen,"
                " person_id=COALESCE(excluded.person_id,emails.person_id),"
                " on_site=MAX(emails.on_site,excluded.on_site),"
                " mx_valid=COALESCE(excluded.mx_valid,emails.mx_valid),"
                " smtp_status=COALESCE(excluded.smtp_status,emails.smtp_status),"
                # more sightings raise confidence, but never above 0.99
                " base_confidence=MIN(0.99, MAX(emails.base_confidence,excluded.base_confidence)"
                "   + 0.05*(excluded.base_confidence>0))",
                (email, domain, person_id, kind, int(on_site), _tri(mx_valid), smtp_status,
                 confidence, now, now),
            )
            for url in sources or []:
                c.execute("INSERT OR IGNORE INTO sources(subject,url,seen_at) VALUES(?,?,?)",
                          (email, url, now))

    def set_smtp_status(self, email: str, status: str) -> None:
        """Record an SMTP verification verdict for an address, if we know the address."""
        with self._tx() as c:
            c.execute("UPDATE emails SET smtp_status=?, last_seen=? WHERE email=?",
                      (status, time.time(), email))

    # ----------------------------------------------------------------- reads

    def get_email(self, email: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM emails WHERE email=?", (email,)).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["confidence"] = decayed(rec["base_confidence"], rec["last_seen"])
        rec["sources"] = [r["url"] for r in self.conn.execute(
            "SELECT url FROM sources WHERE subject=? ORDER BY seen_at DESC", (email,))]
        return rec

    def org(self, domain: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM orgs WHERE domain=?", (domain,)).fetchone()
        return dict(row) if row else None

    def emails_for_domain(self, domain: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM emails WHERE domain=? ORDER BY base_confidence DESC", (domain,)).fetchall()
        out = []
        for row in rows:
            rec = dict(row)
            rec["confidence"] = decayed(rec["base_confidence"], rec["last_seen"])
            out.append(rec)
        return sorted(out, key=lambda r: -r["confidence"])

    def person_email(self, domain: str, first: str, last: str) -> dict | None:
        row = self.conn.execute(
            "SELECT e.* FROM emails e JOIN people p ON e.person_id=p.id"
            " WHERE p.domain=? AND p.first_name=? AND p.last_name=?"
            " ORDER BY e.base_confidence DESC LIMIT 1",
            (domain, first, last)).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec["confidence"] = decayed(rec["base_confidence"], rec["last_seen"])
        return rec

    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {
            "orgs": q("SELECT COUNT(*) FROM orgs"),
            "people": q("SELECT COUNT(*) FROM people"),
            "emails": q("SELECT COUNT(*) FROM emails"),
            "verified_emails": q("SELECT COUNT(*) FROM emails WHERE smtp_status='valid'"),
        }

    def close(self) -> None:
        self.conn.close()


def _tri(value: bool | None) -> int | None:
    return None if value is None else int(value)
