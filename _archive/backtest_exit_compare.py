"""
EXIT COMPARISON BACKTEST — your scoring entries, every exit, NET.

Takes the SAME scoring entries (4H+1H+5m+OB+wick, >=3.5, long+short) and runs
each entry through MULTIPLE exit strategies to see which nets best:

  1. HARD_ONLY   — hard stop at entry, profit target, no trail
  2. TRAIL_ONLY  — trailing stop off high once green (no hard stop)
  3. COMBO       — hard stop + trail once green + target (current)
  4. TP_SCALE    — TP1/TP2 partial scale-out + trail on runner
  Plus COMBO variants at different stop/target %s (tuning).

All returns NET of slippage. Compared to buy-hold baseline.

Run locally:  python3 backtest_exit_compare.py
Honest limit: ~10-day 5m sample. Real answer needs Alpaca long history.
"""

import sys
import time
import requests
from auth import get_valid_token

BASE_URL = "https://api.schwabapi.com/marketdata/v1"

HOLD_BARS       = 60
SLIPPAGE_PCT    = 0.05
ROUND_TRIP_COST = SLIPPAGE_PCT * 2

W_4H, W_1H, W_5M, W_OB, W_WICK, W_INSIGHT = 1.0, 1.0, 1.0, 0.5, 0.5, 0.25
THRESHOLD = 3.5


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def fetch(symbol, freq):
    try:
        r = requests.get(f"{BASE_URL}/pricehistory", headers=headers(),
                         params={"symbol": symbol, "periodType": "day", "period": 10,
                                 "frequencyType": "minute", "frequency": freq,
                                 "needExtendedHoursData": False}, timeout=15)
        r.raise_for_status()
        return r.json().get("candles", [])
    except Exception:
        return []


def aggregate(candles, group):
    out = []
    for i in range(0, len(candles) - group + 1, group):
        ch = candles[i:i+group]
        if len(ch) < group:
            break
        out.append({"open": ch[0]["open"], "high": max(c["high"] for c in ch),
                    "low": min(c["low"] for c in ch), "close": ch[-1]["close"]})
    return out


def direction(candles, lookback=3):
    if len(candles) < lookback + 1:
        return "flat"
    r = candles[-lookback:]
    hh = [c["high"] for c in r]; ll = [c["low"] for c in r]
    if hh[-1] > hh[0] and ll[-1] > ll[0]:
        return "bull"
    if hh[-1] < hh[0] and ll[-1] < ll[0]:
        return "bear"
    return "flat"


def has_order_block(candles, dir_):
    if len(candles) < 4:
        return False
    if dir_ == "bull":
        return any(candles[-i]["close"] < candles[-i]["open"] for i in range(2, 4))
    return any(candles[-i]["close"] > candles[-i]["open"] for i in range(2, 4))


def has_wick_rejection(c, dir_):
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    rng = h - l
    if rng <= 0:
        return False
    if dir_ == "bull":
        return (min(o, cl) - l) > rng * 0.4
    return (h - max(o, cl)) > rng * 0.4


def score_entry(m5_window, h1, h4):
    d4 = direction(h4)
    if d4 == "flat":
        return 0, None
    dir_ = d4
    score = W_4H
    if direction(h1) == dir_:
        score += W_1H
    c5 = m5_window[-1]
    if (dir_ == "bull" and c5["close"] > c5["open"]) or \
       (dir_ == "bear" and c5["close"] < c5["open"]):
        score += W_5M
    if has_order_block(m5_window, dir_):
        score += W_OB
    if has_wick_rejection(c5, dir_):
        score += W_WICK
    if h1:
        h1c = h1[-1]
        if (dir_ == "bull" and h1c["close"] > h1c["open"]) or \
           (dir_ == "bear" and h1c["close"] < h1c["open"]):
            score += W_INSIGHT
    return score, dir_


# ---------- EXIT STRATEGIES (all return NET %) ----------

def _long_hard(entry, fwd, hard, target):
    hp = entry * (1 - hard); tp = entry * (1 + target)
    for c in fwd:
        lo = c.get("low", c["close"]); hi = c.get("high", c["close"])
        if lo <= hp: return (hp - entry) / entry * 100
        if hi >= tp: return (tp - entry) / entry * 100
    return (fwd[-1]["close"] - entry) / entry * 100


def _long_trail(entry, fwd, trail, be_at):
    high = entry
    for c in fwd:
        px = c["close"]; hi = c.get("high", px)
        high = max(high, hi)
        if (px - entry) / entry >= be_at and px <= high * (1 - trail):
            return (px - entry) / entry * 100
    return (fwd[-1]["close"] - entry) / entry * 100


def _long_combo(entry, fwd, hard, trail, be_at, target):
    high = entry; hp = entry * (1 - hard); tp = entry * (1 + target)
    for c in fwd:
        px = c["close"]; lo = c.get("low", px); hi = c.get("high", px)
        high = max(high, hi)
        if lo <= hp: return (hp - entry) / entry * 100
        if hi >= tp: return (tp - entry) / entry * 100
        if (px - entry) / entry >= be_at and px <= high * (1 - trail):
            return (px - entry) / entry * 100
    return (fwd[-1]["close"] - entry) / entry * 100


