"""
api.py - a small HTTP API over the contact engine, shaped like Hunter's.

Run:
    pip install fastapi uvicorn
    uvicorn api:app --reload

Endpoints:
    POST /harvest   {"domain": "acme.com"}                 -> crawl + load the store
    GET  /domain/{domain}                                  -> emails known for a domain
    POST /enrich    {"name": "Jane Doe", "domain": "acme.com"}  -> best email for a person
    GET  /stats                                            -> store totals

Only public, self-published data is used. There is deliberately no endpoint that reveals
an individual's private email or phone from a third-party profile - that capability is
what the LinkedIn-scraping tools do, and it isn't built here.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from engine import enrich as engine_enrich
from engine import harvest as engine_harvest
from store import Store

DB_PATH = "contacts.db"
app = FastAPI(title="Contact Engine", version="1.0")


def get_store() -> Store:
    return Store(DB_PATH)


class HarvestBody(BaseModel):
    domain: str
    max_pages: int = 15


class EnrichBody(BaseModel):
    name: str
    domain: str
    smtp: bool = False


@app.post("/harvest")
def harvest(body: HarvestBody):
    store = get_store()
    try:
        return engine_harvest(store, body.domain, body.max_pages)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        store.close()


@app.post("/enrich")
def enrich(body: EnrichBody):
    store = get_store()
    try:
        return engine_enrich(store, body.name, body.domain, body.smtp)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        store.close()


@app.get("/domain/{domain}")
def domain(domain: str):
    store = get_store()
    try:
        emails = store.emails_for_domain(domain)
        if not emails:
            raise HTTPException(status_code=404, detail=f"nothing harvested for {domain} yet")
        return {"domain": domain, "org": store.org(domain), "emails": emails}
    finally:
        store.close()


@app.get("/stats")
def stats():
    store = get_store()
    try:
        return store.stats()
    finally:
        store.close()
