"""
PULLBACK STRATEGY BACKTEST — the real entry fix.

Entry (long; short mirrors):
  • Price above EMA200 AND EMA200 rising (direction filter)
  • Pullback: price dips toward EMA9/EMA21
  • Reclaim: a bullish candle closes back above EMA9
  • Enter above that candle's high (stop-entry trigger)
  → NOT every EMA200 touch. EMA200 = direction, fast EMAs = entry.
    This catches the RESUMPTION of a move, not random drift (fixes timeout).

Risk (ATR-based, R-multiples):
  • Stop = N_ATR * ATR below entry (tested: 1.0, 1.5)
  • Partial at +1R, move stop to breakeven
  • Final target +1.5R or +2R (tested)

Trade management (bar counts scale by timeframe):
  • Entry order expires if not triggered within EXPIRY bars
  • No-progress timeout, absolute max hold
  • Daily 2-loss shutdown

Tests BOTH 5m and 15m timeframes, and several ATR/R combos. NET of slippage.

Run locally:  python3 backtest_pullback.py
"""

import sys
import time
import requests
from datetime import datetime, timezone
from auth import get_valid_token

BASE_URL = "https://api.schwabapi.com/marketdata/v1"
SLIPPAGE_PCT = 0.05
ROUND_TRIP_COST = SLIPPAGE_PCT * 2

# Timeframe configs: (label, freq, expiry_bars, noprogress_bars, max_bars)
TIMEFRAMES = [
    ("5m",  5,  3, 6, 12),
    ("15m", 15, 3, 5, 8),
]

# Risk configs: (label, n_atr, partial_R, final_R)
RISK_CONFIGS = [
    ("1.0ATR 1R+2R",   1.0, 1.0, 2.0),
    ("1.5ATR 1R+1.5R", 1.5, 1.0, 1.5),
    ("1.0ATR 1R+1.5R", 1.0, 1.0, 1.5),
    ("1.5ATR 1R+2R",   1.5, 1.0, 2.0),
]

DAILY_MAX_LOSSES = 2


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def fetch_daily_year(symbol):
    try:
        r = requests.get(f"{BASE_URL}/pricehistory", headers=headers(),
                         params={"symbol": symbol, "periodType": "year", "period": 1,
                                 "frequencyType": "daily", "frequency": 1,
                                 "needExtendedHoursData": False}, timeout=15)
        r.raise_for_status()
        return r.json().get("candles", [])
    except Exception:
        return []


def fetch_intraday(symbol, freq):
    try:
        r = requests.get(f"{BASE_URL}/pricehistory", headers=headers(),
                         params={"symbol": symbol, "periodType": "day", "period": 10,
                                 "frequencyType": "minute", "frequency": freq,
                                 "needExtendedHoursData": False}, timeout=15)
        r.raise_for_status()
        return r.json().get("candles", [])
    except Exception:
        return []


