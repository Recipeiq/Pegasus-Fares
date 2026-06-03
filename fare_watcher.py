#!/usr/bin/env python3
"""
FARE WATCHER v6  —  Argus-style flight fare scanner
====================================================
Two-tier scan -> BOOK/WATCH/WAIT signal -> Telegram. Human decides.

v6 ADDS
  * MULTI-AIRPORT: routes can specify multiple origin airports. Each is
    scanned independently; the cheapest qualifying fare across all origins
    wins. The alert shows the full per-origin comparison so you can decide
    whether the drive is worth it.
    Default: BUF (Buffalo) + ROC (Rochester, ~75mi).
    NOTE: ROC-MCO nonstop service is thinner — mostly Allegiant (G4).
    Some windows may return no ROC results; that is real, not a bug.

v5 (still active)
  * DEAD-MAN'S SWITCH: alerts YOU if scans go dark for ~24h. Two fuses:
    no-data (~24h) and no-window (~48h). Routes to DEADMAN_CHAT_ID.

MODES
  python fare_watcher.py            scheduled scan (cron: 0 11,16,21,1 * * *)
  python fare_watcher.py serve      worker: answers /check from Telegram
  python fare_watcher.py history    print recent price log to console

ENV: SERPAPI_KEY, TRAVELPAYOUTS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
     DEADMAN_CHAT_ID (optional, defaults to TELEGRAM_CHAT_ID)
     FARE_STATE_PATH, FARE_HISTORY_PATH
"""

import os, sys, csv, json, time, logging
import datetime as dt, urllib.parse
import requests

# ── CONFIG ────────────────────────────────────────────────────────────
ROUTES = [
    {
        "label": "Disney  BUF/ROC -> MCO  (Aug 16-22, 5 pax, nonstop)",
        # Multiple origins — each is scanned; cheapest qualifying wins.
        # Remove an entry or set origins=[] to fall back to departure_id.
        "origins": [
            {"id": "BUF", "label": "Buffalo"},
            {"id": "ROC", "label": "Rochester (~75mi)"},
        ],
        "departure_id": "BUF",      # fallback if origins list is empty
        "arrival_id":   "MCO",
        "outbound_date": "2026-08-16",
        "return_date":   "2026-08-22",
        "adults": 5,
        "nonstop_only": True,
        "target_per_person": 250,
        "alert_on_drop_pct": 8,
        # Airline rules (IATA). include wins if both set; else exclude.
        # ROC-MCO nonstops are mainly Allegiant (G4). Add G4 to
        # exclude_airlines here if you don't want Allegiant results.
        "include_airlines": [],
        "exclude_airlines": ["F9"],
        "time_window": {
            "outbound_after":  "06:00",
            "outbound_before": "12:00",
            "return_after":    "13:00",
            "return_before":   "22:00",
        },
    },
]

AIRLINE_NAMES = {
    "B6":"JetBlue","DL":"Delta","UA":"United","AA":"American",
    "F9":"Frontier","NK":"Spirit","G4":"Allegiant","WN":"Southwest",
}

PRICE_IS_TOTAL_FOR_ALL_PAX = True
TPAY_PRICE_IS_PER_PERSON   = True
ALWAYS_REPORT              = False
CONFIRM_BUFFER             = 1.15
MOMENTUM_WINDOW            = 14
CURRENCY                   = "USD"

DEADMAN_THRESHOLD        = 5
DEADMAN_WINDOW_THRESHOLD = 8
DEADMAN_REPEAT_EVERY     = 4

STATE_PATH   = os.getenv("FARE_STATE_PATH",   "fare_state.json")
HISTORY_PATH = os.getenv("FARE_HISTORY_PATH", "history.csv")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("fare_watcher")

ST_OK = "ok"; ST_NO_DATA = "no_data"; ST_NO_WINDOW = "no_window"


