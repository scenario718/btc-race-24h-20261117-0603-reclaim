"""Reclaim bot for agent-fun/bars-replay/v1: washouts and range reclaims.

One asset, CBBTC against USDC, long only, 1,000 USDC of paper money. A full
round trip in this replay costs roughly 26 bps (4 bps fee, 2 bps modelled
slippage, the rest price impact), so one-minute noise cannot pay for
itself. The bot trades two shapes, both only after the price has started
to reclaim ground from a low — buying a dip that is still falling is how
this strategy family loses, so the reclaim test guards every entry:

1. Washouts. The close sits far under a rolling high (a capitulation),
   the lowest close of the last hour is a few bars old, the close has
   bounced a set margin off that low, is still rising against a few bars
   ago, and the signal bar's own range is not extreme. Three tiers,
   deepest first: 3.8% under the 360-bar high with a 0.90% bounce buys
   95% of cash; 3.0% under it with a 0.70% bounce buys 85%; 2.5% under
   the 120-bar high with a 0.60% bounce buys 70%. Exits: hard stop 1.0%
   under the entry, take-profit 2.0% above it, a trailing stop 0.8%
   under the best close once the trade has run 1.3% into profit, or a
   time stop after 360 bars; then a 120-bar rest, at most 5 legs.

2. Range reclaims. Some windows never wash out: they chop inside a band
   a couple of percent wide for a day, and a washout-only bot scores
   exactly zero there, which fails the dev gate. So a second, smaller
   family trades the band: when the 30-bar low has reached 1.0% under
   the 90-bar high, the close is still at least 0.4% under that high,
   and the close has bounced 0.30% off the low, it buys 60% of cash —
   but only in a true range regime: the close must sit within 2.0% of
   the highest close of the window so far and no more than 0.9% under
   the close 360 bars earlier. A staircase down fails both tests. In a
   real drawdown the
   band family stands aside and lets the washout tiers judge. Exits are
   tighter: stop 0.65%, take-profit 0.85%, trailing 0.35% after a 0.55%
   run, time stop 120 bars; then a 45-bar rest, at most 6 legs.

Nothing opens in the last 75 bars of the window, where a fresh trade
could not earn its costs back before the replay flattens everything.
State lives on the SDK's BarsState object (a fresh one per execution),
so two executions over the same bars answer identically. Standard
library only; no network, no wall clock, no randomness.
"""

import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
BPS = 10000
MIN_NOTIONAL = 1_000_000

PARAMS = {
    "total_bars": 1440,        # scored bars in a window
    "tail_bars": 75,           # no new entries this close to the end
    "min_history": 130,        # closes needed before the first entry
    "long_lb": 360,            # rolling-high lookback for the deep tiers
    "short_lb": 120,           # rolling-high lookback for the mild tier
    "low_lb": 60,              # recent-low lookback (washout bounce ref)
    "order_slip_bps": 250,
    "use_washout": True,
    "use_micro": True,
    # washout family
    "w_low_min_age": 3,        # the low must be at least this many bars old
    "w_rise_lag": 3,           # close must beat the close this far back
    "w_range_max_bps": 300,    # skip signal bars wider than this
    # (dip_bps, lookback key, bounce_bps, size_bps of cash), deepest first
    "w_tiers": (
        (380, "long_lb", 90, 9500),
        (300, "long_lb", 70, 8500),
        (250, "short_lb", 60, 7000),
    ),
    "w_tp_bps": 200,
    "w_stop_bps": 100,
    "w_trail_arm_bps": 130,
    "w_trail_bps": 80,
    "w_time_stop_bars": 360,
    "w_cooldown_bars": 120,
    "w_max_legs": 5,
    # range-reclaim (micro) family
    "m_lb": 90,                # rolling-high lookback
    "m_low_lb": 30,            # recent-low lookback
    "m_dip_bps": 100,          # the 30-bar low reached this far under the high
    "m_close_dip_bps": 40,     # and the close is still at least this far under
    "m_bounce_bps": 30,
    "m_low_min_age": 2,
    "m_rise_lag": 2,
    "m_range_max_bps": 200,
    "m_size_bps": 6000,
    "m_drift_lb": 360,             # regime drift lookback
    "m_drift_min_bps": -90,        # close vs that bar: no real downward drift
    "m_exp_high_max_bps": 200,     # close within this of the all-window high
    "m_tp_bps": 85,
    "m_stop_bps": 65,
    "m_trail_arm_bps": 55,
    "m_trail_bps": 35,
    "m_time_stop_bars": 120,
    "m_cooldown_bars": 45,
    "m_max_legs": 6,
    "reject_cooldown_bars": 5,
}