def _long_tp_scale(entry, fwd, hard, tp1, tp2, trail, be_at):
    """TP1 (1/3 off), TP2 (1/3 off), trail runner. Blended return."""
    high = entry; hp = entry * (1 - hard)
    tp1_px = entry * (1 + tp1); tp2_px = entry * (1 + tp2)
    hit1 = hit2 = False; realized = 0.0; remaining = 1.0
    for c in fwd:
        px = c["close"]; lo = c.get("low", px); hi = c.get("high", px)
        high = max(high, hi)
        if lo <= hp:
            realized += remaining * (hp - entry) / entry * 100
            return realized
        if not hit1 and hi >= tp1_px:
            realized += (1/3) * tp1 * 100; remaining -= 1/3; hit1 = True
        if not hit2 and hi >= tp2_px:
            realized += (1/3) * tp2 * 100; remaining -= 1/3; hit2 = True
        if hit2 and (px - entry) / entry >= be_at and px <= high * (1 - trail):
            realized += remaining * (px - entry) / entry * 100
            return realized
    realized += remaining * (fwd[-1]["close"] - entry) / entry * 100
    return realized


def apply_exit(entry, fwd, dir_, name):
    """Route to the right exit; mirror for shorts by flipping the price path."""
    if dir_ == "bear":
        # transform to an equivalent long by mirroring returns around entry
        mfwd = [{"open": 2*entry - c["open"], "high": 2*entry - c.get("low", c["close"]),
                 "low": 2*entry - c.get("high", c["close"]), "close": 2*entry - c["close"]}
                for c in fwd]
        fwd = mfwd
    if name == "HARD_ONLY":
        r = _long_hard(entry, fwd, 0.07, 0.12)
    elif name == "TRAIL_ONLY":
        r = _long_trail(entry, fwd, 0.07, 0.02)
    elif name == "COMBO":
        r = _long_combo(entry, fwd, 0.07, 0.07, 0.02, 0.12)
    elif name == "COMBO_5/10":
        r = _long_combo(entry, fwd, 0.05, 0.05, 0.02, 0.10)
    elif name == "COMBO_8/15":
        r = _long_combo(entry, fwd, 0.08, 0.08, 0.02, 0.15)
    elif name == "TP_SCALE":
        r = _long_tp_scale(entry, fwd, 0.07, 0.05, 0.10, 0.05, 0.02)
    else:
        r = 0
    return r - ROUND_TRIP_COST


EXITS = ["HARD_ONLY", "TRAIL_ONLY", "COMBO", "COMBO_5/10", "COMBO_8/15", "TP_SCALE"]


def backtest_symbol(symbol):
    m5 = fetch(symbol, 5); m30 = fetch(symbol, 30)
    if len(m5) < 80 or len(m30) < 16:
        return None
    h4 = aggregate(m30, 8); h1 = aggregate(m30, 2)
    if len(h4) < 4 or len(h1) < 4:
        return None
    out = {e: [] for e in EXITS}; bh = []
    for i in range(20, len(m5) - HOLD_BARS - 1, 3):
        window = m5[:i+1]; entry = m5[i]["close"]
        fwd = m5[i+1:i+1+HOLD_BARS]
        if entry <= 0 or not fwd:
            continue
        bh.append((fwd[-1]["close"] - entry) / entry * 100)
        score, dir_ = score_entry(window, h1, h4)
        if score >= THRESHOLD and dir_:
            for e in EXITS:
                out[e].append(apply_exit(entry, fwd, dir_, e))
    out["_bh"] = bh
    return out


def run(symbols):
    print(f"\n{'='*66}")
    print("EXIT COMPARISON — your scoring entries, every exit, NET")
    print(f"{'='*66}")
    print(f"Same entries (score>=3.5) run through each exit. NET of slippage.\n")

    agg = {e: [] for e in EXITS}; bh_all = []
    for sym in symbols:
        r = backtest_symbol(sym)
        if r:
            for e in EXITS:
                agg[e].extend(r[e])
            bh_all.extend(r["_bh"])
            print(f"  {sym}: {len(r['COMBO'])} entries")
        time.sleep(0.3)

    print(f"\n{'-'*66}")
    print(f"{'EXIT':<14}{'trades':>8}{'win%':>8}{'avg net':>10}{'worst':>9}{'total':>11}")
    print(f"{'-'*66}")

    def line(name, rets):
        if not rets:
            print(f"{name:<14}{'0':>8}"); return None
        n = len(rets); w = [x for x in rets if x > 0]
        avg = sum(rets)/n
        print(f"{name:<14}{n:>8}{len(w)/n*100:>7.0f}%{avg:>9.2f}%"
              f"{min(rets):>8.1f}%{sum(rets):>10.1f}%")
        return avg

    best_name, best_avg = None, -999
    for e in EXITS:
        a = line(e, agg[e])
        if a is not None and a > best_avg:
            best_avg, best_name = a, e
    bh_avg = line("BUY_HOLD", bh_all)

    print(f"\n{'-'*66}")
    print("VERDICT:")
    if best_name:
        print(f"  Best exit: {best_name} at {best_avg:+.2f}% net/trade")
        if bh_avg is not None:
            print(f"  Market (buy-hold): {bh_avg:+.2f}%")
            if best_avg > bh_avg and best_avg > 0:
                print(f"  -> {best_name} beats market AND net-positive. Test live small.")
            elif best_avg > bh_avg:
                print(f"  -> {best_name} beats market, both negative. Edge survives costs.")
            else:
                print(f"  -> Best exit still under market. Entries need work, not exits.")
    print("  ~10-day sample. Real answer = Alpaca long history.")
    print(f"{'='*66}\n")


if __name__ == "__main__":
    syms = sys.argv[1:] if len(sys.argv) > 1 else \
        ["NVDA", "TSLA", "AMZN", "MSFT", "GOOGL", "META", "SMCI", "CVNA", "SHOP",
         "AMD", "PLTR", "COIN", "AVGO", "CRWD", "MU", "NFLX", "UBER", "HOOD"]
    run(syms)