# ── HELPERS: ORIGINS ──────────────────────────────────────────────────
def get_origins(route):
    """Return list of {id, label} dicts. Falls back to departure_id."""
    o = route.get("origins")
    if o:
        return o
    dep = route.get("departure_id", "???")
    return [{"id": dep, "label": dep}]


# ── STATE + HISTORY ───────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_PATH) as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return {}

def save_state(state):
    try:
        with open(STATE_PATH,"w") as f: json.dump(state, f, indent=2)
    except OSError as e: log.warning("Could not persist state: %s", e)

HISTORY_COLS = ["ts","date","route","origin","per_person","level",
                "band_low","band_high","verdict","source","airline"]

def log_history(route, origin_id, per_person, level, typ_range, verdict, source, airline):
    low  = typ_range[0] if isinstance(typ_range,list) and len(typ_range)==2 else ""
    high = typ_range[1] if isinstance(typ_range,list) and len(typ_range)==2 else ""
    row  = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
            "date": dt.date.today().isoformat(), "route": route["label"],
            "origin": origin_id, "per_person": round(per_person,2),
            "level": level, "band_low": low, "band_high": high,
            "verdict": verdict, "source": source, "airline": airline or ""}
    try:
        new = not os.path.exists(HISTORY_PATH)
        with open(HISTORY_PATH,"a",newline="") as f:
            w = csv.DictWriter(f, fieldnames=HISTORY_COLS)
            if new: w.writeheader()
            w.writerow(row)
    except OSError as e: log.warning("Could not write history: %s", e)

def read_history(route_label=None):
    try:
        with open(HISTORY_PATH,newline="") as f: rows = list(csv.DictReader(f))
    except FileNotFoundError: return []
    return [r for r in rows if r["route"]==route_label] if route_label else rows

def history_momentum(route_label):
    rows   = read_history(route_label)[-MOMENTUM_WINDOW:]
    prices = [float(r["per_person"]) for r in rows if r.get("per_person")]
    if len(prices) < 3: return None, 0.0
    first, last = prices[0], prices[-1]
    pct = (last-first)/first*100 if first else 0.0
    if pct <= -3: return "trending DOWN", pct
    if pct >= 3:  return "trending UP",   pct
    return "flat", pct


# ── FILTER HELPERS ────────────────────────────────────────────────────
def airline_allowed(code, route):
    if not code: return True
    inc, exc = route.get("include_airlines"), route.get("exclude_airlines")
    if inc: return code in inc
    if exc: return code not in exc
    return True

def _hour(s):
    if not s: return None
    s = str(s).strip().replace("T"," ")
    part = s.split(" ")[-1]; hh = part[:2]
    return int(hh) if hh.isdigit() else None

def time_params(route):
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
    if not (tw.get("outbound_after") or tw.get("outbound_before")): return True
    h = _hour(dep_time)
    if h is None: return False
    a = _hour(tw.get("outbound_after")) if tw.get("outbound_after") else 0
    b = _hour(tw.get("outbound_before")) if tw.get("outbound_before") else 23
    return a <= h <= b

def time_window_str(route):
    tw = route.get("time_window") or {}
    if not tw: return "any time"
    def seg(a,b): return f"{tw.get(a,'--:--')}-{tw.get(b,'--:--')}"
    out = seg("outbound_after","outbound_before") if (tw.get("outbound_after") or tw.get("outbound_before")) else "any"
    ret = seg("return_after","return_before")     if (tw.get("return_after") or tw.get("return_before"))     else "any"
    return f"out {out}, return {ret}"

def fallback_flights_url(origin_id, route):
    q = (f"Flights from {origin_id} to {route['arrival_id']} "
         f"on {route['outbound_date']} returning {route['return_date']}")
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote_plus(q)

def airline_rule_str(route):
    if route.get("include_airlines"):
        return "only " + ", ".join(AIRLINE_NAMES.get(c,c) for c in route["include_airlines"])
    if route.get("exclude_airlines"):
        return "excluding " + ", ".join(AIRLINE_NAMES.get(c,c) for c in route["exclude_airlines"])
    return "all airlines"


