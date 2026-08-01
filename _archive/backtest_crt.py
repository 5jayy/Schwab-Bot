"""
CRT BACKTEST — does Candle Range Theory beat the market?

Walks historical 15m candles, fires a CRT entry wherever a sweep-and-reversal
occurred (price swept below a prior-range low then reclaimed it, green close,
with higher-timeframe daily bias bullish), applies COMBO exits, and compares
to buy-hold (market average).

This is the moment of truth: the old momentum filters LOST to random/market.
Does CRT actually have edge?

Run locally (no deploy):  python3 backtest_crt.py

Honest limits:
  • 15m triggers backtest only ~10 days (Schwab intraday cap) — small sample.
  • Daily bias uses real daily history (deep).
  • Directional evidence, not a promise of exact returns.
"""

import sys
import time
import requests
from auth import get_valid_token

BASE_URL = "https://api.schwabapi.com/marketdata/v1"

# COMBO exit (the proven exit rule)
HARD_STOP = 0.07
TRAIL     = 0.07
BE_AT     = 0.02
TARGET    = 0.12
HOLD_BARS = 24   # 15m bars to hold max (~6 hours of trading)

# --- REAL-WORLD COSTS (for NET returns) ---
# Stocks at Schwab: $0 commission. But slippage is real — you don't fill at
# the exact close. Model round-trip cost as a % haircut per trade.
SLIPPAGE_PCT   = 0.05   # ~0.05% each side (entry+exit) = realistic for liquid names
COMMISSION_PCT = 0.0    # $0 stock commission at Schwab
ROUND_TRIP_COST = SLIPPAGE_PCT * 2 + COMMISSION_PCT  # entry + exit slippage


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def fetch(symbol, freq, period=10, ptype="day", pd=None):
    params = {"symbol": symbol, "periodType": ptype,
              "period": pd if pd else period,
              "frequencyType": "minute" if ptype == "day" else "daily",
              "frequency": freq, "needExtendedHoursData": False}
    if ptype == "month":
        params["frequencyType"] = "daily"
        params["frequency"] = 1
    try:
        r = requests.get(f"{BASE_URL}/pricehistory", headers=headers(),
                         params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("candles", [])
    except Exception:
        return []


def get_bias(candles, lookback=3):
    if len(candles) < lookback + 1:
        return "neutral"
    recent = candles[-lookback:]
    highs = [c["high"] for c in recent]; lows = [c["low"] for c in recent]
    if highs[-1] > highs[0] and lows[-1] > lows[0]:
        return "bull"
    if highs[-1] < highs[0] and lows[-1] < lows[0]:
        return "bear"
    return "neutral"


def combo_exit(entry, fwd):
    high = entry
    hard_px = entry * (1 - HARD_STOP)
    tgt_px  = entry * (1 + TARGET)
    for c in fwd:
        px = c["close"]; lo = c.get("low", px); hi = c.get("high", px)
        high = max(high, hi)
        if lo <= hard_px:
            return (hard_px - entry) / entry * 100
        if hi >= tgt_px:
            return (tgt_px - entry) / entry * 100
        if (px - entry) / entry >= BE_AT:
            if px <= high * (1 - TRAIL):
                return (px - entry) / entry * 100
    return (fwd[-1]["close"] - entry) / entry * 100


def aggregate(candles, group):
    """Aggregate N smaller candles into larger bars (e.g. 30m x8 = 4h)."""
    out = []
    for i in range(0, len(candles) - group + 1, group):
        chunk = candles[i:i+group]
        if len(chunk) < group:
            break
        out.append({
            "open":  chunk[0]["open"],
            "high":  max(c["high"] for c in chunk),
            "low":   min(c["low"] for c in chunk),
            "close": chunk[-1]["close"],
        })
    return out


def backtest_symbol(symbol):
    daily = fetch(symbol, 1, ptype="month", pd=3)
    m15   = fetch(symbol, 15)
    m30   = fetch(symbol, 30)
    if len(daily) < 5 or len(m15) < 40:
        return None

    daily_bias = get_bias(daily)  # single bias for the window (daily is slow)
    h4 = aggregate(m30, 8) if len(m30) >= 8 else []
    h4_bias = get_bias(h4) if h4 else "neutral"

    crt_trades = []
    bh_trades  = []

    # Walk 15m candles. At each bar i, the "range" = prior candle's low.
    # CRT long trigger: candle i sweeps below prior low then closes back above, green.
    for i in range(5, len(m15) - HOLD_BARS - 1):
        prior = m15[i-1]
        c     = m15[i]
        level_low = prior["low"]

        swept   = c["low"] < level_low
        reclaim = c["close"] > level_low
        green   = c["close"] > c["open"]
        entry   = c["close"]
        fwd     = m15[i+1:i+1+HOLD_BARS]
        if entry <= 0 or not fwd:
            continue

        # Baseline: every bar buy-hold
        bh_trades.append((fwd[-1]["close"] - entry) / entry * 100)

        # CRT entry: sweep-reversal AND daily bias bull AND 4h bias not bearish
        if swept and reclaim and green and daily_bias == "bull" and h4_bias != "bear":
            gross = combo_exit(entry, fwd)
            net   = gross - ROUND_TRIP_COST   # subtract slippage/commission
            crt_trades.append(net)

    return {"CRT": crt_trades, "BUY_HOLD": bh_trades,
            "bias": daily_bias, "h4": h4_bias}


def run(symbols):
    print(f"\n{'='*60}")
    print("CRT BACKTEST — Candle Range Theory vs market")
    print(f"{'='*60}")
    print(f"Entry: sweep prior-low + reclaim + green + daily bias bull")
    print(f"Exit: COMBO (hard {int(HARD_STOP*100)}% + trail {int(TRAIL*100)}% "
          f"+ target {int(TARGET*100)}%)")
    print(f"CRT returns are NET (slippage {SLIPPAGE_PCT}%/side, "
          f"round-trip -{ROUND_TRIP_COST:.2f}%). Buy-hold is gross.\n")

    crt_all = []; bh_all = []
    for sym in symbols:
        r = backtest_symbol(sym)
        if r:
            crt_all.extend(r["CRT"])
            bh_all.extend(r["BUY_HOLD"])
            print(f"  {sym}: daily={r['bias']:<7} 4h={r['h4']:<7} CRT entries={len(r['CRT'])}")
        time.sleep(0.3)

    print(f"\n{'-'*60}")
    print(f"{'STRATEGY':<12}{'trades':>8}{'win%':>8}{'avg':>9}{'worst':>9}{'total':>10}")
    print(f"{'-'*60}")

    def stats(name, rets, tag=""):
        if not rets:
            print(f"{name:<12}{'0':>8}  (no trades)")
            return None
        n = len(rets); wins = [x for x in rets if x > 0]
        avg = sum(rets)/n
        print(f"{name:<12}{n:>8}{len(wins)/n*100:>7.0f}%{avg:>8.2f}%"
              f"{min(rets):>8.1f}%{sum(rets):>9.1f}%{tag}")
        return avg

    crt_avg = stats("CRT", crt_all)
    bh_avg  = stats("BUY_HOLD", bh_all, "  <-- market")

    print(f"\n{'-'*60}")
    print("VERDICT:")
    if crt_avg is None:
        print("  CRT fired no trades — setup too rare in this window,")
        print("  or daily bias wasn't bull. Try more/different symbols.")
    elif bh_avg is None:
        print("  No baseline.")
    else:
        print(f"  CRT NET avg {crt_avg:+.2f}%/trade  vs  market gross {bh_avg:+.2f}%/trade")
        if crt_avg > bh_avg and crt_avg > 0:
            print("  -> CRT beats market AFTER COSTS and is positive. Real edge.")
        elif crt_avg > bh_avg:
            print("  -> CRT beats market even after costs, but both negative (rough window).")
            print("     The edge survives real-world costs — that's the key test.")
        elif crt_avg > 0:
            print("  -> CRT positive after costs but under market. Marginal.")
        else:
            print("  -> CRT negative after costs. The thin edge didn't survive slippage.")
    print("  ~10-day 15m sample = directional, small. Re-run with more symbols.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    syms = sys.argv[1:] if len(sys.argv) > 1 else \
        ["NVDA", "AAPL", "AMD", "TSLA", "PLTR", "SOFI", "F", "INTC",
         "HOOD", "COIN", "MARA", "RIOT", "UBER", "SNAP", "PLUG",
         "AMZN", "MSFT", "GOOGL", "META", "NFLX",
         "BAC", "WFC", "DIS", "PYPL", "SQ", "SHOP", "ROKU", "DKNG",
         "CVNA", "AFRM", "RBLX", "ABNB", "CRWD", "PANW", "SMCI",
         "MU", "QCOM", "AVGO", "TXN", "CSCO"]
    run(syms)
