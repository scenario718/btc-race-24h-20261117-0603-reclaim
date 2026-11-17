"""Reclaim-after-washout bot for agent-fun/bars-replay/v1.

One asset, CBBTC against USDC, long only, 1,000 USDC of paper money. A full
round trip in this replay costs roughly 26 bps (4 bps fee, 2 bps modelled
slippage, the rest price impact), so one-minute noise cannot pay for itself:
the bot only trades capitulation washouts, and only after the price has
started to reclaim ground from its low. Buying a dip that is still falling
is how this strategy family loses; the reclaim test is what keeps it out of
the waterfall.

Entry, all conditions on closes, deepest tier first:
- the latest close sits far enough under a rolling high (a washout),
- the lowest close of the last hour is at least a few bars old (the fall
  has stopped making new lows),
- the latest close has bounced a set margin off that low (the turn),
- the latest close is higher than the close a few bars earlier (the turn
  is still in progress, not a single stray print),
- the signal bar's own range is not extreme (no mid-crash bars).
Tiers: 3.5% under the 360-bar high with a 0.80% bounce buys 95% of cash;
2.6% under the 360-bar high with a 0.60% bounce buys 85%; 2.0% under the
120-bar high with a 0.50% bounce buys 70%.

Exits, whichever fires first: a hard stop 1.1% under the entry, a
take-profit 1.8% above it, a trailing stop 0.7% under the best close once
the trade has run 1.2% into profit, or a time stop after 300 bars. After
an exit the bot rests 90 bars; at most 5 legs per window; nothing opens in
the last 75 bars, where a fresh trade could not earn its costs back before
the replay flattens everything at the end.

State lives on the SDK's BarsState object (a fresh one per execution), so
two executions over the same bars answer identically. Standard library
only; no network, no wall clock, no randomness.
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
    "low_lb": 60,              # recent-low lookback (the bounce reference)
    "low_min_age": 3,          # the low must be at least this many bars old
    "rise_lag": 3,             # close must beat the close this many bars back
    "range_max_bps": 300,      # skip signal bars wider than this
    # (dip_bps, lookback key, bounce_bps, size_bps of cash), deepest first
    "tiers": (
        (350, "long_lb", 80, 9500),
        (260, "long_lb", 60, 8500),
        (200, "short_lb", 50, 7000),
    ),
    "tp_bps": 180,
    "stop_bps": 110,
    "trail_arm_bps": 120,
    "trail_bps": 70,
    "time_stop_bars": 300,
    "cooldown_bars": 90,
    "reject_cooldown_bars": 5,
    "max_legs": 5,
    "order_slip_bps": 250,
}


class WashoutBot:
    """The strategy as a pure bar-by-bar state machine.

    on_bar() is fed the scored bar index, the bar's close/high/low, and the
    account (cash, position qty, position cost in quote units) as the
    harness reports them, and returns SDK-shaped order dicts. Position
    state is taken from the account every bar, never assumed: an order
    decided on bar k fills at bar k+1's open, and bar k+1's account is the
    first place the fill shows.
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
        self.legs = 0
        self.cooldown_until = -1
        self.awaiting_fill = False

    # -- helpers ---------------------------------------------------------
    def _rolling_high(self, lookback):
        window = self.closes[-lookback:]
        return max(window)

    def _recent_low(self):
        window = self.closes[-self.p["low_lb"]:]
        low = min(window)
        # age of the most recent bar that printed this low
        idx = len(window) - 1 - window[::-1].index(low)
        age = len(window) - 1 - idx
        return low, age

    def _entry_signal(self, close, high, low):
        p = self.p
        if len(self.closes) < p["min_history"]:
            return 0
        if (high - low) * BPS > close * p["range_max_bps"]:
            return 0
        recent_low, low_age = self._recent_low()
        if low_age < p["low_min_age"]:
            return 0
        if close <= self.closes[-1 - p["rise_lag"]]:
            return 0
        for dip_bps, lb_key, bounce_bps, size_bps in p["tiers"]:
            high_ref = self._rolling_high(p[lb_key])
            if close * BPS > high_ref * (BPS - dip_bps):
                continue
            if close * BPS < recent_low * (BPS + bounce_bps):
                continue
            return size_bps
        return 0

    def _exit_reason(self, seq, close):
        p = self.p
        entry = self.entry_px
        if close * BPS <= entry * (BPS - p["stop_bps"]):
            return "stop"
        if close * BPS >= entry * (BPS + p["tp_bps"]):
            return "take_profit"
        if self.best_close * BPS >= entry * (BPS + p["trail_arm_bps"]):
            if close * BPS <= self.best_close * (BPS - p["trail_bps"]):
                return "trailing_stop"
        if seq - self.entry_seq >= p["time_stop_bars"]:
            return "time_stop"
        return None

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
                self.legs += 1
                self.awaiting_fill = False
            if close > self.best_close:
                self.best_close = close
            if self._exit_reason(seq, close) is not None:
                return [{"side": "sell", "symbol": SYMBOL, "qty": pos_qty,
                         "max_slippage_bps": p["order_slip_bps"]}]
            return []

        # flat
        if self.entry_px is not None:
            # the position closed since the last bar (our exit or the
            # replay's flatten): start the rest
            self.entry_px = None
            self.best_close = 0
            self.cooldown_until = max(self.cooldown_until,
                                      seq + p["cooldown_bars"])
        if self.awaiting_fill:
            # the buy decided last bar never landed (rejected): short rest
            self.awaiting_fill = False
            self.cooldown_until = max(self.cooldown_until,
                                      seq + p["reject_cooldown_bars"])
        if seq < self.cooldown_until:
            return []
        if self.legs >= p["max_legs"]:
            return []
        if seq >= p["total_bars"] - p["tail_bars"]:
            return []
        size_bps = self._entry_signal(close, high, low)
        if size_bps == 0:
            return []
        notional = cash * size_bps // BPS
        if notional < MIN_NOTIONAL:
            return []
        self.awaiting_fill = True
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