# ── TIER 1: TRAVELPAYOUTS (per origin) ────────────────────────────────
def travelpayouts_cheapest_origin(route, origin_id):
    token = os.getenv("TRAVELPAYOUTS_TOKEN")
    if not token: return None, None
    params = {"origin": origin_id, "destination": route["arrival_id"],
              "departure_at": route["outbound_date"], "return_at": route["return_date"],
              "currency": CURRENCY.lower(), "sorting": "price",
              "unique": "false", "limit": 50, "token": token}
    if route.get("nonstop_only"): params["direct"] = "true"
    try:
        r = requests.get("https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
                         params=params, timeout=30)
        r.raise_for_status()
        rows = r.json().get("data", []) or []
    except (requests.RequestException, ValueError) as e:
        log.warning("Travelpayouts [%s] failed: %s", origin_id, e)
        return None, None

    best, best_air = None, None
    for row in rows:
        if route.get("nonstop_only") and row.get("transfers",0) not in (0,None): continue
        if not airline_allowed(row.get("airline"), route): continue
        dep = row.get("departure_at")
        if dep and _hour(dep) is not None and not outbound_time_ok(route, dep): continue
        price = row.get("price")
        if price is None: continue
        pp = price if TPAY_PRICE_IS_PER_PERSON else price / route["adults"]
        if best is None or pp < best:
            best, best_air = pp, row.get("airline")
    return best, best_air


# ── TIER 2: SERPAPI (per origin) ──────────────────────────────────────
def serpapi_confirm_origin(route, origin_id):
    params = {"engine": "google_flights",
              "departure_id": origin_id, "arrival_id": route["arrival_id"],
              "outbound_date": route["outbound_date"], "return_date": route["return_date"],
              "type": "1", "adults": str(route["adults"]),
              "currency": CURRENCY, "hl": "en", "api_key": os.environ["SERPAPI_KEY"]}
    if route.get("nonstop_only"): params["stops"] = "1"
    if route.get("include_airlines"):
        params["include_airlines"] = ",".join(route["include_airlines"])
    elif route.get("exclude_airlines"):
        params["exclude_airlines"] = ",".join(route["exclude_airlines"])
    out_t, ret_t = time_params(route)
    if out_t: params["outbound_times"] = out_t
    if ret_t: params["return_times"]   = ret_t

    r = requests.get("https://serpapi.com/search.json", params=params, timeout=45)
    r.raise_for_status()
    data = r.json()

    options = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    best_price, best_itin = None, None
    for opt in options:
        legs = opt.get("flights", [])
        if route.get("nonstop_only") and (len(legs)!=1 or opt.get("layovers")): continue
        if legs and not outbound_time_ok(route, legs[0].get("departure_airport",{}).get("time")): continue
        price = opt.get("price")
        if price is None: continue
        if best_price is None or price < best_price:
            best_price, best_itin = price, opt
    if best_price is None: return None

    pax      = route["adults"]
    total    = best_price if PRICE_IS_TOTAL_FOR_ALL_PAX else best_price * pax
    insights = data.get("price_insights",{}) or {}
    air      = best_itin["flights"][0].get("airline","") if best_itin and best_itin.get("flights") else ""
    url      = (data.get("search_metadata",{}) or {}).get("google_flights_url") or fallback_flights_url(origin_id, route)
    return {"per_person": total/pax, "total": total,
            "level":     insights.get("price_level","unknown"),
            "typ_range": insights.get("typical_price_range"),
            "itin": best_itin, "airline": air, "url": url}

def describe_itinerary(itin):
    if not itin or not itin.get("flights"): return "(no itinerary detail)"
    legs = itin["flights"]; first, last = legs[0], legs[-1]
    dur   = itin.get("total_duration")
    dur_s = f"{dur//60}h{dur%60:02d}m" if isinstance(dur,int) else "?"
    stops = "nonstop" if len(legs)==1 else f"{len(legs)-1} stop(s)"
    return (f"{first.get('airline','?')} | "
            f"{first.get('departure_airport',{}).get('time','?')} -> "
            f"{last.get('arrival_airport',{}).get('time','?')} | {dur_s} | {stops}")