# per-family exit/rest knobs, keyed by family name
_FAMILY_KEYS = ("tp_bps", "stop_bps", "trail_arm_bps", "trail_bps",
                "time_stop_bars", "cooldown_bars")


class WashoutBot:
    """The strategy as a pure bar-by-bar state machine.

    on_bar() is fed the scored bar index, the bar's close/high/low, and
    the account (cash, position qty, position cost in quote units) as the
    harness reports them, and returns SDK-shaped order dicts. Position
    state is taken from the account every bar, never assumed: an order
    decided on bar k fills at bar k+1's open, and bar k+1's account is
    the first place the fill shows.
    """

    def __init__(self, params=None):
        p = dict(PARAMS)
        if params:
            p.update(params)
        self.p = p
        self.closes = []
        self.entry_px = None      # Fraction, raw price units
        self.entry_seq = -1
        self.best_close = 0
        self.family = None        # family of the open leg
        self.pending_family = None
        self.closed_family = None
        self.legs = {"w": 0, "m": 0}
        self.cooldown_until = -1
        self.awaiting_fill = False

    # -- helpers ---------------------------------------------------------
    def _high(self, lookback):
        return max(self.closes[-lookback:])

    def _low_and_age(self, lookback):
        window = self.closes[-lookback:]
        low = min(window)
        idx = len(window) - 1 - window[::-1].index(low)
        return low, len(window) - 1 - idx

    def _turn_ok(self, close, low_lb, min_age, rise_lag, bounce_bps):
        low, age = self._low_and_age(low_lb)
        if age < min_age:
            return False
        if close * BPS < low * (BPS + bounce_bps):
            return False
        if close <= self.closes[-1 - rise_lag]:
            return False
        return True

    def _washout_signal(self, close, high, low):
        p = self.p
        if (high - low) * BPS > close * p["w_range_max_bps"]:
            return 0
        for dip_bps, lb_key, bounce_bps, size_bps in p["w_tiers"]:
            if close * BPS > self._high(p[lb_key]) * (BPS - dip_bps):
                continue
            if self._turn_ok(close, p["low_lb"], p["w_low_min_age"],
                             p["w_rise_lag"], bounce_bps):
                return size_bps
        return 0

    def _micro_signal(self, close, high, low):
        p = self.p
        if (high - low) * BPS > close * p["m_range_max_bps"]:
            return 0
        # regime gate: a range has no real downward drift and its dips
        # stay near the window's high; a staircase down fails both tests
        ref = self.closes[max(0, len(self.closes) - 1 - p["m_drift_lb"])]
        if close * BPS < ref * (BPS + p["m_drift_min_bps"]):
            return 0
        if close * BPS < max(self.closes) * (BPS - p["m_exp_high_max_bps"]):
            return 0
        high_ref = self._high(p["m_lb"])
        low, _age = self._low_and_age(p["m_low_lb"])
        if low * BPS > high_ref * (BPS - p["m_dip_bps"]):
            return 0  # the dip never reached reclaim depth
        if close * BPS > high_ref * (BPS - p["m_close_dip_bps"]):
            return 0  # already back at the high: nothing left to reclaim
        if not self._turn_ok(close, p["m_low_lb"], p["m_low_min_age"],
                             p["m_rise_lag"], p["m_bounce_bps"]):
            return 0
        return p["m_size_bps"]

    def _exit_hit(self, seq, close):
        p = self.p
        fam = self.family or "w"
        tp = p[f"{fam}_tp_bps"]
        stop = p[f"{fam}_stop_bps"]
        arm = p[f"{fam}_trail_arm_bps"]
        trail = p[f"{fam}_trail_bps"]
        entry = self.entry_px
        if close * BPS <= entry * (BPS - stop):
            return True
        if close * BPS >= entry * (BPS + tp):
            return True
        if self.best_close * BPS >= entry * (BPS + arm):
            if close * BPS <= self.best_close * (BPS - trail):
                return True
        if seq - self.entry_seq >= p[f"{fam}_time_stop_bars"]:
            return True
        return False

    # -- main step -------------------------------------------------------
    def on_bar(self, seq, close, high, low, cash, pos_qty, pos_cost):
        p = self.p
        self.closes.append(close)

        if pos_qty > 0:
            if self.entry_px is None:
                # first sight of the position: the account's cost is the
                # fill truth (fees included), so the entry price is too
                self.entry_px = Fraction(pos_cost * 10**7, pos_qty)
                self.entry_seq = seq
                self.best_close = close
                self.family = self.pending_family or "w"
                self.legs[self.family] += 1
                self.awaiting_fill = False
                self.pending_family = None
            if close > self.best_close:
                self.best_close = close
            if self._exit_hit(seq, close):
                self.closed_family = self.family
                return [{"side": "sell", "symbol": SYMBOL, "qty": pos_qty,
                         "max_slippage_bps": p["order_slip_bps"]}]
            return []

        # flat
        if self.entry_px is not None:
            # the position closed since the last bar (our exit or the
            # replay's flatten): start the family's rest
            fam = self.closed_family or self.family or "w"
            self.cooldown_until = max(self.cooldown_until,
                                      seq + p[f"{fam}_cooldown_bars"])
            self.entry_px = None
            self.family = None
            self.closed_family = None
            self.best_close = 0
        if self.awaiting_fill:
            # the buy decided last bar never landed (rejected): short rest
            self.awaiting_fill = False
            self.pending_family = None
            self.cooldown_until = max(self.cooldown_until,
                                      seq + p["reject_cooldown_bars"])
        if seq < self.cooldown_until:
            return []
        if len(self.closes) < p["min_history"]:
            return []
        if seq >= p["total_bars"] - p["tail_bars"]:
            return []

        size_bps = 0
        family = None
        if p["use_washout"] and self.legs["w"] < p["w_max_legs"]:
            size_bps = self._washout_signal(close, high, low)
            if size_bps:
                family = "w"
        if not size_bps and p["use_micro"] and self.legs["m"] < p["m_max_legs"]:
            size_bps = self._micro_signal(close, high, low)
            if size_bps:
                family = "m"
        if not size_bps:
            return []
        notional = cash * size_bps // BPS
        if notional < MIN_NOTIONAL:
            return []
        self.awaiting_fill = True
        self.pending_family = family
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": notional,
                 "max_slippage_bps": p["order_slip_bps"]}]


def on_batch(state, batch):
    bot = getattr(state, "_washout_bot", None)
    if bot is None:
        bot = WashoutBot()
        state._washout_bot = bot
    bar = None
    for b in batch["bars"]:
        if b["symbol"] == SYMBOL:
            bar = b
            break
    if bar is None:
        return []
    held = state.positions.get(SYMBOL)
    qty = held.qty if held else 0
    cost = held.cost if held else 0
    scored_seq = state.seq - state.warmup_bars
    return bot.on_bar(scored_seq, int(bar["c"]), int(bar["h"]), int(bar["l"]),
                      state.cash, qty, cost)


if __name__ == "__main__":
    makefun_bars.run(on_batch)
