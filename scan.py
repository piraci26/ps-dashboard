#!/usr/bin/env python3
"""
P/S Dashboard scanner — pulls CBOE delayed option chains for the full
universe, finds the ATM straddle for the next-Friday weekly expiry, and
computes the yield you collect for selling that straddle against
strike-sized capital:

    straddle_premium = call_last + put_last     (ATM, both legs)
    weekly_yield     = straddle_premium / strike

You post the strike as allocated capital, collect the straddle premium for
one week — that ratio is your gross weekly return on capital.

Run after Friday's close to lock in EOD pricing for the next-Friday weekly
(launchd: com.kuba.psscan, every Fri 21:30 UTC).
"""
import json, urllib.request, urllib.error, os, time, ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TICKERS = json.load(open(os.path.join(HERE, "universe.json")))
NAMES = json.load(open(os.path.join(HERE, "universe_names.json")))
SHARES = {k: v.get("shares") for k, v in json.load(open(os.path.join(HERE, "shares_outstanding.json"))).items()}

CBOE = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
SSL_CTX = ssl.create_default_context()


def next_friday(today: date) -> date:
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


def parse_occ(occ: str, root: str):
    body = occ[len(root):]
    yy, mm, dd = body[0:2], body[2:4], body[4:6]
    cp = body[6]
    strike = int(body[7:]) / 1000
    return f"20{yy}-{mm}-{dd}", cp, strike


def fetch(sym: str, retries: int = 3):
    url = CBOE.format(sym=sym)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
                return sym, json.loads(r.read())
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 + attempt * 2)
                continue
            if e.code == 404:
                return sym, {"error": "404 (no chain)"}
            return sym, {"error": last_err}
        except Exception as e:
            last_err = str(e)[:120]
            if attempt < retries - 1:
                time.sleep(1)
                continue
            return sym, {"error": last_err}
    return sym, {"error": last_err or "retries exhausted"}


def last_or_mid(o):
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


def live_mcap_b(sym: str, price: float):
    s = SHARES.get(sym)
    if s and price:
        return round(price * s / 1e9)
    return 0


def analyze(sym: str, payload: dict, target_expiry: date):
    if "error" in payload:
        return {"sym": sym, "error": payload["error"]}
    d = payload.get("data") or {}
    opts = d.get("options") or []
    spot = d.get("current_price")
    if not spot or not opts:
        return {"sym": sym, "error": "no data"}

    by_exp = {}
    for o in opts:
        try:
            exp, cp, k = parse_occ(o["option"], sym)
        except Exception:
            continue
        by_exp.setdefault(exp, {}).setdefault(k, {})[cp] = o
    if not by_exp:
        return {"sym": sym, "error": "no expiries parsed"}

    expiries = sorted(by_exp.keys())
    best_exp = min(
        expiries,
        key=lambda e: abs(date.fromisoformat(e).toordinal() - target_expiry.toordinal()),
    )
    chain_by_strike = by_exp[best_exp]
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
    yield_pct = straddle / atm_k * 100
    dte = (date.fromisoformat(best_exp) - date.today()).days
    # Put-call parity check: C − P should ≈ S − K (rate ≈ 0 at 1w).
    # Big deviation means at least one leg's last print is stale → flag it.
    parity_skew = abs((call_px - put_px) - (spot - atm_k))
    parity_skew_pct = parity_skew / spot * 100  # as % of spot
    stale = parity_skew_pct > 0.5  # >0.5% of spot = leg is stale

    return {
        "sym": sym,
        "name": NAMES.get(sym, sym),
        "mcap": live_mcap_b(sym, spot),
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
        "yield_pct": round(yield_pct, 3),
        "premium_per_100k": round(100_000 * straddle / atm_k, 2),
        "breakeven_up": round(spot + straddle, 2),
        "breakeven_dn": round(spot - straddle, 2),
        "parity_skew_pct": round(parity_skew_pct, 3),
        "stale": bool(stale),
        "call_oi": int(call.get("open_interest") or 0),
        "put_oi": int(put.get("open_interest") or 0),
        "data_ts": payload.get("timestamp"),
    }


def run_scan():
    t0 = time.time()
    target = next_friday(date.today())
    print(f"[{datetime.now(timezone.utc).isoformat()}] target expiry: {target.isoformat()} | universe: {len(TICKERS)}")

    raw = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(fetch, s): s for s in TICKERS}
        done = 0
        for f in as_completed(futures):
            sym, payload = f.result()
            raw[sym] = payload
            done += 1
            if done % 200 == 0:
                print(f"  ... {done}/{len(TICKERS)}")

    rows, errors = [], []
    for sym in TICKERS:
        r = analyze(sym, raw.get(sym, {}), target)
        if "error" in r:
            errors.append(r)
        else:
            rows.append(r)

    srt = sorted(rows, key=lambda r: -r["yield_pct"])
    for i, r in enumerate(srt, 1):
        r["rank"] = i

    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "target_expiry": target.isoformat(),
        "scan_seconds": round(time.time() - t0, 2),
        "scanned": len(rows),
        "universe_size": len(TICKERS),
        "errors": errors[:50],  # cap to keep JSON manageable
        "errors_total": len(errors),
        "rows": rows,
    }
    out_path = os.path.join(HERE, "docs", "results.json")
    with open(out_path, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(
        f"[{out['updated_at']}] scanned {len(rows)}/{len(TICKERS)} "
        f"in {out['scan_seconds']}s ({len(errors)} errors). expiry {target.isoformat()}"
    )


if __name__ == "__main__":
    run_scan()
