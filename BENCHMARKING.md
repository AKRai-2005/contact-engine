# Benchmarking the email guesser

This explains how the accuracy numbers are produced, how to grow the ground-truth set, and
how to read the calibration metric. The goal is a benchmark you can trust and reproduce —
not a number tuned to look good.

## Current result

Run:

```bash
python measure_accuracy.py ground_truth.csv
```

Latest (10 orgs, 126 verified people):

| metric | value |
|---|---|
| top-1 accuracy | 0.786 |
| top-3 accuracy | 0.889 |
| expected calibration error (ECE) | 0.188 |

Per-org top-1 ranges from 0.39 (eff.org, a mixed non-collision scheme) to 1.00 (several
consistent-scheme orgs). The spread is the point: the guesser is strong on consistent orgs
and weak where an org's pattern is effectively arbitrary.

## Method: leave-one-out (no leakage)

For each person we learn the company's pattern from **every other person's** email, then
predict that person's address. The target never trains on itself, so the score is genuine
prediction, not memorisation. This is why a single-person org is skipped (nothing to learn
from) and why collision pairs must be kept intact — see below.

## How to grow the ground-truth set

More orgs = a more representative number and better-calibrated confidence. The set must stay
**verified** — a fabricated or mislabelled pair silently corrupts every metric.

1. **Harvest candidate orgs** (newsrooms, nonprofits, universities tend to publish staff
   emails; most corporates don't):

   ```bash
   python engine.py harvest someorg.org --max-pages 25 --db /tmp/gt.db
   ```

2. **Extract orientation-verified pairs.** Keep a `(name, email)` pair only when the email is
   derivable from the name in **first→last order** under a separated/initial pattern
   (`{first}.{last}`, `{f}{last}`, `{first}{l}`, …) — never a glued form like `{first}{last}`
   (matches dictionary words like `supremecourt`) and never a reversed `{last}.{first}`
   (admits scraped-backwards names like "Hartman Sarah"). Screen out role words
   (`info`, `press`, `litigation`, …). The helper used to build this set applies exactly
   these rules; re-use it rather than hand-copying, then **spot-check the output**.

3. **Keep collision pairs together.** If an org falls back to `{f}{last}` when a first name
   is taken (e.g. Aaron Jue = `aaron@`, Aaron Mackey = `amackey@`), include *both* people.
   Under leave-one-out the fallback is only learnable when its colliding partner is present;
   dropping one makes that case unpredictable and understates the model.

4. **Append and re-run.** Add the rows to `ground_truth.csv` (columns `name,domain,email`)
   and re-run `measure_accuracy.py`. Aim for a spread of org *types* and both consistent and
   mixed schemes.

### What makes a pair trustworthy

- The address is genuinely published by that org (it came out of a real harvest).
- The name is a real person's name (two tokens, no role/section words), not a scraped
  fragment like "Litigation Aaron".
- The email is clearly that person's — the distinctive local part confirms it
  (`jlynch` ↔ *Jennifer Lynch*), not a coincidental co-occurrence on a page.

## Reading calibration

`measure_accuracy.py` reports, for each rounded top-confidence bucket, the empirical accuracy
of guesses in it, plus a single **Expected Calibration Error (ECE)**: the population-weighted
average gap between predicted confidence and actual accuracy. 0 = perfect; lower is better.

A well-calibrated model's 0.9 bucket is right ~90% of the time. In the current set the ~0.9
bucket (n=73) is right 97% — so **high-confidence output can be trusted and thresholded**
(e.g. "only act on ≥0.9"). The ~0.4 band is overconfident, which is why ECE is still 0.188
rather than near zero.

### Validating on a held-out split (stricter)

Leave-one-out already cross-validates within each org. For a cross-org check that confidence
transfers to *unseen* orgs, split the file by domain — learn only on the train orgs, predict
the test orgs — and compare the ECE. If calibration holds out-of-org, the confidences are
safe to use as probabilities; if ECE jumps, treat them only as a ranking. This split harness
is a good next addition; the per-bucket data it needs is already emitted today.

## Honest limits of this benchmark

- **Sampled toward orgs that publish emails.** Corporations (which mostly don't) are
  under-represented, so the real-world number on a corporate domain is likely lower.
- **Labels depend on the harvester's name-linking**, which is heuristic; the strict extractor
  filters most noise, but spot-checking remains necessary.
- **It measures pattern prediction only** — not whether the mailbox exists. Pair it with SMTP
  verification (see the README) to turn a ranked guess into a confirmed address.