def ema_series(values, length):
    if len(values) < length:
        return [None] * len(values)
    k = 2 / (length + 1)
    out = [None] * (length - 1)
    e = sum(values[:length]) / length
    out.append(e)
    for v in values[length:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def atr_at(candles, idx, period=14):
    if idx < period:
        return None
    trs = []
    for i in range(idx - period + 1, idx + 1):
        h = candles[i]["high"]; l = candles[i]["low"]; pc = candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / period


def daily_trend(daily):
    """EMA200 direction: returns ('bull'/'bear'/'flat', rising_bool)."""
    closes = [c["close"] for c in daily]
    es = ema_series(closes, 200)
    if es[-1] is None:
        es = ema_series(closes, 50)
        if es[-1] is None:
            return "flat", False
    price = closes[-1]; e = es[-1]
    # slope: EMA now vs 10 bars ago
    e_prev = es[-11] if len(es) >= 11 and es[-11] is not None else e
    rising = e > e_prev
    if price > e and rising:
        return "bull", rising
    if price < e and not rising:
        return "bear", rising
    return "flat", rising


def find_entries(candles, direction):
    """
    Find pullback-reclaim entries. Returns list of (entry_idx, entry_price, stop_ref_low_or_high).
    Long: price pulled toward EMA9/21, then a bullish candle closes back above EMA9,
    entry = break above that candle's high.
    """
    closes = [c["close"] for c in candles]
    ema9  = ema_series(closes, 9)
    ema21 = ema_series(closes, 21)
    signals = []
    for i in range(25, len(candles) - 1):
        e9 = ema9[i]; e21 = ema21[i]
        if e9 is None or e21 is None:
            continue
        c = candles[i]; prev = candles[i-1]
        if direction == "bull":
            # pullback: recent low touched near/below ema9 or ema21
            pulled = prev["low"] <= max(e9, e21) * 1.001
            reclaim = c["close"] > e9 and c["close"] > c["open"]  # bullish close above ema9
            if pulled and reclaim:
                signals.append((i, c["high"], c["low"]))  # enter above high, stop ref = low
        else:
            pulled = prev["high"] >= min(e9, e21) * 0.999
            reclaim = c["close"] < e9 and c["close"] < c["open"]
            if pulled and reclaim:
                signals.append((i, c["low"], c["high"]))
    return signals


def simulate_trade(candles, sig_idx, trigger_px, stop_ref, direction,
                   n_atr, partial_R, final_R, expiry, noprog, maxbar):
    """
    Execute one pullback trade with ATR risk, partial, breakeven, timeouts.
    Returns (net_pct, outcome) or None if entry never triggered.
    """
    atr = atr_at(candles, sig_idx)
    if not atr or atr <= 0:
        return None

    # 1) Wait for entry trigger (break of trigger_px) within `expiry` bars
    entry_idx = None
    for j in range(sig_idx + 1, min(sig_idx + 1 + expiry, len(candles))):
        if direction == "bull" and candles[j]["high"] >= trigger_px:
            entry_idx = j; entry_px = trigger_px; break
        if direction == "bear" and candles[j]["low"] <= trigger_px:
            entry_idx = j; entry_px = trigger_px; break
    if entry_idx is None:
        return None  # order expired, never filled

    # 2) Set stop/targets in R terms
    risk = n_atr * atr
    if direction == "bull":
        stop = entry_px - risk
        partial_px = entry_px + partial_R * risk
        final_px   = entry_px + final_R * risk
    else:
        stop = entry_px + risk
        partial_px = entry_px - partial_R * risk
        final_px   = entry_px - final_R * risk

    remaining = 1.0
    realized = 0.0
    be_moved = False
    best = entry_px
    bars_since_progress = 0

    for k in range(entry_idx + 1, min(entry_idx + 1 + maxbar, len(candles))):
        c = candles[k]
        hi = c["high"]; lo = c["low"]; px = c["close"]

        # stop check
        if direction == "bull" and lo <= stop:
            realized += remaining * (stop - entry_px) / entry_px * 100
            return realized - ROUND_TRIP_COST, ("breakeven_stop" if be_moved else "stop")
        if direction == "bear" and hi >= stop:
            realized += remaining * (entry_px - stop) / entry_px * 100
            return realized - ROUND_TRIP_COST, ("breakeven_stop" if be_moved else "stop")

        # partial at +1R → take half, move stop to breakeven
        if not be_moved:
            hit_partial = (hi >= partial_px) if direction == "bull" else (lo <= partial_px)
            if hit_partial:
                realized += 0.5 * (partial_R * risk) / entry_px * 100
                remaining = 0.5
                stop = entry_px  # breakeven
                be_moved = True

        # final target
        hit_final = (hi >= final_px) if direction == "bull" else (lo <= final_px)
        if hit_final:
            realized += remaining * (final_R * risk) / entry_px * 100
            return realized - ROUND_TRIP_COST, "target"

        # progress tracking for no-progress timeout
        prog = (hi > best) if direction == "bull" else (lo < best)
        if prog:
            best = hi if direction == "bull" else lo
            bars_since_progress = 0
        else:
            bars_since_progress += 1
        if bars_since_progress >= noprog:
            exit_px = px
            r = ((exit_px - entry_px) if direction == "bull" else (entry_px - exit_px)) / entry_px * 100
            realized += remaining * r
            return realized - ROUND_TRIP_COST, "noprogress"

    # absolute max hold
    last = candles[min(entry_idx + maxbar, len(candles) - 1)]["close"]
    r = ((last - entry_px) if direction == "bull" else (entry_px - last)) / entry_px * 100
    realized += remaining * r
    return realized - ROUND_TRIP_COST, "maxbar"


def backtest(symbols, tf_label, freq, expiry, noprog, maxbar, n_atr, partial_R, final_R):
    all_trades = []
    for sym in symbols:
        daily = fetch_daily_year(sym)
        if len(daily) < 60:
            continue
        direction, rising = daily_trend(daily)
        if direction == "flat":
            continue
        candles = fetch_intraday(sym, freq)
        if len(candles) < 60:
            continue
        sigs = find_entries(candles, direction)
        for (idx, trig, ref) in sigs:
            res = simulate_trade(candles, idx, trig, ref, direction,
                                 n_atr, partial_R, final_R, expiry, noprog, maxbar)
            if res:
                all_trades.append(res)
        time.sleep(0.25)
    return all_trades


def run(symbols):
    print(f"\n{'='*70}")
    print("PULLBACK STRATEGY — EMA200 dir + EMA9/21 pullback entry")
    print(f"{'='*70}")
    print("Entry: pullback+reclaim (not every touch) | ATR risk | partial+BE")
    print("Testing 2 timeframes x 4 risk configs. NET.\n")

    print(f"{'TF':<5}{'RISK CONFIG':<18}{'trades':>8}{'win%':>7}{'net avg':>10}{'total':>9}")
    print("-" * 70)

    best = None
    for (tf_label, freq, expiry, noprog, maxbar) in TIMEFRAMES:
        for (rlabel, n_atr, pR, fR) in RISK_CONFIGS:
            trades = backtest(symbols, tf_label, freq, expiry, noprog, maxbar, n_atr, pR, fR)
            if not trades:
                print(f"{tf_label:<5}{rlabel:<18}{'0':>8}")
                continue
            rets = [t[0] for t in trades]
            n = len(rets); avg = sum(rets)/n
            wins = sum(1 for x in rets if x > 0)
            print(f"{tf_label:<5}{rlabel:<18}{n:>8}{wins/n*100:>6.0f}%{avg:>9.3f}%{sum(rets):>8.1f}%")
            if best is None or avg > best[2]:
                best = (tf_label, rlabel, avg, n)

    print("-" * 70)
    if best:
        print(f"BEST: {best[0]} / {best[1]} → {best[2]:+.3f}% net/trade ({best[3]} trades)")
        if best[2] > 0:
            print("  -> Net-positive. Pullback entry + ATR risk shows edge.")
            print("     Compare to old +0.189% (drift-based). If higher AND")
            print("     more trades hit target (not timeout), this is the real fix.")
        else:
            print("  -> Still negative. Pullback entry didn't beat drift here.")
    print("  ~10-day intraday sample. Validate on Alpaca long history.")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    syms = sys.argv[1:] if len(sys.argv) > 1 else \
        ["NVDA", "AMZN", "MSFT", "GOOGL", "AMD", "AVGO", "CRWD", "MU",
         "META", "TSLA", "SMCI", "CVNA", "COIN", "PLTR", "NFLX", "SHOP",
         "UBER", "HOOD", "RIOT", "MARA"]
    run(syms)
