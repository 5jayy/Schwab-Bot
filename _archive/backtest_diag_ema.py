"""
DIAGNOSTIC v2 — daily EMA 200 bias (better direction) + full detail.

KEY CHANGE: the 4H/daily direction now uses the DAILY EMA 200 trend filter
instead of the noisy 3-candle higher-high/higher-low check.
  • price > EMA200 = bull bias   • price < EMA200 = bear bias
This is a real, established-trend filter — the theory being that entering only
stocks in a genuine daily uptrend/downtrend means trades actually MOVE (fixing
the 84% timeout), instead of entering choppy stocks that drift.

Pulls 1 year of daily (for a real EMA200) + 10 days of 5m/30m for entry timing.
Shows: win/loss peaks, MFE (left on table), exit reasons, long/short, timeout%.
Uses COMBO 5/10 exit. NET of slippage.

Run locally:  python3 backtest_diag_ema.py
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
TARGET    = 0.10
TRAIL     = 0.05
BE_AT     = 0.02

W_4H, W_1H, W_5M, W_OB, W_WICK, W_INSIGHT = 1.0, 1.0, 1.0, 0.5, 0.5, 0.25
THRESHOLD = 3.5
EMA_LEN = 200


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def fetch_daily_year(symbol):
    """Full year of daily candles for a real EMA200."""
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
    e = sum(values[:length]) / length  # seed with SMA
    for v in values[length:]:
        e = v * k + e * (1 - k)
    return e


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
    """EMA200 trend filter: price vs EMA200."""
    closes = [c["close"] for c in daily]
    e = ema(closes, EMA_LEN)
    if e is None:
        # fallback to shorter EMA if not enough history
        e = ema(closes, 50)
        if e is None:
            return "flat"
    price = closes[-1]
    if price > e * 1.01:   # >1% above = clear bull
        return "bull"
    if price < e * 0.99:   # >1% below = clear bear
        return "bear"
    return "flat"          # too close to EMA = no clear trend


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
    """4H/direction now = daily EMA200 bias (passed in as dbias)."""
    if dbias == "flat":
        return 0, None
    dir_ = dbias
    s = W_4H  # daily EMA200 trend present (required)
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


def simulate(entry, fwd, dir_):
    sign_hard = (1 - HARD_STOP) if dir_ == "bull" else (1 + HARD_STOP)
    hard = entry * sign_hard
    tgt = entry * (1 + TARGET) if dir_ == "bull" else entry * (1 - TARGET)
    peak = entry; mfe = 0.0; mae = 0.0; exit_ret = None; reason = "timeout"
    for c in fwd:
        px = c["close"]; lo = c.get("low", px); hi = c.get("high", px)
        fav = (hi - entry) / entry * 100 if dir_ == "bull" else (entry - lo) / entry * 100
        adv = (entry - lo) / entry * 100 if dir_ == "bull" else (hi - entry) / entry * 100
        mfe = max(mfe, fav); mae = max(mae, adv)
        if dir_ == "bull":
            peak = max(peak, hi)
            if lo <= hard: exit_ret = -HARD_STOP*100; reason = "stop"; break
            if hi >= tgt: exit_ret = TARGET*100; reason = "target"; break
            if (px-entry)/entry >= BE_AT and px <= peak*(1-TRAIL):
                exit_ret = (px-entry)/entry*100; reason = "trail"; break
        else:
            peak = min(peak, lo)
            if hi >= hard: exit_ret = -HARD_STOP*100; reason = "stop"; break
            if lo <= tgt: exit_ret = TARGET*100; reason = "target"; break
            if (entry-px)/entry >= BE_AT and px >= peak*(1+TRAIL):
                exit_ret = (entry-px)/entry*100; reason = "trail"; break
    if exit_ret is None:
        last = fwd[-1]["close"]
        exit_ret = ((last-entry) if dir_ == "bull" else (entry-last))/entry*100
    return {"net": exit_ret - ROUND_TRIP_COST, "reason": reason,
            "mfe": mfe, "mae": mae, "dir": dir_}


def run(symbols):
    print(f"\n{'='*64}")
    print("DIAGNOSTIC v2 — daily EMA200 bias")
    print(f"{'='*64}")
    print(f"Bias: daily EMA{EMA_LEN} | Exit: COMBO 5/10 | NET\n")

    trades = []
    bias_report = []
    for sym in symbols:
        daily = fetch_daily_year(sym)
        if len(daily) < 60:
            continue
        dbias = daily_bias(daily)
        bias_report.append(f"{sym}={dbias}")
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
                trades.append(simulate(entry, fwd, dir_)); cnt += 1
        if cnt:
            print(f"  {sym}: {cnt} ({dbias})")
        time.sleep(0.3)

    print(f"\n  EMA200 bias: {', '.join(bias_report)}")

    if not trades:
        print("\nNo trades — EMA200 filter rejected all (may be too strict, "
              "or few stocks in clear trend). That itself is informative.")
        return

    nets = [t["net"] for t in trades]
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    longs = [t for t in trades if t["dir"] == "bull"]
    shorts = [t for t in trades if t["dir"] == "bear"]

    print(f"\n{'-'*64}")
    print(f"OVERALL: {len(trades)} trades | net total {sum(nets):+.1f}% | avg {sum(nets)/len(trades):+.3f}%")
    print(f"Win rate: {len(wins)/len(trades)*100:.0f}%")

    print(f"\nWIN/LOSS PEAKS:")
    if wins:
        aw = sum(t['net'] for t in wins)/len(wins)
        print(f"  Avg WIN:  {aw:+.2f}%  Biggest: {max(t['net'] for t in wins):+.2f}%")
    if losses:
        al = sum(t['net'] for t in losses)/len(losses)
        print(f"  Avg LOSS: {al:+.2f}%  Biggest: {min(t['net'] for t in losses):+.2f}%")
    if wins and losses:
        print(f"  Win/Loss ratio: {abs(aw/al):.2f}")

    print(f"\nEXIT REASONS:")
    for reason in ["target", "trail", "stop", "timeout"]:
        c = sum(1 for t in trades if t["reason"] == reason)
        print(f"  {reason:<8}: {c:>4} ({c/len(trades)*100:.0f}%)")
    to_pct = sum(1 for t in trades if t['reason']=='timeout')/len(trades)*100

    print(f"\nLONG vs SHORT:")
    for name, grp in [("LONG", longs), ("SHORT", shorts)]:
        if grp:
            print(f"  {name}: {len(grp)} | avg {sum(t['net'] for t in grp)/len(grp):+.3f}% "
                  f"| win {sum(1 for t in grp if t['net']>0)/len(grp)*100:.0f}%")
        else:
            print(f"  {name}: 0")

    print(f"\n{'-'*64}")
    print("COMPARE TO OLD BIAS (prior run was -0.166% avg, 84% timeout):")
    print(f"  This run: {sum(nets)/len(trades):+.3f}% avg, {to_pct:.0f}% timeout")
    if sum(nets)/len(trades) > -0.166:
        print("  -> EMA200 bias IMPROVED it. Better direction = fewer dead trades.")
    else:
        print("  -> EMA200 bias didn't improve avg here (small sample caveat).")
    print(f"{'='*64}\n")


if __name__ == "__main__":
    syms = sys.argv[1:] if len(sys.argv) > 1 else \
        ["NVDA", "TSLA", "AMZN", "MSFT", "GOOGL", "META", "SMCI", "CVNA", "SHOP",
         "AMD", "PLTR", "COIN", "AVGO", "CRWD", "MU", "NFLX", "UBER", "HOOD", "RIOT", "MARA"]
    run(syms)
