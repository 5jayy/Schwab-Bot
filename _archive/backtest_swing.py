"""
SWING BACKTEST — your scoring entry system, long + short.

Your entry scoring (need >= 3.5 to trade):
  4H direction   1.0  (REQUIRED — flat 4H = no trade)
  1H structure   1.0  (aligned with 4H)
  5m entry       1.0  (confirmation in direction)
  OB order block 0.5
  wick rejection 0.5
  1H insight     0.25

Core 3.0 (4H+1H+5m) + at least one of OB/wick (0.5) clears 3.5.
Direction: LONG on bull setups, SHORT on bear setups.

Timeframes on Schwab (no native 4h/1h — built by aggregation):
  4h = 30m x8, 1h = 30m x2, 5m = native.

Exit: COMBO (hard stop + trail once green + target), applied both directions.
Returns are NET (slippage). Compared to buy-hold baseline.

Run locally:  python3 backtest_swing.py
Honest limit: 5m/intraday only ~10 days on Schwab. Small sample until real data.
"""

import sys
import time
import requests
from auth import get_valid_token

BASE_URL = "https://api.schwabapi.com/marketdata/v1"

# Exit params (COMBO)
HARD_STOP = 0.07
TRAIL     = 0.07
BE_AT     = 0.02
TARGET    = 0.12
HOLD_BARS = 60   # 5m bars to hold (~5 hours)

# Costs
SLIPPAGE_PCT    = 0.05
ROUND_TRIP_COST = SLIPPAGE_PCT * 2

# Entry scoring weights
W_4H, W_1H, W_5M, W_OB, W_WICK, W_INSIGHT = 1.0, 1.0, 1.0, 0.5, 0.5, 0.25
THRESHOLD = 3.5


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def fetch(symbol, freq, ptype="day", period=10, pd=None):
    params = {"symbol": symbol, "periodType": ptype,
              "period": pd if pd else period,
              "frequencyType": "minute", "frequency": freq,
              "needExtendedHoursData": False}
    if ptype == "month":
        params["frequencyType"] = "daily"; params["frequency"] = 1
    try:
        r = requests.get(f"{BASE_URL}/pricehistory", headers=headers(),
                         params=params, timeout=15)
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
    """bull / bear / flat from swing structure."""
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
    """OB: last opposite-color candle before the current move."""
    if len(candles) < 3:
        return False
    if dir_ == "bull":
        # a red candle in recent bars (the OB that got run through up)
        return any(candles[-i]["close"] < candles[-i]["open"] for i in range(2, 4))
    else:
        return any(candles[-i]["close"] > candles[-i]["open"] for i in range(2, 4))


def has_wick_rejection(candle, dir_):
    """Long wick against trend = rejection in trend direction."""
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    rng = h - l
    if rng <= 0:
        return False
    body_hi = max(o, c); body_lo = min(o, c)
    lower_wick = body_lo - l
    upper_wick = h - body_hi
    if dir_ == "bull":
        return lower_wick > rng * 0.4   # long lower wick = buyers rejected lows
    else:
        return upper_wick > rng * 0.4   # long upper wick = sellers rejected highs


def score_entry(m5_window, h1, h4):
    """
    Compute the entry score and direction. Returns (score, direction) or (0, None).
    4H direction is REQUIRED; if flat, no trade.
    """
    d4 = direction(h4)
    if d4 == "flat":
        return 0, None
    dir_ = d4

    score = W_4H  # 4H direction present (required)

    # 1H structure aligned
    d1 = direction(h1)
    if d1 == dir_:
        score += W_1H

    # 5m entry confirmation (last 5m candle closes in direction)
    c5 = m5_window[-1]
    if dir_ == "bull" and c5["close"] > c5["open"]:
        score += W_5M
    elif dir_ == "bear" and c5["close"] < c5["open"]:
        score += W_5M

    # OB
    if has_order_block(m5_window, dir_):
        score += W_OB

    # wick rejection on the entry candle
    if has_wick_rejection(c5, dir_):
        score += W_WICK

    # 1H insight (1h last candle momentum in direction)
    if h1 and len(h1) >= 1:
        h1c = h1[-1]
        if dir_ == "bull" and h1c["close"] > h1c["open"]:
            score += W_INSIGHT
        elif dir_ == "bear" and h1c["close"] < h1c["open"]:
            score += W_INSIGHT

    return score, dir_


