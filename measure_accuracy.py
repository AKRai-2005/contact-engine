"""
measure_accuracy.py - measure how good the email guesser actually is, and whether its
confidence scores mean anything (calibration).

Method: leave-one-out over a ground-truth set of known (name -> email) pairs per domain.
For each person we LEARN the company pattern from the *other* people's emails only, then
guess that person's email and check it. The target's own email never leaks into training,
so this measures genuine predictive accuracy, not memorisation.

Metrics:
  * top-1 accuracy  - the single best guess is exactly right
  * top-3 accuracy  - the right answer is among the top 3 guesses
  * calibration     - for guesses the model reports at confidence X, how often is it right?
                      (a well-calibrated 0.6 should be right ~60% of the time)

Input CSV columns: name, domain, email   (email = the person's real address)

    python measure_accuracy.py ground_truth.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict

from email_guesser import distributions_from_pairs, rank_candidates, split_name


def evaluate(rows: list[dict]) -> dict:
    by_domain: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for r in rows:
        name = (r.get("name") or "").strip()
        domain = (r.get("domain") or "").strip().lower()
        email = (r.get("email") or "").strip().lower()
        if not (name and domain and "@" in email):
            continue
        first, last = split_name(name)
        local = email.split("@")[0]
        by_domain[domain].append((first, last, local, email))

    total = top1 = top3 = 0
    calib: dict[float, list[int]] = defaultdict(list)   # confidence bucket -> [correct flags]
    conf_correct: list[tuple[float, int]] = []          # (predicted confidence, was-correct)
    per_domain: dict[str, list[int]] = defaultdict(list)
    misses = []

    for domain, people in by_domain.items():
        if len(people) < 2:
            print(f"  (skipping {domain}: needs >=2 known emails to leave one out)", file=sys.stderr)
            continue
        for i, (first, last, local, email) in enumerate(people):
            others = [(f, l, lc) for j, (f, l, lc, _) in enumerate(people) if j != i]
            shares, fallback_shares, occupied = distributions_from_pairs(others)
            cands = rank_candidates(first, last, domain, shares, occupied, fallback_shares)
            if not cands:
                continue
            total += 1
            ranked = [c["email"] for c in cands]
            correct1 = ranked[0] == email
            top1 += correct1
            top3 += email in ranked[:3]
            per_domain[domain].append(int(correct1))
            conf_correct.append((cands[0]["confidence"], int(correct1)))
            bucket = round(cands[0]["confidence"] * 10) / 10
            calib[bucket].append(int(correct1))
            if not correct1:
                misses.append({"name": f"{first} {last}", "domain": domain,
                               "true": email, "guessed": ranked[0], "confidence": cands[0]["confidence"]})

    calibration = {
        f"{b:.1f}": {"n": len(v), "empirical_accuracy": round(sum(v) / len(v), 2)}
        for b, v in sorted(calib.items())
    }
    by_domain_report = {
        d: {"n": len(v), "top1_accuracy": round(sum(v) / len(v), 2)}
        for d, v in sorted(per_domain.items(), key=lambda kv: -len(kv[1]))
    }
    # Expected Calibration Error: avg gap between predicted confidence and actual accuracy,
    # weighted by bin population. 0 = perfectly calibrated; lower is better.
    ece = sum(len(v) / total * abs((sum(v) / len(v)) - b) for b, v in calib.items()) if total else None

    return {
        "orgs_evaluated": len(by_domain_report),
        "people_evaluated": total,
        "top1_accuracy": round(top1 / total, 3) if total else None,
        "top3_accuracy": round(top3 / total, 3) if total else None,
        "expected_calibration_error": round(ece, 3) if ece is not None else None,
        "by_domain": by_domain_report,
        "calibration": calibration,
        "misses": misses,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Leave-one-out accuracy + calibration for the guesser.")
    parser.add_argument("csv", help="ground-truth CSV with name, domain, email columns")
    parser.add_argument("--show-misses", action="store_true")
    args = parser.parse_args()

    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    report = evaluate(rows)
    misses = report.pop("misses")
    import json
    print(json.dumps(report, indent=2))
    if args.show_misses and misses:
        print("\nMisses:", file=sys.stderr)
        for m in misses:
            print(f"  {m['name']} @ {m['domain']}: guessed {m['guessed']} "
                  f"(conf {m['confidence']}), actual {m['true']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