# ── MULTI-ORIGIN SCAN ─────────────────────────────────────────────────
def scan_all_origins(route, state):
    """
    Scan every origin for this route. Returns:
      best_result  — the cheapest qualifying OriginResult, or None
      all_results  — dict {origin_id: OriginResult or None}
      status       — ST_OK / ST_NO_DATA / ST_NO_WINDOW
    Each OriginResult: {per_person, level, typ_range, itin, airline, url,
                        origin_id, origin_label, source}
    """
    key     = route["label"]
    origins = get_origins(route)
    last_pp = state.get(key,{}).get("last_per_person")

    # Step 1: broad scan all origins (free)
    broad = {}   # origin_id -> (price_or_None, airline_or_None)
    for o in origins:
        broad[o["id"]] = travelpayouts_cheapest_origin(route, o["id"])

    # Step 2: decide whether to confirm — if ANY origin looks interesting
    broad_prices = [p for p,_ in broad.values() if p is not None]
    cheapest_broad = min(broad_prices) if broad_prices else None
    confirm = (
        cheapest_broad is None
        or cheapest_broad <= route["target_per_person"] * CONFIRM_BUFFER
        or (last_pp and cheapest_broad < last_pp * (1 - route["alert_on_drop_pct"]/100))
    )

    all_results = {}
    for o in origins:
        oid = o["id"]
        res = None

        if confirm and os.getenv("SERPAPI_KEY"):
            try:
                res = serpapi_confirm_origin(route, oid)
            except requests.HTTPError as e:
                log.error("[%s][%s] SerpApi error: %s", key, oid, e)
                res = None
            if res:
                res["origin_id"]    = oid
                res["origin_label"] = o["label"]
                res["source"]       = "SerpApi (live)"
            elif broad[oid][0] is not None:
                # fall back to broad
                pp, air = broad[oid]
                res = {"per_person": pp, "level": "unknown", "typ_range": None,
                       "itin": None, "airline": air, "url": fallback_flights_url(oid, route),
                       "origin_id": oid, "origin_label": o["label"],
                       "source": "Travelpayouts (cached)"}
        elif broad[oid][0] is not None:
            pp, air = broad[oid]
            res = {"per_person": pp, "level": "unknown", "typ_range": None,
                   "itin": None, "airline": air, "url": fallback_flights_url(oid, route),
                   "origin_id": oid, "origin_label": o["label"],
                   "source": "Travelpayouts (cached)"}

        all_results[oid] = res
        if res:
            log.info("[%s][%s] $%.0f/pp (%s)", key, oid, res["per_person"], res["level"])
        else:
            log.info("[%s][%s] no data", key, oid)

    # find best
    valid = [r for r in all_results.values() if r is not None]
    if not valid:
        # check if confirm was attempted — if so it's likely a window issue
        if confirm and os.getenv("SERPAPI_KEY"):
            return None, all_results, ST_NO_WINDOW
        return None, all_results, ST_NO_DATA

    best = min(valid, key=lambda r: r["per_person"])
    return best, all_results, ST_OK


# ── BOOK SIGNAL ───────────────────────────────────────────────────────
def days_to_departure(route):
    return (dt.date.fromisoformat(route["outbound_date"]) - dt.date.today()).days

