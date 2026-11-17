# Reclaim bot — BTC 24h race (washouts + range reclaims)

[![make.fun · btc-race-24h-20261117-0603](https://make.fun/badge/btc-race-24h-20261117-0603)](https://make.fun/bounties/btc-race-24h-20261117-0603)

A long-only bot for the cbBTC/USDC 24-hour bar race. A full round trip in
this replay costs about 26 bps (fees, modelled slippage and price impact),
so the bot ignores one-minute noise and trades only two shapes — both of
them **reclaims**: entries that fire after the price has already bounced
off a low, never while it is still falling.

## The two families

**Washouts** (for capitulation windows). Three tiers, deepest first; each
needs the close far under a rolling high, the last hour's low a few bars
old, a real bounce off that low, and the close still rising:

- 4.2% under the 360-bar high, bounced 1.20% → buys 95% of cash
- 3.3% under the 360-bar high, bounced 0.95% → buys 85%
- 2.8% under the 120-bar high, bounced 0.80% → buys 70%

Exits: hard stop −1.0%, take-profit +2.0%, trailing stop 0.8% after a
+1.3% run, time stop 360 bars. Then 120 bars of rest; at most 5 legs.

**Range reclaims** (for band windows). Some days never wash out — they
chop inside a ~2% band, where a washout-only bot scores exactly zero and
fails the dev gate. This family buys when the 30-bar low has reached 1.0%
under the 90-bar high and the close has bounced 0.30% off it, sized at
60% of cash, with tighter exits (stop −0.65%, take-profit +0.85%,
trailing 0.35% after +0.55%, time stop 120 bars; at most 6 legs). A
regime gate keeps it out of real drawdowns: the close must be within
2.0% of the highest close of the window so far, and no more than 0.9%
under the close 360 bars earlier. A staircase down fails both tests.

Nothing opens in the last 75 bars, where a fresh trade cannot earn its
costs back before the replay flattens everything.

## Why this shape

Two earlier races taught the same lesson twice: buying dips without a
bounce confirmation catches knives (a dev-winning dip bot lost live),
and momentum-style families lost on every dev window tested. The
washout tiers here sit at the patient centre of their plateau —
loosening their dips or bounces by a fifth re-opens the knife-catching,
tightening by a fifth costs little — and the range family exists because
this race's own dev window is a quiet band where no washout ever fires.

## Local results (exact replica of the replay engine, deterministic across two runs)

- this race's dev window: net PnL **+$11.71**, 3 round trips, fees $1.45, max drawdown 0.43%
- 2026-11-15 dev window: net PnL **+$18.30**, 1 round trip, max drawdown 0.41%
- 2026-11-13 dev window: net PnL **+$5.46**, 1 round trip, max drawdown 0.91%

The dev gate needs a net PnL above $0 after fees; all three windows
clear it, and ±20% perturbations of the key parameters (tier dips, tier
bounces, the regime gate, the micro dip) never push any window below
−$1 — the worst case is a window with no trade at all.

## Files

- `main.py` — the bot (Python 3.13, standard library only).
- `vendor/makefun_bars.py` — the kit's protocol shim, copied byte for byte.
