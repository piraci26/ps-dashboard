# P/S Dashboard

Cross-sectional ranking of how richly priced near-week options are across the top-50 US equities.

**P/S = option mid ÷ spot**, evaluated at 30Δ for the next-Friday weekly expiry.

At matched delta and DTE, P/S is proportional to **IV·√T** — so it's a clean way to spot names where you collect the most premium per dollar of underlying exposure (i.e. richly-priced names to sell).

## Run

```sh
python3 scan.py    # writes docs/results.json
```

Open `docs/index.html` locally, or push to GitHub Pages.

## Cadence

Designed to be run weekly **after Friday's close** to lock EOD pricing for the next-Friday expiry. CBOE updates throughout the day so re-running on weekdays just refreshes the snapshot for whichever expiry is closest to next Friday.

## Data

CBOE delayed quotes endpoint:
`https://cdn.cboe.com/api/global/delayed_quotes/options/{SYM}.json`

~15-minute delay, free, no auth. Provides full chain incl. delta/IV/bid/ask/OI per contract, plus underlying spot, IV30, and price-change for each ticker.