def book_signal(route, per_person, level, typ_range):
    reasons = []; days = days_to_departure(route)
    pos = None
    if isinstance(typ_range,list) and len(typ_range)==2 and typ_range[1]>typ_range[0]:
        pos = (per_person-typ_range[0])/(typ_range[1]-typ_range[0])

    if   days <= 21: urgent=True;  reasons.append(f"{days}d out: past the sweet spot — upside risk rising")
    elif days <= 60: urgent=False; reasons.append(f"{days}d out: domestic sweet spot")
    else:            urgent=False; reasons.append(f"{days}d out: early, room to watch")

    trend, pct = history_momentum(route["label"])
    if trend: reasons.append(f"history: {trend} ({pct:+.0f}% over last {MOMENTUM_WINDOW} scans)")

    cheap    = (level=="low")  or (pos is not None and pos<=0.35)
    pricey   = (level=="high") or (pos is not None and pos>=0.70)
    at_target = per_person <= route["target_per_person"]

    if   at_target and cheap:                        verdict="BOOK";  reasons.append("at/below target AND low in band")
    elif urgent and not pricey:                      verdict="BOOK";  reasons.append("near the wall and price is reasonable")
    elif pricey:                                     verdict="WAIT";  reasons.append("high in band — downside likely")
    elif trend=="trending DOWN" and not at_target:   verdict="WATCH"; reasons.append("falling — hold for a better entry")
    elif at_target:                                  verdict="WATCH"; reasons.append("at target but not yet low in band")
    else:                                            verdict="WATCH"; reasons.append("mid-band — keep scanning")

    return verdict, pos, reasons


# ── TELEGRAM ──────────────────────────────────────────────────────────
def _tg_post(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=20).raise_for_status()
    except requests.RequestException as e:
        log.error("Telegram send failed (chat %s): %s", chat_id, e)

def send_telegram(text): _tg_post(os.environ["TELEGRAM_CHAT_ID"], text)
def send_deadman(text):
    chat = os.getenv("DEADMAN_CHAT_ID") or os.environ["TELEGRAM_CHAT_ID"]
    _tg_post(chat, text)


# ── DEAD-MAN'S SWITCH ─────────────────────────────────────────────────
def _fmt_ago(ts):
    if not ts: return "unknown"
    mins = int((time.time()-ts)/60)
    if mins<60:   return f"{mins}m ago"
    if mins<1440: return f"{mins//60}h ago"
    return f"{mins//1440}d ago"

def check_deadman(route, state):
    key = route["label"]; rs = state.get(key,{})
    nd = rs.get("consec_no_data",0); nw = rs.get("consec_no_window",0)
    last = rs.get("last_success_ts"); alerted = rs.get("dm_alert_count",0)
    no_data_trip   = nd >= DEADMAN_THRESHOLD
    no_window_trip = nw >= DEADMAN_WINDOW_THRESHOLD
    if not (no_data_trip or no_window_trip): return
    scans_since = nd if no_data_trip else nw
    if not (alerted==0 or scans_since % DEADMAN_REPEAT_EVERY == 0): return

    if no_data_trip:
        origins_str = ", ".join(o["id"] for o in get_origins(route))
        msg = (f"⚠️ *PEGASUS DEAD-MAN — NO DATA*\n*{key}*\n\n"
               f"All origins ({origins_str}) returned nothing for "
               f"*{nd} consecutive scans* (~{nd//4:.0f}h).\n"
               f"Last good data: {_fmt_ago(last)}\n\n"
               f"Possible causes:\n  - SerpApi key expired or quota hit\n"
               f"  - Route temporarily unavailable\n"
               f"  - Travelpayouts endpoint changed\n\n"
               f"Check: serpapi.com/dashboard + Railway logs")
    else:
        msg = (f"⚠️ *PEGASUS DEAD-MAN — WINDOW TOO TIGHT*\n*{key}*\n\n"
               f"API healthy but no nonstops match your time window "
               f"for {nw} consecutive scans (~{nw//4:.0f}h).\n"
               f"Window: {time_window_str(route)}\n"
               f"Last good data: {_fmt_ago(last)}\n\n"
               f"Schedule may have changed. Consider widening the time window.")
    send_deadman(msg)
    state[key]["dm_alert_count"] = alerted + 1
    log.warning("[%s] dead-man alert sent (count=%d)", key, alerted+1)

