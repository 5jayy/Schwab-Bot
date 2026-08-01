"""
EXIT BACKTEST — tests stop/profit-exit rules on real price history.

Your losses come from EXITS, not entries: positions like PLUG (-22%) and
OPEN (-21%) blew past the 7% trailing stop. Why? Your live stop is a TRAILING
stop that only engages AFTER the position is up (breakeven locks at +2%, then
trails 7% off the high). If a stock drops straight from entry and never goes
green, the trailing logic never activates — so it can fall 20%+ before anything
cuts it. That's the leak.

This backtest replays real candles and compares exit rules:
  A) LIVE   — trailing 7% off high, breakeven at +2% (your current logic)
  B) HARD   — hard stop at entry (e.g. -7% from buy, always active)
  C) COMBO  — hard stop from entry AND trailing once green

Entry is NOT modeled here (entry logic is live-only / MTF+order-flow). Instead
we assume entry at each candle and measure how each EXIT rule performs on the
forward price path. This isolates the exit question: which rule caps the big
losers without choking winners?

Run:  python3 backtest_exits.py NVDA AAPL PLUG   (or no args = default set)
"""

import sys
import time
import requests
from datetime import datetime, timezone

from auth import get_valid_token

BASE_URL = "https://api.schwabapi.com/marketdata/v1"


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_candles(symbol: str, period: int = 10, frequency: int = 30) -> list:
    """Real 30-min candles (matches live scanner: period=10, 30-min)."""
    try:
        resp = requests.get(
            f"{BASE_URL}/pricehistory",
            headers=headers(),
            params={"symbol": symbol, "periodType": "day", "period": period,
                    "frequencyType": "minute", "frequency": frequency,
                    "needExtendedHoursData": False},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("candles", [])
    except Exception as ex:
        print(f"  {symbol}: fetch error {ex}")
        return []


# ---- Exit rules ----
# Each takes the entry price and the forward candles, returns (exit_price, reason).

def exit_live_trailing(entry, fwd, base_trail=0.07, be_at=0.02):
    """LIVE logic: breakeven lock at +2%, then trail base_trail off the high.
    No hard stop before green — this is the current bot behavior."""
    high = entry
    stop = None  # not active until we go green
    for c in fwd:
        px = c["close"]
        high = max(high, px)
        profit = (px - entry) / entry
        if profit >= be_at:
            # breakeven+ active: stop = max(breakeven, high*(1-trail))
            stop = max(entry, high * (1 - base_trail))
        if stop is not None and px <= stop:
            return px, "trail_stop"
    return fwd[-1]["close"], "expiry"  # held to end of data


def exit_hard_stop(entry, fwd, hard=0.07, target=0.10):
    """HARD stop at -hard from entry (always active), take profit at +target."""
    stop_px = entry * (1 - hard)
    tgt_px  = entry * (1 + target)
    for c in fwd:
        lo = c.get("low", c["close"])
        hi = c.get("high", c["close"])
        if lo <= stop_px:
            return stop_px, "hard_stop"
        if hi >= tgt_px:
            return tgt_px, "target"
    return fwd[-1]["close"], "expiry"


def exit_combo(entry, fwd, hard=0.07, base_trail=0.07, be_at=0.02, target=0.12):
    """COMBO: hard stop from entry (catches instant drops) + trailing once green."""
    high = entry
    hard_px = entry * (1 - hard)
    tgt_px  = entry * (1 + target)
    for c in fwd:
        px = c["close"]
        lo = c.get("low", px)
        hi = c.get("high", px)
        high = max(high, hi)
        if lo <= hard_px:
            return hard_px, "hard_stop"
        if hi >= tgt_px:
            return tgt_px, "target"
        profit = (px - entry) / entry
        if profit >= be_at:
            trail_px = high * (1 - base_trail)
            if px <= trail_px:
                return px, "trail_stop"
    return fwd[-1]["close"], "expiry"


def backtest_symbol(symbol: str, hold_bars: int = 12):
    candles = get_candles(symbol)
    if len(candles) < 40:
        return None

    rules = {"LIVE_trail": exit_live_trailing,
             "HARD_stop":  exit_hard_stop,
             "COMBO":      exit_combo}
    results = {k: [] for k in rules}

    # Simulate entering every ~4 bars, hold up to hold_bars forward
    for i in range(0, len(candles) - hold_bars - 1, 4):
        entry = candles[i]["close"]
        fwd   = candles[i+1 : i+1+hold_bars]
        if entry <= 0 or not fwd:
            continue
        for name, fn in rules.items():
            exit_px, _ = fn(entry, fwd)
            results[name].append((exit_px - entry) / entry * 100)  # % return
    return results


def run(symbols):
    print(f"\n{'='*58}")
    print("EXIT BACKTEST — which stop rule caps the big losers?")
    print(f"{'='*58}")
    print("Entry assumed each bar; measures EXIT rule on forward path.")
    print("LIVE = your current trailing (no hard stop until green).\n")

    agg = {"LIVE_trail": [], "HARD_stop": [], "COMBO": []}
    for sym in symbols:
        r = backtest_symbol(sym)
        if r:
            for k in agg:
                agg[k].extend(r[k])
            print(f"  tested {sym}")
        time.sleep(0.3)

    print(f"\n{'-'*58}")
    print(f"{'RULE':<14}{'trades':>8}{'win%':>8}{'avg':>9}{'worst':>9}{'total':>10}")
    print(f"{'-'*58}")
    for name, rets in agg.items():
        if not rets:
            continue
        n      = len(rets)
        wins   = [x for x in rets if x > 0]
        winpct = len(wins) / n * 100
        avg    = sum(rets) / n
        worst  = min(rets)
        total  = sum(rets)
        print(f"{name:<14}{n:>8}{winpct:>7.0f}%{avg:>8.2f}%{worst:>8.1f}%{total:>9.1f}%")

    print(f"\n{'-'*58}")
    print("READ: 'worst' is the single biggest loss the rule allowed.")
    print("Your LIVE rule's worst should be a big negative (the leak).")
    print("If HARD or COMBO has a much smaller 'worst' AND similar/better")
    print("total, that's the fix — a hard stop from entry caps disasters.")
    print("NOTE: ~10 days of 30-min data = directional, not exact dollars.")
    print(f"{'='*58}\n")


if __name__ == "__main__":
    syms = sys.argv[1:] if len(sys.argv) > 1 else \
        ["NVDA", "AAPL", "PLUG", "RIOT", "SOFI", "F", "INTC", "AMD", "HOOD", "MARA"]
    run(syms)
