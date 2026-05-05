#!/usr/bin/env python3
"""
P/S Dashboard scanner — pulls CBOE delayed option chains for the universe,
finds the 30Δ call/put for the next-Friday weekly expiry, computes
P/S = mid_premium / spot, and writes docs/results.json.

Run weekly after Friday close (or on demand). CBOE feed updates in delayed
fashion throughout the day; the prev_day_close / last_trade_time fields
indicate freshness.
"""
import json, urllib.request, os, time, ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TICKERS = json.load(open(os.path.join(HERE, "universe.json")))
NAMES = json.load(open(os.path.join(HERE, "universe_names.json")))

CBOE = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
TARGET_DELTA = 0.30
SSL_CTX = ssl.create_default_context()


def next_friday(today: date) -> date:
    """First Friday strictly after `today`. If today is Friday, returns the
    following Friday."""
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


def parse_occ(occ: str, root: str):
    """OCC symbol like 'AAPL260508C00280000' → ('2026-05-08', 'C', 280.0)."""
    body = occ[len(root):]
    yy, mm, dd = body[0:2], body[2:4], body[4:6]
    cp = body[6]
    strike = int(body[7:]) / 1000
    return f"20{yy}-{mm}-{dd}", cp, strike


def fetch(sym: str):
    url = CBOE.format(sym=sym)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
            return sym, json.loads(r.read())
    except Exception as e:
        return sym, {"error": str(e)[:120]}


def mid(o):
    bid, ask = o.get("bid", 0), o.get("ask", 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return o.get("theo") or o.get("last_trade_price") or 0


def closest(opts, target_delta):
    """Pick the option whose delta is closest to target_delta."""
    valid = [o for o in opts if o.get("delta") is not None]
    if not valid:
        return None
    return min(valid, key=lambda o: abs(o["delta"] - target_delta))


def analyze(sym: str, payload: dict, target_expiry: date):
    if "error" in payload:
        return {"sym": sym, "error": payload["error"]}
    d = payload.get("data") or {}
    opts = d.get("options") or []
    spot = d.get("current_price")
    if not spot or not opts:
        return {"sym": sym, "error": "no data"}

    # Group by expiry
    by_exp = {}
    for o in opts:
        try:
            exp, cp, k = parse_occ(o["option"], sym)
        except Exception:
            continue
        by_exp.setdefault(exp, {"C": [], "P": []})[cp].append({**o, "_strike": k})

    if not by_exp:
        return {"sym": sym, "error": "no expiries parsed"}

    # Pick expiry closest to target_expiry
    expiries = sorted(by_exp.keys())
    best_exp = min(
        expiries,
        key=lambda e: abs(date.fromisoformat(e).toordinal() - target_expiry.toordinal()),
    )
    chain = by_exp[best_exp]
    call = closest(chain["C"], TARGET_DELTA)
    put = closest(chain["P"], -TARGET_DELTA)
    if not call or not put:
        return {"sym": sym, "error": f"no 30Δ pair at {best_exp}"}

    call_mid, put_mid = mid(call), mid(put)
    dte = (date.fromisoformat(best_exp) - date.today()).days
    return {
        "sym": sym,
        "name": NAMES.get(sym, sym),
        "spot": round(spot, 2),
        "expiry": best_exp,
        "dte": dte,
        "iv30": round(d.get("iv30") or 0, 2),
        "call_strike": call["_strike"],
        "call_delta": round(call["delta"], 3),
        "call_mid": round(call_mid, 3),
        "call_iv": round(call.get("iv") or 0, 4),
        "call_oi": int(call.get("open_interest") or 0),
        "put_strike": put["_strike"],
        "put_delta": round(put["delta"], 3),
        "put_mid": round(put_mid, 3),
        "put_iv": round(put.get("iv") or 0, 4),
        "put_oi": int(put.get("open_interest") or 0),
        # P/S in percent of spot (more readable than raw ratio)
        "ps_call": round(call_mid / spot * 100, 3),
        "ps_put": round(put_mid / spot * 100, 3),
        "ps_avg": round((call_mid + put_mid) / 2 / spot * 100, 3),
        "data_ts": payload.get("timestamp"),
    }


def run_scan():
    t0 = time.time()
    target = next_friday(date.today())
    print(f"[{datetime.now(timezone.utc).isoformat()}] target expiry: {target.isoformat()}")

    raw = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        for f in as_completed([ex.submit(fetch, s) for s in TICKERS]):
            sym, payload = f.result()
            raw[sym] = payload

    rows, errors = [], []
    for sym in TICKERS:
        r = analyze(sym, raw.get(sym, {}), target)
        if "error" in r:
            errors.append(r)
        else:
            rows.append(r)

    # Cross-sectional ranks
    def rank(rows, key):
        srt = sorted(rows, key=lambda r: -r[key])
        out = {}
        for i, r in enumerate(srt, 1):
            out[r["sym"]] = i
        return out

    rank_call = rank(rows, "ps_call")
    rank_put = rank(rows, "ps_put")
    rank_avg = rank(rows, "ps_avg")
    for r in rows:
        r["rank_call"] = rank_call[r["sym"]]
        r["rank_put"] = rank_put[r["sym"]]
        r["rank_avg"] = rank_avg[r["sym"]]

    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "target_expiry": target.isoformat(),
        "target_delta": TARGET_DELTA,
        "scan_seconds": round(time.time() - t0, 2),
        "scanned": len(rows),
        "errors": errors,
        "rows": rows,
    }
    out_path = os.path.join(HERE, "docs", "results.json")
    with open(out_path, "w") as fh:
        json.dump(out, fh, indent=2)
    print(
        f"[{out['updated_at']}] scanned {len(rows)}/{len(TICKERS)} "
        f"in {out['scan_seconds']}s ({len(errors)} errors). target expiry {target.isoformat()}"
    )


if __name__ == "__main__":
    run_scan()