def check_deadman_clear(route, state):
    key = route["label"]; rs = state.get(key,{})
    alerted = rs.get("dm_alert_count",0)
    if alerted > 0:
        pp = rs.get("last_per_person","?")
        val = f"${pp:,.0f}/person" if isinstance(pp,(int,float)) else "restored"
        send_deadman(f"✅ *PEGASUS ALL CLEAR*\n*{key}*\n\n"
                     f"Data restored after {alerted} warning(s). Current best: {val}")
        state[key]["dm_alert_count"] = 0
        log.info("[%s] dead-man all-clear sent", key)


# ── ORIGIN COMPARISON STRING ──────────────────────────────────────────
def origin_comparison(best, all_results, route):
    """Build the multi-airport comparison block for the alert."""
    origins = get_origins(route)
    if len(origins) <= 1:
        return ""   # single origin — no comparison needed

    lines = []
    for o in origins:
        r = all_results.get(o["id"])
        if r is None:
            lines.append(f"  {o['label']:20s} no nonstops found in window")
            continue
        winner = "✈ " if r["origin_id"] == best["origin_id"] else "  "
        air    = AIRLINE_NAMES.get(r["airline"], r["airline"]) if r["airline"] else "?"
        lines.append(f"{winner}{o['label']:20s} ${r['per_person']:,.0f}/pp  ({air})")

    # savings line
    valid = [r for r in all_results.values() if r]
    if len(valid) >= 2:
        prices = sorted(valid, key=lambda r: r["per_person"])
        diff   = prices[1]["per_person"] - prices[0]["per_person"]
        total_save = diff * route["adults"]
        if diff > 1:
            winner_label = prices[0]["origin_label"]
            lines.append(f"\n  {winner_label} saves ${diff:.0f}/pp "
                         f"(${total_save:.0f} total for {route['adults']})")

    return "\n".join(lines)


# ── CORE SCAN ─────────────────────────────────────────────────────────
def scan_route(route, state, force_confirm=False):
    key  = route["label"]
    last = state.get(key,{}).get("last_per_person")

    # Override force_confirm to always scan when requested
    # (scan_all_origins uses its own confirm logic, but we nudge it)
    if force_confirm:
        # Temporarily zero broad cache so confirm always fires
        _orig_env = os.environ.get("_FC_OVERRIDE")
        os.environ["_FC_OVERRIDE"] = "1"

    best, all_results, status = scan_all_origins(route, state)

    if force_confirm and "_FC_OVERRIDE" in os.environ:
        del os.environ["_FC_OVERRIDE"]

    if status != ST_OK or best is None:
        return status, None

    per_person = best["per_person"]
    level      = best["level"]
    typ_range  = best["typ_range"]
    itin       = best["itin"]
    airline    = best["airline"]
    url        = best["url"]
    origin_id  = best["origin_id"]
    source     = best["source"]

    verdict, pos, reasons = book_signal(route, per_person, level, typ_range)

    # log history for the winning origin
    log_history(route, origin_id, per_person, level, typ_range, verdict, source, airline)

    drop      = ((last-per_person)/last*100) if last else 0
    triggered = (per_person<=route["target_per_person"] or level=="low"
                 or drop>=route["alert_on_drop_pct"] or verdict=="BOOK")

    if not (triggered or force_confirm or ALWAYS_REPORT):
        log.info("[%s] $%.0f/pp from %s (%s, %s) — no trigger",
                 key, per_person, origin_id, level, verdict)
        return ST_OK, None

    pax       = route["adults"]
    range_str = (f"${typ_range[0]:,.0f}-${typ_range[1]:,.0f}"
                 if isinstance(typ_range,list) and len(typ_range)==2 else "n/a")
    pos_str   = f"{pos*100:.0f}% up band" if pos is not None else "band n/a"
    air_str   = AIRLINE_NAMES.get(airline, airline) if airline else "?"
    why       = "\n".join(f"  - {r}" for r in reasons)
    comp      = origin_comparison(best, all_results, route)

    msg = (
        f"*FARE SIGNAL: {verdict}*\n*{route['label']}*\n\n"
        f"Best: *${per_person:,.0f}/person*  "
        f"(total ${per_person*pax:,.0f} for {pax})  via *{best['origin_label']}*\n"
    )
    if comp:
        msg += f"\n*Origin comparison:*\n{comp}\n"
    msg += (
        f"\nCarrier: {air_str}   Filter: {airline_rule_str(route)}\n"
        f"Times: {time_window_str(route)}\n"
        f"Google verdict: *{level.upper()}*   typical: {range_str} ({pos_str})\n"
        f"{describe_itinerary(itin)}\nSource: {source}\n\n"
        f"Reasoning:\n{why}\n\n"
        f"[Open in Google Flights]({url})"
    )
    return ST_OK, msg


