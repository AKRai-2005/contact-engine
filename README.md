# Contact Engine

A legitimate, Hunter-style contact-discovery engine. It finds and verifies the contact
details organisations **publish about themselves**, learns each company's email naming
pattern, and enriches a name + domain into a best-guess work email with a confidence score.

It uses only public, self-published data. It does **not** log into or scrape third-party
profile sites (LinkedIn, etc.), and it has no feature that reveals an individual's private
email or phone — that capability, which the LinkedIn-scraping tools sell, depends on mass
profile scraping and covert contact-book harvesting and is deliberately not built here.

## Layout

| File | What it does |
|---|---|
| `contact_finder.py` | Crawl one site, return published emails + phone numbers (JSON). |
| `email_guesser.py`  | Guess a work email from name + domain; learns the company's pattern. |
| `store.py`          | SQLite store with entity resolution + time-decaying confidence. |
| `engine.py`         | `harvest` a domain into the store; `enrich` a person from it. |
| `batch.py`          | Bulk-enrich a CSV of `name,domain` rows. |
| `api.py`            | FastAPI HTTP service over the engine. |
| `renderer.py`       | Optional headless-browser (Playwright) rendering for JS-heavy sites. |
| `providers.py`      | Optional licensed-data APIs (Hunter, People Data Labs) as a fallback source. |
| `measure_accuracy.py` | Leave-one-out accuracy + confidence calibration (see `BENCHMARKING.md`). |
| `tests/`            | pytest suite (offline; no network) — 39 tests. |

## Install

```bash
pip install -r requirements.txt
```

## Use

```bash
# find what a site publishes
python contact_finder.py https://example.com

# guess one person's email (learns the pattern from the site)
python email_guesser.py "Jane Doe" example.com

# harvest a domain into the store, then enrich from it
python engine.py harvest example.com
python engine.py enrich "Jane Doe" example.com
python engine.py enrich "Jane Doe" example.com --smtp   # also verify over SMTP (see caveat)
python engine.py verify jane.doe@example.com            # verify one address, record result
python engine.py stats

# bulk enrich a CSV (needs name + domain columns)
python batch.py people.csv --out results.csv

# run the HTTP API
uvicorn api:app --reload

# run the tests
pip install pytest
python -m pytest tests -q

# measure guesser accuracy on a ground-truth CSV (name,domain,email)
python measure_accuracy.py ground_truth.csv --show-misses
```

## Accuracy (measured, not claimed)

`measure_accuracy.py` uses **leave-one-out**: for each known person it learns the company
pattern from everyone *else's* emails, then guesses that person's address. The target never
trains on itself, so this is genuine prediction, not memorisation.

`ground_truth.csv` is a verified, orientation-checked multi-org set (**126 people across 10
domains**). Every pair was confirmed from the organisation's own published emails — none are
fabricated. Larger orgs were auto-extracted with a strict filter that only accepts a pair
when the email is derivable from the name in first→last order, rejecting reversed captures
("Hartman Sarah") and role inboxes ("Campaigns Freedom"). See `BENCHMARKING.md` for the full
method and how to grow the set.

| overall (10 orgs, 126 people) | value |
|---|---|
| top-1 accuracy | **0.79** |
| top-3 accuracy | **0.89** |
| expected calibration error (ECE) | **0.188** |

Per-org top-1 runs from **1.00** on consistent-scheme orgs (citizen, theintercept,
earthjustice, revealnews, oceanconservancy, propublica) down to **0.39** on eff.org, whose
scheme is mixed and *not* collision-driven (staff use `{first}{l}`/`{f}{last}` even when
`{first}@` is free — unpredictable from patterns alone). The spread is honest: the guesser is
strong where a scheme exists and weak where it's effectively arbitrary.

- **Calibration is solid at the top end:** of the 73 guesses the model reports at ~0.9,
  **97% are correct**, so high-confidence output can be trusted / thresholded. The ~0.4 band
  stays overconfident, which is what keeps ECE at 0.188 rather than near zero.

### Mixed-scheme handling (`rank_candidates`)

The ranker is collision-aware: it knows which mailboxes an org already publishes, never
proposes one that's taken, and when a person's primary-pattern address *is* taken it scores
by the org's learned **fallback** pattern instead. Unit-tested, and it fires correctly —
with `aaron@` taken, "Aaron Mackey" is routed off `{first}` onto the fallback.

**texastribune.org (0.68) validates it:** that org uses `{first}.{last}` but drops to
`{f}{last}` on collisions (`matthew.watkins`→`mwatkins`), and because the ground truth keeps
both colliding people, the fallback is actually learnable there. Growing the set from 4→10
orgs lifted overall **top-1 0.54 → 0.79 and top-3 0.62 → 0.89**.

**eff.org (0.39) stays the worst case, honestly.** Its variation is mostly *not*
collision-driven — staff use `{first}{l}`/`{f}{last}` even when `{first}@` was free, a
historical choice no pattern model can predict. There, pattern-only guessing is near its
theoretical ceiling; the remaining cases need non-pattern signal (SMTP verification or an
actual-address source), which is the next lever.

`ground_truth_sample.csv` (eff.org only) is kept as the single-org worst case.

