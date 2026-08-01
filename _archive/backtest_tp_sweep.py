"""
TP / BREAKEVEN SWEEP — find the best scale-out + breakeven config, NET.

Tests your scoring entries through multiple TP1/TP2/full-close/breakeven
configurations to see which nets best AND how often each level actually fires.

Each config:
  • TP1: take 1/3 off at +tp1%
  • TP2: take another 1/3 off at +tp2%
  • Runner: trails, or full-closes at +target%
  • BREAKEVEN: once trade reaches +be_arm%, stop moves to entry (breakeven)
    -> HONEST TEST: shows how often breakeven even ARMS (most trades drift
       and never get there — breakeven can't protect what never goes green).

Shows exit-reason breakdown so you SEE how many trades reach TP1/TP2/target
vs just time out. Returns NET of slippage.

Run locally:  python3 backtest_tp_sweep.py
"""

import sys
import time
import requests
from auth import get_valid_token

BASE_URL = "https://api.schwabapi.com/marketdata/v1"

HOLD_BARS = 60
SLIPPAGE_PCT = 0.05
ROUND_TRIP_COST = SLIPPAGE_PCT * 2
HARD_STOP = 0.05

W_4H, W_1H, W_5M, W_OB, W_WICK, W_INSIGHT = 1.0, 1.0, 1.0, 0.5, 0.5, 0.25
THRESHOLD = 3.5

# Configs to test: (name, tp1%, tp2%, target%, be_arm%, trail%)
# be_arm = profit level at which stop moves to breakeven
CONFIGS = [
    ("tp2/4/6  be2",  0.02, 0.04, 0.06, 0.02, 0.03),
    ("tp3/5/8  be3",  0.03, 0.05, 0.08, 0.03, 0.04),
    ("tp2/4/6  be1.5",0.02, 0.04, 0.06, 0.015,0.03),
    ("tp3/6/10 be3",  0.03, 0.06, 0.10, 0.03, 0.05),
    ("tp2/3/4  be1.5",0.02, 0.03, 0.04, 0.015,0.02),  # tight, fast targets
]


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


def has_ob(candles, dir_):
    if len(candles) < 4:
        return False
    if dir_ == "bull":
        return any(candles[-i]["close"] < candles[-i]["open"] for i in range(2, 4))
    return any(candles[-i]["close"] > candles[-i]["open"] for i in range(2, 4))


def has_wick(c, dir_):
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    rng = h - l
    if rng <= 0:
        return False
    return (min(o, cl) - l) > rng * 0.4 if dir_ == "bull" else (h - max(o, cl)) > rng * 0.4


def score_entry(w, h1, h4):
    d4 = direction(h4)
    if d4 == "flat":
        return 0, None
    dir_ = d4; s = W_4H
    if direction(h1) == dir_:
        s += W_1H
    c5 = w[-1]
    if (dir_ == "bull" and c5["close"] > c5["open"]) or (dir_ == "bear" and c5["close"] < c5["open"]):
        s += W_5M
    if has_ob(w, dir_):
        s += W_OB
    if has_wick(c5, dir_):
        s += W_WICK
    if h1 and ((dir_ == "bull" and h1[-1]["close"] > h1[-1]["open"]) or
               (dir_ == "bear" and h1[-1]["close"] < h1[-1]["open"])):
        s += W_INSIGHT
    return s, dir_


