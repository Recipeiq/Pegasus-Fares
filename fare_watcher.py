#!/usr/bin/env python3
"""
FARE WATCHER v4  —  Argus-style flight fare scanner
====================================================
Two-tier scan -> BOOK/WATCH/WAIT signal -> Telegram. Human decides.

v4 ADDS
  * TIME-OF-DAY WINDOW: per-route outbound/return departure windows.
    Enforced at the SerpApi query (outbound_times/return_times) AND verified
    on the returned outbound legs. The "best" price now reflects only flights
    you'd actually take — so it may sit higher than the unconstrained cheapest.
  * BOOKING LINK: every alert includes a tap-to-open Google Flights link
    (SerpApi's own results URL when live, a query-URL fallback otherwise).

CAVEATS
  * Southwest (WN) doesn't distribute to aggregators — never appears. Check it
    directly on BUF->MCO.
  * Return-time filtering is applied server-side via the API param; the alert
    displays the OUTBOUND leg times (Google's round-trip response only carries
    outbound legs in the first stage).

MODES
  python fare_watcher.py            scheduled scan (cron: 0 7,12,17,21 * * *)
  python fare_watcher.py serve      worker: answers /check from Telegram
  python fare_watcher.py history    print recent price log to console

ENV: SERPAPI_KEY, TRAVELPAYOUTS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import sys
import csv
import json
import time
import logging
import datetime as dt
import urllib.parse
import requests

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
ROUTES = [
    {
        "label": "Disney  BUF -> MCO  (Aug 16-22, 5 pax, nonstop)",
        "departure_id": "BUF",
        "arrival_id": "MCO",
        "outbound_date": "2026-08-16",
        "return_date": "2026-08-22",
        "adults": 5,
        "nonstop_only": True,
        "target_per_person": 250,
        "alert_on_drop_pct": 8,
        # Airline rules (IATA). include wins if both set; else exclude.
        "include_airlines": [],
        "exclude_airlines": ["F9"],          # block Frontier
        # Time-of-day windows (24h "HH:MM"). Leave a side empty for "any".
        # Pre-filled with a sensible Disney-trip window — widen/empty to taste.
        "time_window": {
            "outbound_after": "06:00",
            "outbound_before": "12:00",
            "return_after": "13:00",
            "return_before": "22:00",
        },
    },
]

AIRLINE_NAMES = {
    "B6": "JetBlue", "DL": "Delta", "UA": "United", "AA": "American",
    "F9": "Frontier", "NK": "Spirit", "G4": "Allegiant", "WN": "Southwest",
}

PRICE_IS_TOTAL_FOR_ALL_PAX = True   # SerpApi — verify on run 1
TPAY_PRICE_IS_PER_PERSON = True     # Travelpayouts — verify on run 1
ALWAYS_REPORT = False
CONFIRM_BUFFER = 1.15
MOMENTUM_WINDOW = 14
CURRENCY = "USD"
STATE_PATH = os.getenv("FARE_STATE_PATH", "fare_state.json")
HISTORY_PATH = os.getenv("FARE_HISTORY_PATH", "history.csv")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("fare_watcher")


# ----------------------------------------------------------------------
# STATE + HISTORY
# ----------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        log.warning("Could not persist state: %s", e)


HISTORY_COLS = ["ts", "date", "route", "per_person", "level",
                "band_low", "band_high", "verdict", "source", "airline"]


def log_history(route, per_person, level, typ_range, verdict, source, airline):
    low = typ_range[0] if isinstance(typ_range, list) and len(typ_range) == 2 else ""
    high = typ_range[1] if isinstance(typ_range, list) and len(typ_range) == 2 else ""
    row = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
           "date": dt.date.today().isoformat(), "route": route["label"],
           "per_person": round(per_person, 2), "level": level,
           "band_low": low, "band_high": high, "verdict": verdict,
           "source": source, "airline": airline or ""}
    try:
        new = not os.path.exists(HISTORY_PATH)
        with open(HISTORY_PATH, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=HISTORY_COLS)
            if new:
                w.writeheader()
            w.writerow(row)
    except OSError as e:
        log.warning("Could not write history: %s", e)


def read_history(route_label=None):
    try:
        with open(HISTORY_PATH, newline="") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return []
    return [r for r in rows if r["route"] == route_label] if route_label else rows


def history_momentum(route_label):
    rows = read_history(route_label)[-MOMENTUM_WINDOW:]
    prices = [float(r["per_person"]) for r in rows if r.get("per_person")]
    if len(prices) < 3:
        return None, 0.0
    first, last = prices[0], prices[-1]
    pct = (last - first) / first * 100 if first else 0.0
    if pct <= -3:
        return "trending DOWN", pct
    if pct >= 3:
        return "trending UP", pct
    return "flat", pct


# ----------------------------------------------------------------------
# FILTER HELPERS (airline + time-of-day)
# ----------------------------------------------------------------------
def airline_allowed(code, route):
    if not code:
        return True
    inc, exc = route.get("include_airlines"), route.get("exclude_airlines")
    if inc:
        return code in inc
    if exc:
        return code not in exc
    return True


def _hour(s):
    """Extract the hour (0-23) from '2026-08-16 06:00', '06:25:00-04:00', etc."""
    if not s:
        return None
    s = str(s).strip().replace("T", " ")
    part = s.split(" ")[-1]
    hh = part[:2]
    return int(hh) if hh.isdigit() else None


def time_params(route):
    """Return (outbound_times, return_times) strings for SerpApi, or (None,None)."""
    tw = route.get("time_window") or {}
    out = ret = None
    if tw.get("outbound_after") or tw.get("outbound_before"):
        a = _hour(tw.get("outbound_after")) if tw.get("outbound_after") else 0
        b = _hour(tw.get("outbound_before")) if tw.get("outbound_before") else 23
        out = f"{a},{b}"
    if tw.get("return_after") or tw.get("return_before"):
        a = _hour(tw.get("return_after")) if tw.get("return_after") else 0
        b = _hour(tw.get("return_before")) if tw.get("return_before") else 23
        ret = f"{a},{b}"
    return out, ret


def outbound_time_ok(route, dep_time):
    tw = route.get("time_window") or {}
    if not (tw.get("outbound_after") or tw.get("outbound_before")):
        return True
    h = _hour(dep_time)
    if h is None:
        return False
    a = _hour(tw.get("outbound_after")) if tw.get("outbound_after") else 0
    b = _hour(tw.get("outbound_before")) if tw.get("outbound_before") else 23
    return a <= h <= b


def time_window_str(route):
    tw = route.get("time_window") or {}
    if not tw:
        return "any time"
    def seg(a, b):
        return f"{tw.get(a,'--:--')}-{tw.get(b,'--:--')}"
    out = seg("outbound_after", "outbound_before") if (tw.get("outbound_after") or tw.get("outbound_before")) else "any"
    ret = seg("return_after", "return_before") if (tw.get("return_after") or tw.get("return_before")) else "any"
    return f"out {out}, return {ret}"


def fallback_flights_url(route):
    q = (f"Flights from {route['departure_id']} to {route['arrival_id']} "
         f"on {route['outbound_date']} returning {route['return_date']}")
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote_plus(q)


# ----------------------------------------------------------------------
# TIER 1 — Travelpayouts (free, cached). Best-effort time filter.
# ----------------------------------------------------------------------
def travelpayouts_cheapest(route):
    token = os.getenv("TRAVELPAYOUTS_TOKEN")
    if not token:
        return None, None
    params = {"origin": route["departure_id"], "destination": route["arrival_id"],
              "departure_at": route["outbound_date"], "return_at": route["return_date"],
              "currency": CURRENCY.lower(), "sorting": "price", "unique": "false",
              "limit": 50, "token": token}
    if route.get("nonstop_only"):
        params["direct"] = "true"
    try:
        r = requests.get("https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
                         params=params, timeout=30)
        r.raise_for_status()
        rows = r.json().get("data", []) or []
    except (requests.RequestException, ValueError) as e:
        log.warning("Travelpayouts failed: %s", e)
        return None, None

    best, best_air = None, None
    for row in rows:
        if route.get("nonstop_only") and row.get("transfers", 0) not in (0, None):
            continue
        if not airline_allowed(row.get("airline"), route):
            continue
        # best-effort outbound time filter if a timestamp is present
        dep = row.get("departure_at")
        if dep and _hour(dep) is not None and not outbound_time_ok(route, dep):
            continue
        price = row.get("price")
        if price is None:
            continue
        pp = price if TPAY_PRICE_IS_PER_PERSON else price / route["adults"]
        if best is None or pp < best:
            best, best_air = pp, row.get("airline")
    return best, best_air


# ----------------------------------------------------------------------
# TIER 2 — SerpApi Google Flights (live + insights + airline + time filter)
# ----------------------------------------------------------------------
def serpapi_confirm(route):
    params = {"engine": "google_flights",
              "departure_id": route["departure_id"], "arrival_id": route["arrival_id"],
              "outbound_date": route["outbound_date"], "return_date": route["return_date"],
              "type": "1", "adults": str(route["adults"]),
              "currency": CURRENCY, "hl": "en", "api_key": os.environ["SERPAPI_KEY"]}
    if route.get("nonstop_only"):
        params["stops"] = "1"
    if route.get("include_airlines"):
        params["include_airlines"] = ",".join(route["include_airlines"])
    elif route.get("exclude_airlines"):
        params["exclude_airlines"] = ",".join(route["exclude_airlines"])
    out_t, ret_t = time_params(route)
    if out_t:
        params["outbound_times"] = out_t
    if ret_t:
        params["return_times"] = ret_t

    r = requests.get("https://serpapi.com/search.json", params=params, timeout=45)
    r.raise_for_status()
    data = r.json()

    options = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    best_price, best_itin = None, None
    for opt in options:
        legs = opt.get("flights", [])
        if route.get("nonstop_only") and (len(legs) != 1 or opt.get("layovers")):
            continue
        if legs and not outbound_time_ok(route, legs[0].get("departure_airport", {}).get("time")):
            continue
        price = opt.get("price")
        if price is None:
            continue
        if best_price is None or price < best_price:
            best_price, best_itin = price, opt
    if best_price is None:
        return None

    pax = route["adults"]
    total = best_price if PRICE_IS_TOTAL_FOR_ALL_PAX else best_price * pax
    insights = data.get("price_insights", {}) or {}
    air = best_itin["flights"][0].get("airline", "") if best_itin and best_itin.get("flights") else ""
    url = (data.get("search_metadata", {}) or {}).get("google_flights_url") or fallback_flights_url(route)
    return {"per_person": total / pax, "total": total,
            "level": insights.get("price_level", "unknown"),
            "typ_range": insights.get("typical_price_range"),
            "itin": best_itin, "airline": air, "url": url}


def describe_itinerary(itin):
    if not itin or not itin.get("flights"):
        return "(no itinerary detail)"
    legs = itin["flights"]
    first, last = legs[0], legs[-1]
    dur = itin.get("total_duration")
    dur_s = f"{dur // 60}h{dur % 60:02d}m" if isinstance(dur, int) else "?"
    stops = "nonstop" if len(legs) == 1 else f"{len(legs) - 1} stop(s)"
    return (f"{first.get('airline','?')} | "
            f"{first.get('departure_airport',{}).get('time','?')} -> "
            f"{last.get('arrival_airport',{}).get('time','?')} | {dur_s} | {stops}")


# ----------------------------------------------------------------------
# BOOK SIGNAL
# ----------------------------------------------------------------------
def days_to_departure(route):
    return (dt.date.fromisoformat(route["outbound_date"]) - dt.date.today()).days


def book_signal(route, per_person, level, typ_range):
    reasons = []
    days = days_to_departure(route)
    pos = None
    if isinstance(typ_range, list) and len(typ_range) == 2 and typ_range[1] > typ_range[0]:
        pos = (per_person - typ_range[0]) / (typ_range[1] - typ_range[0])

    if days <= 21:
        urgent = True
        reasons.append(f"{days}d out: past the sweet spot — upside risk rising")
    elif days <= 60:
        urgent = False
        reasons.append(f"{days}d out: domestic sweet spot")
    else:
        urgent = False
        reasons.append(f"{days}d out: early, room to watch")

    trend, pct = history_momentum(route["label"])
    if trend:
        reasons.append(f"history: {trend} ({pct:+.0f}% over last {MOMENTUM_WINDOW} scans)")

    cheap = (level == "low") or (pos is not None and pos <= 0.35)
    pricey = (level == "high") or (pos is not None and pos >= 0.70)
    at_target = per_person <= route["target_per_person"]

    if at_target and cheap:
        verdict = "BOOK"; reasons.append("at/below target AND low in band")
    elif urgent and not pricey:
        verdict = "BOOK"; reasons.append("near the wall and price is reasonable")
    elif pricey:
        verdict = "WAIT"; reasons.append("high in band — downside likely")
    elif trend == "trending DOWN" and not at_target:
        verdict = "WATCH"; reasons.append("falling — hold for a better entry")
    elif at_target:
        verdict = "WATCH"; reasons.append("at target but not yet low in band")
    else:
        verdict = "WATCH"; reasons.append("mid-band — keep scanning")

    return verdict, pos, reasons


# ----------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------
def send_telegram(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
            json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=20).raise_for_status()
    except requests.RequestException as e:
        log.error("Telegram send failed: %s", e)


def airline_rule_str(route):
    if route.get("include_airlines"):
        return "only " + ", ".join(AIRLINE_NAMES.get(c, c) for c in route["include_airlines"])
    if route.get("exclude_airlines"):
        return "excluding " + ", ".join(AIRLINE_NAMES.get(c, c) for c in route["exclude_airlines"])
    return "all airlines"


# ----------------------------------------------------------------------
# CORE SCAN
# ----------------------------------------------------------------------
def scan_route(route, state, force_confirm=False):
    key = route["label"]
    last = state.get(key, {}).get("last_per_person")

    broad, broad_air = travelpayouts_cheapest(route)
    confirm = (force_confirm or broad is None
               or broad <= route["target_per_person"] * CONFIRM_BUFFER
               or (last and broad < last * (1 - route["alert_on_drop_pct"] / 100)))

    level, typ_range, itin = "unknown", None, None
    source, airline, url = "Travelpayouts (cached)", broad_air, fallback_flights_url(route)
    if confirm and os.getenv("SERPAPI_KEY"):
        res = serpapi_confirm(route)
        if res:
            per_person = res["per_person"]
            level, typ_range, itin = res["level"], res["typ_range"], res["itin"]
            source, airline, url = "SerpApi (live)", res["airline"], res["url"]
        elif broad is not None:
            per_person = broad
        else:
            log.info("[%s] no qualifying flights (check time window?)", key); return None
    elif broad is not None:
        per_person = broad
    else:
        log.info("[%s] no data", key); return None

    verdict, pos, reasons = book_signal(route, per_person, level, typ_range)
    log_history(route, per_person, level, typ_range, verdict, source, airline)

    drop = ((last - per_person) / last * 100) if last else 0
    triggered = (per_person <= route["target_per_person"] or level == "low"
                 or drop >= route["alert_on_drop_pct"] or verdict == "BOOK")
    state[key] = {"last_per_person": per_person, "ts": time.time()}

    if not (triggered or force_confirm or ALWAYS_REPORT):
        log.info("[%s] $%.0f/pp (%s, %s) — no trigger", key, per_person, level, verdict)
        return None

    pax = route["adults"]
    range_str = (f"${typ_range[0]:,.0f}-${typ_range[1]:,.0f}"
                 if isinstance(typ_range, list) and len(typ_range) == 2 else "n/a")
    pos_str = f"{pos*100:.0f}% up band" if pos is not None else "band n/a"
    air_str = AIRLINE_NAMES.get(airline, airline) if airline else "?"
    why = "\n".join(f"  - {r}" for r in reasons)
    return (
        f"*FARE SIGNAL: {verdict}*\n*{route['label']}*\n\n"
        f"Best: *${per_person:,.0f}/person*  (total ${per_person*pax:,.0f} for {pax})\n"
        f"Carrier: {air_str}   Filter: {airline_rule_str(route)}\n"
        f"Times: {time_window_str(route)}\n"
        f"Google verdict: *{level.upper()}*   typical: {range_str} ({pos_str})\n"
        f"{describe_itinerary(itin)}\nSource: {source}\n\n"
        f"Reasoning:\n{why}\n\n"
        f"[Open in Google Flights]({url})"
    )


def run_scan(force_confirm=False):
    log.info("=== scan start (force_confirm=%s) ===", force_confirm)
    state = load_state()
    messages = []
    for route in ROUTES:
        try:
            msg = scan_route(route, state, force_confirm)
            if msg:
                send_telegram(msg); messages.append(msg)
                log.info("[%s] alert sent", route["label"])
        except requests.HTTPError as e:
            log.error("[%s] API error: %s", route["label"], e)
        except Exception as e:
            log.exception("[%s] error: %s", route["label"], e)
    save_state(state)
    log.info("=== scan done ===")
    return messages


def print_history():
    rows = read_history()
    if not rows:
        print("No history yet — run a scan first."); return
    print(f"{'date':<11}{'route':<24}{'$/pp':>7}  {'level':<8}{'verdict':<7} airline")
    print("-" * 72)
    for r in rows[-40:]:
        print(f"{r['date']:<11}{r['route'][:23]:<24}{float(r['per_person']):>7.0f}  "
              f"{r['level']:<8}{r['verdict']:<7} {r.get('airline','')}")


def serve():
    base = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"
    offset = None
    log.info("=== serve mode: listening for /check ===")
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(f"{base}/getUpdates", params=params, timeout=60)
            resp.raise_for_status()
            for upd in resp.json().get("result", []):
                offset = upd["update_id"] + 1
                text = (upd.get("message", {}) or {}).get("text", "") or ""
                if text.strip().lower().startswith("/check"):
                    send_telegram("Running a live check now...")
                    if not run_scan(force_confirm=True):
                        send_telegram("Checked — nothing in your filters right now.")
        except requests.RequestException as e:
            log.warning("serve poll error: %s", e)
            time.sleep(5)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "serve":
        serve()
    elif mode == "history":
        print_history()
    else:
        run_scan(force_confirm=False)


if __name__ == "__main__":
    main()