## Licensed-data fallback (`--provider`)

The public-web engine finds what orgs publish about themselves. For coverage it *can't* reach
— personal emails, people who aren't on the company site — the legitimate path is a paid data
API you bring your own key for. `providers.py` wraps two:

```bash
export HUNTER_API_KEY=...        # or: export PDL_API_KEY=...
python engine.py enrich "Jane Doe" acme.com --provider hunter
```

It slots in as a fallback tier in `enrich`: a store hit (a genuinely published address) still
wins; the provider is only consulted when the result would otherwise be a bare pattern guess,
and its answer is labelled `licensed:<provider>`. With no key set the provider reports itself
unavailable and the engine falls back to the guess — so the code runs the same either way.
No scraping is involved; the provider is a service that handles its own compliance.

## Rendering JS-heavy sites (`--render`)

The static crawler only sees server-returned HTML, so sites that build their contact/team
pages client-side come back nearly empty. Pass `--render` to fall back to a headless Chromium
(via Playwright) that runs the page's JavaScript first:

```bash
pip install playwright && playwright install chromium   # optional, one-time
python contact_finder.py https://example.com --render
python engine.py harvest example.com --render
```

It's **static-first**: each page is fetched normally, and the browser is only used when the
static HTML `looks_thin` (little text / no links / no mailto), so most pages stay fast. One
browser is reused for the whole crawl. Playwright is an **optional** dependency — if it isn't
installed (or the browser can't launch), the crawler prints a note and falls back to static
HTML; nothing breaks. (Pattern-learning's name scraping in `email_guesser` stays static, so a
fully client-rendered team page can still limit `enrich` name-linking.)

## Verification — two ways

### A. HTTPS verifier (no port 25) — recommended

Pass `--provider` and verification goes through the provider's HTTPS email-verifier endpoint
instead of a raw SMTP handshake, so it works from **any** host (home, cloud, this repo's CI):

```bash
export HUNTER_API_KEY=...
python engine.py verify jane.doe@acme.com --provider hunter
python engine.py enrich "Jane Doe" acme.com --smtp --provider hunter   # guess, then HTTPS-verify
```

`verify_address()` tries the provider first and falls back to raw SMTP only if no provider is
configured. This removes the port-25 requirement entirely when you have a key.

### B. Raw SMTP (the port-25 caveat)

With no provider, `--smtp` (and `engine.py verify <addr>`) asks the domain's mail server
whether a mailbox exists, using a `MAIL FROM:<>` + `RCPT TO` handshake and then `QUIT` —
**no email is ever sent.** A `valid` verdict pins confidence to 0.97; `invalid` drops it to
~0. Verdicts are persisted to the store's `smtp_status`, so a later lookup reuses them.

It degrades honestly instead of lying, and the reason is always in `status`:

| status | meaning |
|---|---|
| `valid` / `invalid` | the server confirmed / denied the mailbox |
| `catch_all` / `accept_all` | server accepts every address — **can't confirm**, treat as unknown |
| `port_25_blocked_or_timeout` | your network blocked outbound port 25 (see below) |
| `no_mx` | the domain has no mail server |
| `unknown (code)` | greylisted / rate-limited / policy block |

**The caveat:** verification needs **outbound TCP port 25**, which almost every home ISP
and most cloud providers block. On such a network every probe returns
`port_25_blocked_or_timeout` and nothing is verified — the guesser's pattern confidence is
all you get. To actually verify, run from a host that permits port 25 (a suitable VPS), and
keep volume low: aggressive probing gets the source IP blocklisted, and many large providers
(Google, Microsoft) greylist or rate-limit probes regardless.

## Notes & limits

- **Pattern learning** is only as good as the emails a site publishes; a site that lists
  no staff emails yields a generic ~30%-confidence guess.
- **Entity resolution** (tying an email to a person) is heuristic and will mislabel some
  records on unusually laid-out team pages.
- **JS-rendered pages** return little, since only raw HTML is read. Add Playwright to fix.
- **For real coverage** like Apollo/People Data Labs, plug in their licensed data API —
  that scale comes from consented contributory networks, not from crawling.

## Development

```bash
pip install -r requirements.txt pytest ruff
python -m pytest tests -q      # 29 tests, all offline (no network)
ruff check .                   # lint (clean)
```

- **CI:** `.github/workflows/ci.yml` runs the tests on Python 3.11 and 3.12, plus an
  advisory ruff lint, on every push and PR.
- **Docker:** `docker build -t contact-engine .` then, e.g.
  `docker run --rm -v "$PWD/data:/data" contact-engine enrich "Jane Doe" example.com --db /data/contacts.db`.
  The store persists on the mounted `/data` volume. See the `Dockerfile` header for the API
  and harvest commands. (SMTP verification still needs a port-25-capable host — see above.)

## Using it responsibly

This is for reaching organisations and people who publish their contact details, or whom
you have a lawful basis to contact. If you operate it as a service, you take on data-broker
obligations (GDPR legitimate-interest + notice, CCPA / California Delete Act, an opt-out and
suppression list). Don't mass-mail guessed addresses — it gets your domain flagged as spam.
