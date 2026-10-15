"""
batch.py - enrich a CSV of people in bulk.

Input CSV needs a 'name' column and a 'domain' (or 'company') column:
    name,domain
    Jane Doe,acme.com
    John Roe,example.org

Each domain is harvested once (crawled + learned) and cached, then every person on
that domain is enriched. Output is a CSV of name, domain, email, confidence, source.

    python batch.py people.csv --out results.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from urllib.parse import urlparse

from contact_finder import normalize_url, site_root
from engine import enrich, harvest
from store import Store


def run(in_path: str, out_path: str, db_path: str, max_pages: int, smtp: bool) -> None:
    store = Store(db_path)
    harvested: set[str] = set()

    with open(in_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("empty input", file=sys.stderr)
        return

    first = rows[0]
    name_key = next((k for k in first if k and k.lower() in ("name", "full_name", "fullname")), None)
    dom_key = next((k for k in first if k and k.lower() in ("domain", "company", "website", "url")), None)
    if not name_key or not dom_key:
        print("CSV needs a name column and a domain/company column", file=sys.stderr)
        return

    results = []
    for row in rows:
        name, raw_domain = (row.get(name_key) or "").strip(), (row.get(dom_key) or "").strip()
        if not name or not raw_domain:
            continue
        domain = site_root(urlparse(normalize_url(raw_domain)).netloc)

        if domain not in harvested:
            try:
                harvest(store, raw_domain, max_pages=max_pages)
            except (ValueError, PermissionError) as exc:
                print(f"  skip {domain}: {exc}", file=sys.stderr)
            harvested.add(domain)

        try:
            res = enrich(store, name, raw_domain, smtp=smtp)
            results.append({
                "name": name, "domain": domain, "email": res.get("email") or "",
                "confidence": res.get("confidence", 0), "source": res.get("source", ""),
            })
            print(f"  {name} @ {domain} -> {res.get('email')} ({res.get('confidence')})", file=sys.stderr)
        except (ValueError, PermissionError) as exc:
            results.append({"name": name, "domain": domain, "email": "", "confidence": 0, "source": f"error: {exc}"})

    store.close()
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "domain", "email", "confidence", "source"])
        writer.writeheader()
        writer.writerows(results)
    print(f"wrote {len(results)} rows to {out_path}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk-enrich a CSV of name,domain rows.")
    parser.add_argument("csv", help="input CSV with name and domain/company columns")
    parser.add_argument("--out", default="results.csv")
    parser.add_argument("--db", default="contacts.db")
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--smtp", action="store_true")
    args = parser.parse_args()
    run(args.csv, args.out, args.db, args.max_pages, args.smtp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
