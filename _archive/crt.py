"""
CRT ENGINE — Candle Range Theory, multi-timeframe swing entries.

Concept:
  • Daily + 4h candles set the BIAS (direction) and KEY LEVELS (prior candle
    high/low = the "range" that liquidity sits above/below).
  • 1h + 15m trigger the ENTRY: price SWEEPS a key level (takes liquidity by
    poking beyond a prior high/low) then REVERSES back inside the range.
    That sweep-and-reversal is the CRT entry — you enter the reversal.

Timeframe sourcing (Schwab has no native 4h/1h — we build them):
  • Daily  → fetched directly (deep history)
  • 4h/1h  → aggregated from 30-min candles
  • 15m    → fetched directly

This module is LOGIC ONLY — it returns a signal dict. It does not place trades.
Backtest it first (backtest_crt.py), then wire into the live bot if it proves out.
"""

import time
import requests
from auth import get_valid_token

BASE_URL = "https://api.schwabapi.com/marketdata/v1"


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


# ---------- Data fetching ----------

def fetch_daily(symbol, days_back=60):
    try:
        resp = requests.get(
            f"{BASE_URL}/pricehistory", headers=headers(),
            params={"symbol": symbol, "periodType": "month", "period": 3,
                    "frequencyType": "daily", "frequency": 1,
                    "needExtendedHoursData": False}, timeout=15)
        resp.raise_for_status()
        return resp.json().get("candles", [])
    except Exception:
        return []


def fetch_30m(symbol, period=10):
    try:
        resp = requests.get(
            f"{BASE_URL}/pricehistory", headers=headers(),
            params={"symbol": symbol, "periodType": "day", "period": period,
                    "frequencyType": "minute", "frequency": 30,
                    "needExtendedHoursData": False}, timeout=15)
        resp.raise_for_status()
        return resp.json().get("candles", [])
    except Exception:
        return []


def fetch_15m(symbol, period=10):
    try:
        resp = requests.get(
            f"{BASE_URL}/pricehistory", headers=headers(),
            params={"symbol": symbol, "periodType": "day", "period": period,
                    "frequencyType": "minute", "frequency": 15,
                    "needExtendedHoursData": False}, timeout=15)
        resp.raise_for_status()
        return resp.json().get("candles", [])
    except Exception:
        return []


def aggregate(candles, group):
    """Aggregate N smaller candles into larger bars (e.g. 30m x2 = 1h)."""
    out = []
    for i in range(0, len(candles) - group + 1, group):
        chunk = candles[i:i+group]
        if len(chunk) < group:
            break
        out.append({
            "open":   chunk[0]["open"],
            "high":   max(c["high"] for c in chunk),
            "low":    min(c["low"] for c in chunk),
            "close":  chunk[-1]["close"],
            "datetime": chunk[-1]["datetime"],
        })
    return out


# ---------- CRT logic ----------

def get_bias(candles, lookback=3):
    """
    Bias from a timeframe's recent candles.
    Bullish if making higher highs+higher lows; bearish if lower.
    Returns 'bull', 'bear', or 'neutral'.
    """
    if len(candles) < lookback + 1:
        return "neutral"
    recent = candles[-lookback:]
    highs = [c["high"] for c in recent]
    lows  = [c["low"] for c in recent]
    if highs[-1] > highs[0] and lows[-1] > lows[0]:
        return "bull"
    if highs[-1] < highs[0] and lows[-1] < lows[0]:
        return "bear"
    return "neutral"


def key_levels(candles):
    """The prior candle's high and low = the range whose edges hold liquidity."""
    if len(candles) < 2:
        return None
    prior = candles[-2]
    return {"high": prior["high"], "low": prior["low"]}


def detect_sweep_reversal(trigger_candles, level, direction):
    """
    CRT trigger: did price SWEEP the level then REVERSE?
    • bull setup: price dips BELOW level['low'] (sweeps sell-side liquidity)
      then closes back ABOVE it → reversal up → LONG
    • bear setup: price pokes ABOVE level['high'] then closes back below.
      (We trade LONG-only swings, so we use the bull case.)
    Returns True if a valid bull sweep-reversal on the latest candle.
    """
    if len(trigger_candles) < 2 or not level:
        return False
    c = trigger_candles[-1]
    if direction == "bull":
        # wick swept below the level low, but candle closed back above it
        swept   = c["low"] < level["low"]
        reclaim = c["close"] > level["low"]
        green   = c["close"] > c["open"]
        return swept and reclaim and green
    return False


def crt_signal(symbol):
    """
    Full CRT check for a symbol. Returns a signal dict:
      {'signal': True/False, 'bias': ..., 'entry': price, 'reason': ...}
    LONG-only (swings). Requires daily+4h bias agree bull, then 15m sweep-reversal.
    """
    daily = fetch_daily(symbol)
    m30   = fetch_30m(symbol)
    m15   = fetch_15m(symbol)
    if len(daily) < 5 or len(m30) < 8 or len(m15) < 4:
        return {"signal": False, "reason": "insufficient_data"}

    h4 = aggregate(m30, 8)   # 30m x8 = 4h
    h1 = aggregate(m30, 2)   # 30m x2 = 1h

    daily_bias = get_bias(daily)
    h4_bias    = get_bias(h4) if h4 else "neutral"

    # Higher-timeframe bias must agree bullish (long-only swings)
    if daily_bias != "bull" or h4_bias not in ("bull", "neutral"):
        return {"signal": False, "bias": f"{daily_bias}/{h4_bias}",
                "reason": "htf_bias_not_bull"}

    # Key levels from 1h (the range we expect a sweep of)
    level = key_levels(h1)
    if not level:
        return {"signal": False, "reason": "no_level"}

    # 15m trigger: sweep + reversal
    if detect_sweep_reversal(m15, level, "bull"):
        return {"signal": True, "bias": f"{daily_bias}/{h4_bias}",
                "entry": m15[-1]["close"], "level": level,
                "reason": "crt_sweep_reversal_long"}

    return {"signal": False, "bias": f"{daily_bias}/{h4_bias}",
            "reason": "no_trigger"}


if __name__ == "__main__":
    import sys
    syms = sys.argv[1:] if len(sys.argv) > 1 else ["NVDA", "AAPL", "AMD"]
    for s in syms:
        print(f"{s}: {crt_signal(s)}")
        time.sleep(0.3)
