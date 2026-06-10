# Pegasus-Fares Changelog

All versions built and deployed on Railway (Recipeiq account).
Repo: `Recipeiq/Pegasus-Fares`

---

## v7.1 — 2026-06-04
**Bug fix: history_momentum crash on schema-shifted rows**
- `history_momentum` crashed with `ValueError: could not convert string to float: 'BUF'`
  on all scans after v6 deploy. Root cause: v6 added an `origin` column to history.csv
  but early rows written before v6 had columns shifted — origin value ("BUF") landed
  in the `per_person` slot, causing `float()` to throw on every momentum read.
- Fix: wrapped `float(r["per_person"])` in try/except — corrupted/shifted rows are
  silently skipped rather than crashing the entire scan.
- Data fix: `FARE_HISTORY_PATH=/data/history_v2.csv` set in Railway to start clean.
- Dead-man's switch correctly fired after 5 consecutive failed scans (~21h gap),
  confirming v5 fuse logic works end-to-end. All-clear sent on recovery.

## v7 — 2026-06-03
**Split one-ways**
- Prices outbound and return as separate one-way searches (SerpApi type=2)
- Allows different airlines per leg — the core value of split ticketing
- Compares split combo total vs round-trip; triggers independently if split crosses target
- Split block appended to alert showing per-leg carrier, times, savings vs RT
- Self-connect risk warning hardcoded into every split alert (no suppression)
- Split scan only fires when confirm is already running — no extra idle API calls
- `ticket_type` column (RT/SPLIT) added to history.csv; momentum reads RT rows only
- `return_time_ok()` helper for direction-aware time filtering on return one-way leg

## v6 — 2026-06-03
**Multi-airport origins**
- Routes now support an `origins` list: BUF (Buffalo) + ROC (Rochester, ~75mi)
- Each origin scanned independently per run; cheapest qualifying fare wins
- Confirm decision (spend SerpApi) based on whether ANY origin looks interesting
- Alert includes per-origin comparison block and drive-savings line
- Dead-man fires only if ALL origins go dark — one live origin = route alive
- `origin` column added to history.csv for per-airport trend tracking
- ROC-MCO nonstops primarily Allegiant (G4); noted in config comments

## v5 — 2026-06-03
**Dead-man's switch**
- Two independent fuses per route:
  - `DEADMAN_THRESHOLD` (default 5, ~24h): both tiers returned nothing
  - `DEADMAN_WINDOW_THRESHOLD` (default 8, ~48h): API healthy but no flights in time window
- Alerts once at threshold, then every 4 scans while still failing
- All-clear message sent when data returns after a failure period
- `DEADMAN_CHAT_ID` env var routes system warnings to operator only
- `scan_route` returns `(status, msg)` tuple: `ok`, `no_data`, `no_window`
- `run_scan` manages per-route failure counters in state.json

## v4 — 2026-06-03
**Time-of-day windows + booking link**
- Per-route `time_window` config: `outbound_after/before`, `return_after/before`
- Time filter enforced at SerpApi query level (outbound_times/return_times params)
- Belt-and-suspenders check on returned outbound leg departure times
- Best price now reflects only flights you'd actually take
- Google Flights URL (from SerpApi metadata) included in every alert as tap-to-open link
- Query-URL fallback when SerpApi URL unavailable

## v3 — 2026-06-03
**Airline filtering + price history**
- Per-route `include_airlines` and `exclude_airlines` lists (IATA codes)
- Filter applied at SerpApi query AND on Travelpayouts cached rows
- `history.csv` appended on every scan: date, price, level, band, verdict, source, airline
- `python fare_watcher.py history` prints last 40 rows as a table
- Momentum signal reads logged history (last 14 scans) instead of just last-vs-current
- Frontier (F9) blocked by default on Disney route

## v2 — 2026-06-03
**Two-tier scan + /check serve mode**
- Tier 1: Travelpayouts free cached scan (broad signal, no SerpApi spend)
- Tier 2: SerpApi confirm fires only when broad price meets confirm threshold
- Most quiet windows cost $0 in API spend
- `python fare_watcher.py serve` long-polls Telegram for `/check` command
- `/check` forces a live confirm scan and replies inline
- `CONFIRM_BUFFER` (1.15) controls the confirm trigger sensitivity

## v1 — 2026-06-03
**Initial build**
- SerpApi Google Flights scan for BUF→MCO, Aug 16-22, 5 pax, nonstop
- BOOK/WATCH/WAIT signal scoring: band position, days-to-departure phase, momentum
- Telegram alerts via Argus bot
- State persistence (fare_state.json) for last-seen price and drop detection
- `ALWAYS_REPORT` flag for testing without waiting for a trigger
- Deployed to Railway (Recipeiq account) as a cron service, 4x daily UTC
- Volume mounted at `/data` for persistent state and history

---

## Planned
- Split one-way alert to separate Telegram thread from round-trip alerts
- `/check BUF` origin-specific on-demand check
- Travelpayouts affiliate deep-link in booking link (passive revenue)
- Netlify dashboard auto-load from Railway volume endpoint
- Multi-user alert subscriptions