def simulate(entry, fwd, dir_, tp1, tp2, target, be_arm, trail):
    """
    Scale-out with breakeven. Returns (net%, reason, be_armed_bool).
    1/3 at tp1, 1/3 at tp2, runner trails or hits target.
    Breakeven: once max favorable >= be_arm, stop = entry for the remainder.
    """
    def fav(px):
        return (px - entry) / entry if dir_ == "bull" else (entry - px) / entry

    realized = 0.0
    remaining = 1.0
    hit1 = hit2 = False
    be_armed = False
    peak = 0.0  # max favorable excursion (fraction)
    hard = -HARD_STOP  # stop as fraction (adverse)

    for c in fwd:
        px = c["close"]
        hi = c.get("high", px); lo = c.get("low", px)
        # favorable at the extreme this bar
        bar_fav = fav(hi if dir_ == "bull" else lo)
        bar_adv = -fav(lo if dir_ == "bull" else hi)  # positive number = adverse move
        peak = max(peak, bar_fav)
        if peak >= be_arm:
            be_armed = True

        # Stop check: breakeven if armed, else hard stop
        stop_level = 0.0 if be_armed else hard
        # adverse move breaches stop?
        cur_adv = -fav(lo if dir_ == "bull" else hi)
        if cur_adv >= -stop_level and not be_armed:
            # hard stop hit
            realized += remaining * (-HARD_STOP) * 100
            return realized - ROUND_TRIP_COST, "stop", be_armed
        if be_armed and fav(px) <= 0:
            # breakeven stop hit (price came back to entry after arming)
            realized += remaining * 0.0 * 100
            return realized - ROUND_TRIP_COST, "breakeven", be_armed

        # TP1
        if not hit1 and bar_fav >= tp1:
            realized += (1/3) * tp1 * 100; remaining -= 1/3; hit1 = True
        # TP2
        if not hit2 and bar_fav >= tp2:
            realized += (1/3) * tp2 * 100; remaining -= 1/3; hit2 = True
        # Runner full close at target
        if hit2 and bar_fav >= target:
            realized += remaining * target * 100
            return realized - ROUND_TRIP_COST, "target", be_armed
        # Runner trail after tp2
        if hit2 and be_armed:
            if fav(px) <= peak - trail:
                realized += remaining * fav(px) * 100
                return realized - ROUND_TRIP_COST, "trail", be_armed

    # timeout — close remainder at last price
    realized += remaining * fav(fwd[-1]["close"]) * 100
    return realized - ROUND_TRIP_COST, "timeout", be_armed


def run(symbols):
    print(f"\n{'='*70}")
    print("TP / BREAKEVEN SWEEP — best scale-out config, NET")
    print(f"{'='*70}")
    print("Entries: score>=3.5 | tests TP1/TP2/target + breakeven combos\n")

    # gather entries once
    entries = []
    for sym in symbols:
        m5 = fetch(sym, 5); m30 = fetch(sym, 30)
        if len(m5) < 80 or len(m30) < 16:
            continue
        h4 = aggregate(m30, 8); h1 = aggregate(m30, 2)
        if len(h4) < 4 or len(h1) < 4:
            continue
        cnt = 0
        for i in range(20, len(m5) - HOLD_BARS - 1, 3):
            w = m5[:i+1]; entry = m5[i]["close"]; fwd = m5[i+1:i+1+HOLD_BARS]
            if entry <= 0 or not fwd:
                continue
            s, dir_ = score_entry(w, h1, h4)
            if s >= THRESHOLD and dir_:
                entries.append((entry, fwd, dir_)); cnt += 1
        if cnt:
            print(f"  {sym}: {cnt}")
        time.sleep(0.3)

    print(f"\n  Total entries: {len(entries)}\n")
    if not entries:
        print("No entries."); return

    print(f"{'CONFIG':<16}{'net avg':>9}{'total':>9}{'win%':>7}"
          f"{'BE armed':>9}{'target':>8}{'timeout':>8}")
    print("-" * 70)

    best = None
    for (name, tp1, tp2, target, be_arm, trail) in CONFIGS:
        rets = []; reasons = {}; armed = 0
        for (entry, fwd, dir_) in entries:
            net, reason, be = simulate(entry, fwd, dir_, tp1, tp2, target, be_arm, trail)
            rets.append(net)
            reasons[reason] = reasons.get(reason, 0) + 1
            if be:
                armed += 1
        n = len(rets); avg = sum(rets)/n
        wins = sum(1 for x in rets if x > 0)
        tgt = reasons.get("target", 0); to = reasons.get("timeout", 0)
        print(f"{name:<16}{avg:>8.3f}%{sum(rets):>8.1f}%{wins/n*100:>6.0f}%"
              f"{armed/n*100:>8.0f}%{tgt/n*100:>7.0f}%{to/n*100:>7.0f}%")
        if best is None or avg > best[1]:
            best = (name, avg)

    print("-" * 70)
    print(f"BEST: {best[0]} at {best[1]:+.3f}% net/trade")
    print("\nHONEST READ:")
    print("  • 'BE armed' = % of trades that got green enough to arm breakeven.")
    print("    The rest NEVER reached breakeven — it can't protect them.")
    print("  • 'timeout' = % that drifted and closed at hold limit.")
    print("    High timeout = the real problem is ENTRIES (trades don't move),")
    print("    which no TP/breakeven config can fix.")
    print("  • If best is still negative: exits are tuned; entries are the bottleneck.")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    syms = sys.argv[1:] if len(sys.argv) > 1 else \
        ["NVDA", "TSLA", "AMZN", "MSFT", "GOOGL", "META", "SMCI", "CVNA", "SHOP"]
    run(syms)