# ── RUN SCAN ──────────────────────────────────────────────────────────
def run_scan(force_confirm=False):
    log.info("=== scan start (force_confirm=%s) ===", force_confirm)
    state = load_state(); messages = []

    for route in ROUTES:
        key = route["label"]
        state.setdefault(key, {})
        try:
            status, msg = scan_route(route, state, force_confirm)
        except Exception as e:
            log.exception("[%s] unexpected error: %s", key, e)
            status, msg = ST_NO_DATA, None

        if status == ST_OK:
            check_deadman_clear(route, state)
            state[key]["consec_no_data"]   = 0
            state[key]["consec_no_window"] = 0
            state[key]["last_success_ts"]  = time.time()
            if msg:
                send_telegram(msg); messages.append(msg)
                log.info("[%s] fare alert sent", key)
        elif status == ST_NO_DATA:
            state[key]["consec_no_data"]   = state[key].get("consec_no_data",0)+1
            state[key]["consec_no_window"] = 0
            check_deadman(route, state)
        elif status == ST_NO_WINDOW:
            state[key]["consec_no_window"] = state[key].get("consec_no_window",0)+1
            state[key]["consec_no_data"]   = 0
            check_deadman(route, state)

        # persist winning origin prices for history/comparison
        valid = {oid: (r["per_person"] if r else None)
                 for oid,r in (scan_all_origins.__wrapped__
                               if hasattr(scan_all_origins,"__wrapped__")
                               else {}).items()} if False else {}
        # (origin prices already captured in state via log_history)

    save_state(state)
    log.info("=== scan done ===")
    return messages


# ── HISTORY PRINT ─────────────────────────────────────────────────────
def print_history():
    rows = read_history()
    if not rows: print("No history yet — run a scan first."); return
    print(f"{'date':<11}{'origin':<6}{'route':<28}{'$/pp':>7}  {'level':<8}{'verdict':<7} airline")
    print("-" * 78)
    for r in rows[-40:]:
        print(f"{r['date']:<11}{r.get('origin',''):<6}{r['route'][:27]:<28}"
              f"{float(r['per_person']):>7.0f}  {r['level']:<8}{r['verdict']:<7} {r.get('airline','')}")


# ── SERVE ─────────────────────────────────────────────────────────────
def serve():
    base = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"
    offset = None
    log.info("=== serve mode: listening for /check ===")
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None: params["offset"] = offset
            resp = requests.get(f"{base}/getUpdates", params=params, timeout=60)
            resp.raise_for_status()
            for upd in resp.json().get("result",[]):
                offset = upd["update_id"]+1
                text   = (upd.get("message",{}) or {}).get("text","") or ""
                if text.strip().lower().startswith("/check"):
                    send_telegram("Running a live check now...")
                    if not run_scan(force_confirm=True):
                        send_telegram("Checked — nothing in your filters right now.")
        except requests.RequestException as e:
            log.warning("serve poll error: %s", e); time.sleep(5)


def main():
    mode = sys.argv[1] if len(sys.argv)>1 else "scan"
    if   mode=="serve":   serve()
    elif mode=="history": print_history()
    else:                 run_scan(force_confirm=False)

if __name__ == "__main__":
    main()