def combo_exit(entry, fwd, dir_):
    """COMBO exit for long or short. Returns NET % return."""
    if dir_ == "bull":
        high = entry; hard = entry * (1 - HARD_STOP); tgt = entry * (1 + TARGET)
        for c in fwd:
            px = c["close"]; lo = c.get("low", px); hi = c.get("high", px)
            high = max(high, hi)
            if lo <= hard: return (hard - entry) / entry * 100 - ROUND_TRIP_COST
            if hi >= tgt:  return (tgt - entry) / entry * 100 - ROUND_TRIP_COST
            if (px - entry) / entry >= BE_AT and px <= high * (1 - TRAIL):
                return (px - entry) / entry * 100 - ROUND_TRIP_COST
        return (fwd[-1]["close"] - entry) / entry * 100 - ROUND_TRIP_COST
    else:  # short: profit when price falls
        low = entry; hard = entry * (1 + HARD_STOP); tgt = entry * (1 - TARGET)
        for c in fwd:
            px = c["close"]; lo = c.get("low", px); hi = c.get("high", px)
            low = min(low, lo)
            if hi >= hard: return (entry - hard) / entry * 100 - ROUND_TRIP_COST
            if lo <= tgt:  return (entry - tgt) / entry * 100 - ROUND_TRIP_COST
            if (entry - px) / entry >= BE_AT and px >= low * (1 + TRAIL):
                return (entry - px) / entry * 100 - ROUND_TRIP_COST
        return (entry - fwd[-1]["close"]) / entry * 100 - ROUND_TRIP_COST


def backtest_symbol(symbol):
    m5  = fetch(symbol, 5)
    m30 = fetch(symbol, 30)
    if len(m5) < 80 or len(m30) < 16:
        return None
    h4 = aggregate(m30, 8)
    h1 = aggregate(m30, 2)
    if len(h4) < 4 or len(h1) < 4:
        return None

    trades = []; longs = 0; shorts = 0
    bh = []
    for i in range(20, len(m5) - HOLD_BARS - 1, 3):
        window = m5[:i+1]
        entry  = m5[i]["close"]
        fwd    = m5[i+1:i+1+HOLD_BARS]
        if entry <= 0 or not fwd:
            continue
        bh.append((fwd[-1]["close"] - entry) / entry * 100)

        score, dir_ = score_entry(window, h1, h4)
        if score >= THRESHOLD and dir_:
            trades.append(combo_exit(entry, fwd, dir_))
            if dir_ == "bull": longs += 1
            else: shorts += 1

    return {"trades": trades, "bh": bh, "longs": longs, "shorts": shorts}


def run(symbols):
    print(f"\n{'='*62}")
    print("SWING BACKTEST — your scoring system (need >=3.5), long+short")
    print(f"{'='*62}")
    print(f"Score: 4H(req)1.0 +1H 1.0 +5m 1.0 +OB 0.5 +wick 0.5 +insight 0.25")
    print(f"Exit COMBO | NET (slip -{ROUND_TRIP_COST:.2f}%/trade) | vs buy-hold\n")

    all_tr = []; all_bh = []; L = 0; S = 0
    for sym in symbols:
        r = backtest_symbol(sym)
        if r:
            all_tr.extend(r["trades"]); all_bh.extend(r["bh"])
            L += r["longs"]; S += r["shorts"]
            print(f"  {sym}: {len(r['trades'])} trades ({r['longs']}L/{r['shorts']}S)")
        time.sleep(0.3)

    print(f"\n{'-'*62}")
    print(f"{'STRATEGY':<12}{'trades':>8}{'win%':>8}{'avg':>9}{'worst':>9}{'total':>10}")
    print(f"{'-'*62}")

    def stats(name, rets, tag=""):
        if not rets:
            print(f"{name:<12}{'0':>8}  (no trades)"); return None
        n = len(rets); w = [x for x in rets if x > 0]
        print(f"{name:<12}{n:>8}{len(w)/n*100:>7.0f}%{sum(rets)/n:>8.2f}%"
              f"{min(rets):>8.1f}%{sum(rets):>9.1f}%{tag}")
        return sum(rets)/n

    sw = stats("SCORING", all_tr)
    bh = stats("BUY_HOLD", all_bh, "  <-- market")

    print(f"\n{'-'*62}")
    print(f"Entries: {L} long / {S} short")
    print("VERDICT:")
    if sw is None:
        print("  No trades cleared 3.5 — threshold strict or flat 4H. Widen symbols.")
    elif bh is None:
        print("  No baseline.")
    else:
        print(f"  SCORING net {sw:+.2f}%/trade  vs  market {bh:+.2f}%/trade")
        if sw > bh and sw > 0:
            print("  -> BEATS market AND net-positive. Real edge — test live small.")
        elif sw > bh:
            print("  -> Beats market, both negative (rough window). Edge survives costs.")
        elif sw > 0:
            print("  -> Net-positive but under market. Marginal.")
        else:
            print("  -> Net-negative. This entry set doesn't clear costs here.")
    print("  ~10-day 5m sample = directional. Real data (Alpaca) = real answer.")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    syms = sys.argv[1:] if len(sys.argv) > 1 else \
        ["NVDA", "AAPL", "AMD", "TSLA", "PLTR", "SOFI", "F", "INTC",
         "HOOD", "COIN", "MARA", "RIOT", "UBER", "SNAP", "AMZN",
         "MSFT", "GOOGL", "META", "NFLX", "AVGO", "CRWD", "PANW",
         "SMCI", "MU", "QCOM", "CVNA", "AFRM", "SHOP", "ROKU", "DKNG"]
    run(syms)
