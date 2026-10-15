# Running the live-only paths

Two features can't be exercised in a sandboxed/dev environment and need a one-time real run
to be considered proven. Both degrade safely until then (they report why and fall back), but
"it ran against a fake server" is not the same as "it worked in production." This is the
checklist to close that gap.

## One command for all of it

```bash
export VERIFY_EMAIL=you@your-domain.com VERIFY_BAD_EMAIL=nobody-xyz@your-domain.com
export HUNTER_API_KEY=...                          # optional
export PROVIDER_TEST_NAME="Jane Doe" PROVIDER_TEST_DOMAIN=acme.com   # optional
bash scripts/live_check.sh
```

It runs all four checks and prints `[PASS] / [FAIL] / [SKIP]` for each (a SKIP just means that
check isn't configured on this host), exiting non-zero if anything actually failed. Render is
a self-contained test (no external site needed). On a port-25-capable host with Playwright
installed and a key set, all four should PASS. The manual steps below are the same checks,
spelled out.

## 0. Verification without port 25 (recommended) — no VPS needed

If you have a provider key, you can skip the port-25 problem entirely: the HTTPS verifier runs
from any host, including this one.

```bash
export HUNTER_API_KEY=...
python engine.py verify you@your-domain.com --provider hunter   # expect: valid
```

`check_https_verify` in `scripts/live_check.py` proves this path. Only use the raw-SMTP route
below if you specifically want verification without any third-party API.

## 1. SMTP verification (needs outbound port 25)

Almost every home ISP and most cloud providers block outbound TCP port 25, so verification
can only run from a host that permits it.

1. Get a VPS/VM on a provider that allows port 25 outbound (some require you to request it).
2. Confirm the port is open:

   ```bash
   python -c "import socket; s=socket.create_connection(('gmail-smtp-in.l.google.com',25),10); print('port 25 OPEN'); s.close()"
   ```

   If this hangs or errors, port 25 is still blocked — stop here.
3. Verify a known-good and a known-bad address and confirm the verdicts:

   ```bash
   python engine.py verify someone@a-domain-you-control.com   # expect: valid
   python engine.py verify no-such-user@a-domain-you-control.com  # expect: invalid
   ```

4. Run an enrich with `--smtp` and confirm `status` becomes `valid`/`invalid` (not
   `port_25_blocked_or_timeout`) and that the verdict persists to the store.

Keep volume low: aggressive probing gets the source IP blocklisted, and Google/Microsoft
greylist or rate-limit regardless (you'll see `unknown (code)`), so test against a domain you
control.

## 2. Playwright rendering (needs a browser binary) — ✅ PROVEN

This path has been run for real with Chromium (not just mocked): on a page whose email is
assembled by JavaScript at runtime, a static fetch found `NONE` while the renderer recovered
`jane.doe@acme.com`. Static `looks_thin` correctly flagged the page so the fallback fired.
To reproduce / run against your own JS-heavy site:

1. Install the optional dependency and the browser:

   ```bash
   pip install -r requirements-optional.txt
   playwright install chromium
   ```

2. Pick a site whose contact/team page is client-rendered (nearly empty static HTML) and run:

   ```bash
   python contact_finder.py https://that-site.example --render
   ```

   Confirm the stderr shows `(rendered …)` for the thin pages and that emails/phones appear
   that a non-`--render` run misses. Then try `engine.py harvest … --render` and confirm the
   learned pattern improves for a JS-heavy team page.

## 3. Licensed-data provider (needs an API key)

1. Get a key (Hunter or People Data Labs) and export it:

   ```bash
   export HUNTER_API_KEY=...        # or PDL_API_KEY=...
   ```

2. Run an enrich for someone *not* on the company's public site and confirm the result is
   labelled `licensed:<provider>`:

   ```bash
   python engine.py enrich "Some Person" theircompany.com --provider hunter
   ```

3. Mind the cost and the provider's terms — each call is billed, and you take on the
   data-broker obligations described in the README's "Using it responsibly" section.

---

Until these three are run for real, treat SMTP, `--render`, and `--provider` as
**implemented and unit-tested, but not production-proven** — the honest status.
