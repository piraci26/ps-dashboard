# P/S Dashboard — ATM Straddle Implied Move

Cross-sectional ranking of the **expected % move** the market is pricing for each name by Friday.

```
straddle      = call_last + put_last        (ATM strike, both legs)
implied_move  = straddle / strike           (≈ % return by expiry)
breakevens    = spot ± straddle
```

Higher implied_move = market expects bigger move = richer premium per dollar of strike. That's where the cross-sectional opportunity is to sell premium (or to fade rich vol). Top of the list = richest; bottom = cheapest.

### Caveat: stale last-prices
When a leg's last trade is from earlier in the day, put-call parity (C − P ≈ S − K) breaks and the straddle skews high. Best run after Friday close so both legs have fresh prints.

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
