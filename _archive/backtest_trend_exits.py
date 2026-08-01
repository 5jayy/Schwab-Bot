"""
TREND EXIT COMPARISON — fix the 88% timeout with trend-based exits.

Problem: fixed target (10%) + fixed hold → 88% of trades time out (drift, never
hit target, close at arbitrary bar limit). The drift is wasted.

Solution to test: TREND-BASED exits that ride the move until the trend actually
breaks — no fixed target, no timeout. Compares:

  FIXED         — target 10% + hard stop + hold limit (current)
  EMA_CROSS     — exit when price closes below fast EMA (9)
  STRUCT_BREAK  — exit when price makes a lower low (structure turned)
  ATR_TRAIL     — chandelier: trail stop 3xATR below the high
  HIGHER_CLOSE  — exit after 2 consecutive lower closes (stall)

All keep the same hard stop for disaster protection. All NET of slippage.
Uses EMA200 daily bias entries (the proven-better direction).

Run locally:  python3 backtest_trend_exits.py
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
EMA_LEN = 200


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


def ema(values, length):
    if len(values) < length:
        return None
    k = 2 / (length + 1)
    e = sum(values[:length]) / length
    for v in values[length:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(values, length):
    """Return EMA at each point (for exit checks)."""
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


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]; l = candles[i]["low"]; pc = candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


def aggregate(candles, group):
    out = []
    for i in range(0, len(candles) - group + 1, group):
        ch = candles[i:i+group]
        if len(ch) < group:
            break
        out.append({"open": ch[0]["open"], "high": max(c["high"] for c in ch),
                    "low": min(c["low"] for c in ch), "close": ch[-1]["close"]})
    return out


def daily_bias(daily):
    closes = [c["close"] for c in daily]
    e = ema(closes, EMA_LEN) or ema(closes, 50)
    if e is None:
        return "flat"
    price = closes[-1]
    if price > e * 1.01:
        return "bull"
    if price < e * 0.99:
        return "bear"
    return "flat"


def structure_dir(candles, lookback=3):
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


def score_entry(w, h1, dbias):
    if dbias == "flat":
        return 0, None
    dir_ = dbias; s = W_4H
    if structure_dir(h1) == dir_:
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


# ---------- EXIT STRATEGIES (bull-oriented; short mirrors) ----------

def exit_fixed(entry, fwd, dir_):
    hard = entry*(1-HARD_STOP) if dir_=="bull" else entry*(1+HARD_STOP)
    tgt  = entry*(1+0.10) if dir_=="bull" else entry*(1-0.10)
    for c in fwd:
        lo = c.get("low", c["close"]); hi = c.get("high", c["close"])
        if dir_=="bull":
            if lo <= hard: return -HARD_STOP*100
            if hi >= tgt: return 10.0
        else:
            if hi >= hard: return -HARD_STOP*100
            if lo <= tgt: return 10.0
    last = fwd[-1]["close"]
    return ((last-entry) if dir_=="bull" else (entry-last))/entry*100


def exit_ema_cross(entry, fwd, dir_, fast=9):
    hard = entry*(1-HARD_STOP) if dir_=="bull" else entry*(1+HARD_STOP)
    closes = [entry] + [c["close"] for c in fwd]
    es = ema_series(closes, fast)
    for idx, c in enumerate(fwd, start=1):
        px = c["close"]; lo = c.get("low", px); hi = c.get("high", px)
        if dir_=="bull" and lo <= hard: return -HARD_STOP*100
        if dir_=="bear" and hi >= hard: return -HARD_STOP*100
        e = es[idx] if idx < len(es) else None
        if e is not None:
            if dir_=="bull" and px < e:
                return (px-entry)/entry*100
            if dir_=="bear" and px > e:
                return (entry-px)/entry*100
    last = fwd[-1]["close"]
    return ((last-entry) if dir_=="bull" else (entry-last))/entry*100


def exit_struct_break(entry, fwd, dir_):
    hard = entry*(1-HARD_STOP) if dir_=="bull" else entry*(1+HARD_STOP)
    prev_low = None; prev_high = None
    for c in fwd:
        px = c["close"]; lo = c.get("low", px); hi = c.get("high", px)
        if dir_=="bull":
            if lo <= hard: return -HARD_STOP*100
            if prev_low is not None and lo < prev_low:  # lower low = structure break
                return (px-entry)/entry*100
            prev_low = lo
        else:
            if hi >= hard: return -HARD_STOP*100
            if prev_high is not None and hi > prev_high:
                return (entry-px)/entry*100
            prev_high = hi
    last = fwd[-1]["close"]
    return ((last-entry) if dir_=="bull" else (entry-last))/entry*100


def exit_atr_trail(entry, fwd, dir_, mult=3):
    a = atr(fwd[:15]) if len(fwd) >= 15 else (entry*0.01)
    if not a: a = entry*0.01
    high = entry; low = entry
    hard = entry*(1-HARD_STOP) if dir_=="bull" else entry*(1+HARD_STOP)
    for c in fwd:
        px = c["close"]; lo = c.get("low", px); hi = c.get("high", px)
        if dir_=="bull":
            if lo <= hard: return -HARD_STOP*100
            high = max(high, hi)
            if px <= high - mult*a:
                return (px-entry)/entry*100
        else:
            if hi >= hard: return -HARD_STOP*100
            low = min(low, lo)
            if px >= low + mult*a:
                return (entry-px)/entry*100
    last = fwd[-1]["close"]
    return ((last-entry) if dir_=="bull" else (entry-last))/entry*100


def exit_higher_close(entry, fwd, dir_):
    hard = entry*(1-HARD_STOP) if dir_=="bull" else entry*(1+HARD_STOP)
    stalls = 0; prev = entry
    for c in fwd:
        px = c["close"]; lo = c.get("low", px); hi = c.get("high", px)
        if dir_=="bull":
            if lo <= hard: return -HARD_STOP*100
            if px < prev: stalls += 1
            else: stalls = 0
            if stalls >= 2: return (px-entry)/entry*100
        else:
            if hi >= hard: return -HARD_STOP*100
            if px > prev: stalls += 1
            else: stalls = 0
            if stalls >= 2: return (entry-px)/entry*100
        prev = px
    last = fwd[-1]["close"]
    return ((last-entry) if dir_=="bull" else (entry-last))/entry*100


EXITS = {
    "FIXED":        exit_fixed,
    "EMA_CROSS":    exit_ema_cross,
    "STRUCT_BREAK": exit_struct_break,
    "ATR_TRAIL":    exit_atr_trail,
    "HIGHER_CLOSE": exit_higher_close,
}


def run(symbols):
    print(f"\n{'='*66}")
    print("TREND EXIT COMPARISON — fix the 88% timeout")
    print(f"{'='*66}")
    print("EMA200-bias entries. Fixed vs trend-based exits. NET.\n")

    entries = []
    for sym in symbols:
        daily = fetch_daily_year(sym)
        if len(daily) < 60:
            continue
        dbias = daily_bias(daily)
        if dbias == "flat":
            continue
        m5 = fetch_intraday(sym, 5); m30 = fetch_intraday(sym, 30)
        if len(m5) < 80 or len(m30) < 16:
            continue
        h1 = aggregate(m30, 2)
        cnt = 0
        for i in range(20, len(m5) - HOLD_BARS - 1, 3):
            w = m5[:i+1]; entry = m5[i]["close"]; fwd = m5[i+1:i+1+HOLD_BARS]
            if entry <= 0 or not fwd:
                continue
            s, dir_ = score_entry(w, h1, dbias)
            if s >= THRESHOLD and dir_:
                entries.append((entry, fwd, dir_)); cnt += 1
        if cnt:
            print(f"  {sym}: {cnt} ({dbias})")
        time.sleep(0.3)

    print(f"\n  Total entries: {len(entries)}\n")
    if not entries:
        print("No entries."); return

    print(f"{'EXIT':<14}{'net avg':>10}{'total':>10}{'win%':>8}{'worst':>9}")
    print("-" * 66)
    results = {}
    for name, fn in EXITS.items():
        rets = [fn(e, f, d) - ROUND_TRIP_COST for (e, f, d) in entries]
        n = len(rets); avg = sum(rets)/n
        wins = sum(1 for x in rets if x > 0)
        results[name] = avg
        print(f"{name:<14}{avg:>9.3f}%{sum(rets):>9.1f}%{wins/n*100:>7.0f}%{min(rets):>8.1f}%")

    print("-" * 66)
    best = max(results, key=results.get)
    print(f"BEST EXIT: {best} at {results[best]:+.3f}% net/trade")
    print(f"  (FIXED was {results['FIXED']:+.3f}% — the current approach)")
    if best != "FIXED" and results[best] > results["FIXED"]:
        gain = results[best] - results["FIXED"]
        print(f"  -> {best} beats FIXED by {gain:+.3f}%/trade — trend exit captures")
        print(f"     the drift that was timing out. Worth switching to.")
    else:
        print(f"  -> FIXED still best here. Trend exits didn't help this sample.")
    print("  ~10-day sample = directional. Validate on Alpaca long history.")
    print(f"{'='*66}\n")


if __name__ == "__main__":
    syms = sys.argv[1:] if len(sys.argv) > 1 else \
        ["NVDA", "TSLA", "AMZN", "MSFT", "GOOGL", "META", "SMCI", "CVNA", "SHOP",
         "AMD", "PLTR", "COIN", "AVGO", "CRWD", "MU", "NFLX", "UBER", "HOOD", "RIOT", "MARA"]
    run(syms)
