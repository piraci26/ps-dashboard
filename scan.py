#!/usr/bin/env python3
"""
P/S Dashboard scanner — pulls CBOE delayed option chains for the universe,
finds the ATM straddle for the next-Friday weekly expiry, and computes the
implied move ("expected return") priced in by the market:

    straddle = call_last + put_last     (ATM, both legs)
    implied_move = straddle / strike    (≈ % return by expiry)

Higher implied_move = the market is pricing more movement = richer premium
relative to underlying = better candidate to fade.

Run after Friday's close to lock in EOD pricing for the next-Friday weekly.
"""
import json, urllib.request, os, time, ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TICKERS = json.load(open(os.path.join(HERE, "universe.json")))
NAMES = json.load(open(os.path.join(HERE, "universe_names.json")))

CBOE = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
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


def last_or_mid(o):
    """Prefer last traded price. Fall back to mid if last is stale/zero."""
    last = o.get("last_trade_price")
    if last and last > 0:
        return last, "last"
    bid, ask = o.get("bid", 0), o.get("ask", 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2, "mid"
    theo = o.get("theo")
    if theo:
        return theo, "theo"
    return 0, "none"


def analyze(sym: str, payload: dict, target_expiry: date):
    if "error" in payload:
        return {"sym": sym, "error": payload["error"]}
    d = payload.get("data") or {}
    opts = d.get("options") or []
    spot = d.get("current_price")
    if not spot or not opts:
        return {"sym": sym, "error": "no data"}

    # Group by expiry → strike → {C, P}
    by_exp = {}
    for o in opts:
        try:
            exp, cp, k = parse_occ(o["option"], sym)
        except Exception:
            continue
        by_exp.setdefault(exp, {}).setdefault(k, {})[cp] = o

    if not by_exp:
        return {"sym": sym, "error": "no expiries parsed"}

    # Pick expiry closest to target_expiry
    expiries = sorted(by_exp.keys())
    best_exp = min(
        expiries,
        key=lambda e: abs(date.fromisoformat(e).toordinal() - target_expiry.toordinal()),
    )
    chain_by_strike = by_exp[best_exp]

    # Find ATM strike — closest strike that has BOTH a call and a put
    candidates = [(k, v) for k, v in chain_by_strike.items() if "C" in v and "P" in v]
    if not candidates:
        return {"sym": sym, "error": f"no straddle at {best_exp}"}
    atm_k, legs = min(candidates, key=lambda kv: abs(kv[0] - spot))
    call, put = legs["C"], legs["P"]
    call_px, call_src = last_or_mid(call)
    put_px,  put_src  = last_or_mid(put)
    if call_px <= 0 or put_px <= 0:
        return {"sym": sym, "error": f"no traded price at K={atm_k}"}

    straddle = call_px + put_px
    # Expected move = straddle / strike, expressed as a percentage
    implied_move = straddle / atm_k * 100
    # Also express as % of spot for comparability (when strike != spot)
    implied_move_spot = straddle / spot * 100
    dte = (date.fromisoformat(best_exp) - date.today()).days

    return {
        "sym": sym,
        "name": NAMES.get(sym, sym),
        "spot": round(spot, 2),
        "expiry": best_exp,
        "dte": dte,
        "iv30": round(d.get("iv30") or 0, 2),
        "strike": atm_k,
        "call_last": round(call_px, 3),
        "call_src": call_src,
        "put_last": round(put_px, 3),
        "put_src": put_src,
        "straddle": round(straddle, 3),
        "implied_move": round(implied_move, 3),
        "implied_move_spot": round(implied_move_spot, 3),
        "breakeven_up": round(spot + straddle, 2),
        "breakeven_dn": round(spot - straddle, 2),
        "moneyness": round((atm_k - spot) / spot * 100, 3),
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

    # Cross-sectional rank by implied_move
    srt = sorted(rows, key=lambda r: -r["implied_move"])
    for i, r in enumerate(srt, 1):
        r["rank"] = i

    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "target_expiry": target.isoformat(),
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
        f"in {out['scan_seconds']}s ({len(errors)} errors). expiry {target.isoformat()}"
    )


if __name__ == "__main__":
    run_scan()
