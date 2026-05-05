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
_SHARES_RAW = json.load(open(os.path.join(HERE, "shares_outstanding.json")))
SHARES   = {k: v.get("shares")    for k, v in _SHARES_RAW.items()}
MCAP_REF = {k: v.get("mcap_ref")  for k, v in _SHARES_RAW.items()}
# Hardcoded fallbacks for tickers where stockanalysis returned no mcap_ref
MCAP_FALLBACK = {"BRK.B": 1100e9}

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
    """Market cap in $B. Prefer mcap_ref (vendor-reported) over price×shares
    because share counts for ADRs reflect the local listing, not the ADR ratio,
    and inflate the calc (e.g. TM, BCH, TSM)."""
    ref = MCAP_REF.get(sym) or MCAP_FALLBACK.get(sym)
    if ref:
        return round(ref / 1e9)
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
    dte = (date.fromisoformat(best_exp) - date.today()).days
    # Put-call parity check: C − P should ≈ S − K (rate ≈ 0 at 1w).
    parity_skew = abs((call_px - put_px) - (spot - atm_k))
    parity_skew_pct = parity_skew / spot * 100
    stale_atm = parity_skew_pct > 0.5

    # ─── OTM tail options at spot ± straddle ─────────────────────────
    # Move away from spot by the straddle amount; that's the breakeven strike.
    # Pull the put at the down strike, call at the up strike, divide by that
    # strike — that's the tail option's yield.
    down_target = spot - straddle
    up_target   = spot + straddle

    def find_closest(side, target):
        """Side='C' or 'P'. Returns (strike, option) closest to target."""
        cands = [(k, v[side]) for k, v in chain_by_strike.items() if side in v]
        if not cands:
            return None, None
        return min(cands, key=lambda kv: abs(kv[0] - target))

    dn_k, put_otm = find_closest("P", down_target)
    up_k, call_otm = find_closest("C", up_target)

    put_otm_px, put_otm_src = last_or_mid(put_otm) if put_otm else (0, "none")
    call_otm_px, call_otm_src = last_or_mid(call_otm) if call_otm else (0, "none")

    yield_otm_put  = (put_otm_px  / dn_k * 100) if (dn_k and put_otm_px > 0) else None
    yield_otm_call = (call_otm_px / up_k * 100) if (up_k and call_otm_px > 0) else None
    # Headline yield: the larger of the two tails (the "fattest" side)
    yields = [y for y in (yield_otm_put, yield_otm_call) if y is not None]
    yield_pct = max(yields) if yields else None
    # And an averaged version (cleaner cross-section)
    yield_avg = (sum(yields) / len(yields)) if yields else None

    return {
        "sym": sym,
        "name": NAMES.get(sym, sym),
        "mcap": live_mcap_b(sym, spot),
        "spot": round(spot, 2),
        "expiry": best_exp,
        "dte": dte,
        "iv30": round(d.get("iv30") or 0, 2),
        # ATM straddle (used to compute the move)
        "atm_strike": atm_k,
        "call_atm_last": round(call_px, 3),
        "put_atm_last": round(put_px, 3),
        "straddle": round(straddle, 3),
        "parity_skew_pct": round(parity_skew_pct, 3),
        "stale_atm": bool(stale_atm),
        # The actual signal — OTM tail options at spot ± straddle
        "dn_strike": round(dn_k, 2) if dn_k is not None else None,
        "up_strike": round(up_k, 2) if up_k is not None else None,
        "put_otm_last": round(put_otm_px, 3) if put_otm_px else None,
        "call_otm_last": round(call_otm_px, 3) if call_otm_px else None,
        "yield_otm_put":  round(yield_otm_put, 3)  if yield_otm_put  is not None else None,
        "yield_otm_call": round(yield_otm_call, 3) if yield_otm_call is not None else None,
        # headline = max of the two tails; avg = clean cross-section
        "yield_pct": round(yield_pct, 3) if yield_pct is not None else None,
        "yield_avg": round(yield_avg, 3) if yield_avg is not None else None,
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

    srt = sorted(rows, key=lambda r: -(r.get("yield_avg") or r.get("yield_pct") or 0))
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
