"""
DIAGNOSTIC BACKTEST — your scoring entries, deep detail on what to fix.

Beyond one net number, this shows:
  • WIN/LOSS PEAKS: biggest win/loss, avg win vs avg loss size
  • LEFT ON TABLE (MFE): how far each trade went in your favor before exit
    → high MFE not captured = exit too early, widen target/trail
  • MAE on winners: how far winners dipped before recovering
    → big MAE = stop too tight, cutting would-be winners
  • EXIT REASON breakdown: target / stop / trail / timeout counts
    → mostly stops = entries or stop-too-tight; mostly timeouts = target too far
  • LONG vs SHORT performance separately

Uses COMBO_5/10 (the best exit found). Returns NET of slippage.

Run locally:  python3 backtest_diagnostic.py
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


def simulate(entry, fwd, dir_):
    """
    Returns dict: net%, reason, mfe% (max favorable), mae% (max adverse).
    Long/short aware. COMBO_5/10 exit.
    """
    sign = 1 if dir_ == "bull" else -1
    hard = entry * (1 - HARD_STOP) if dir_ == "bull" else entry * (1 + HARD_STOP)
    tgt  = entry * (1 + TARGET) if dir_ == "bull" else entry * (1 - TARGET)
    peak = entry  # best price in our favor
    mfe = 0.0; mae = 0.0
    exit_ret = None; reason = "timeout"
    for c in fwd:
        px = c["close"]; lo = c.get("low", px); hi = c.get("high", px)
        # track favorable/adverse excursion (in our direction)
        fav = (hi - entry) / entry * 100 if dir_ == "bull" else (entry - lo) / entry * 100
        adv = (entry - lo) / entry * 100 if dir_ == "bull" else (hi - entry) / entry * 100
        mfe = max(mfe, fav); mae = max(mae, adv)
        if dir_ == "bull":
            peak = max(peak, hi)
            if lo <= hard:
                exit_ret = (hard - entry) / entry * 100; reason = "stop"; break
            if hi >= tgt:
                exit_ret = (tgt - entry) / entry * 100; reason = "target"; break
            if (px - entry) / entry >= BE_AT and px <= peak * (1 - TRAIL):
                exit_ret = (px - entry) / entry * 100; reason = "trail"; break
        else:
            peak = min(peak, lo)
            if hi >= hard:
                exit_ret = (entry - hard) / entry * 100; reason = "stop"; break
            if lo <= tgt:
                exit_ret = (entry - tgt) / entry * 100; reason = "target"; break
            if (entry - px) / entry >= BE_AT and px >= peak * (1 + TRAIL):
                exit_ret = (entry - px) / entry * 100; reason = "trail"; break
    if exit_ret is None:
        last = fwd[-1]["close"]
        exit_ret = ((last - entry) if dir_ == "bull" else (entry - last)) / entry * 100
    return {"net": exit_ret - ROUND_TRIP_COST, "reason": reason,
            "mfe": mfe, "mae": mae, "dir": dir_}


def run(symbols):
    print(f"\n{'='*64}")
    print("DIAGNOSTIC BACKTEST — what to fix")
    print(f"{'='*64}")
    print(f"Entries: score>=3.5 | Exit: COMBO 5/10 | NET\n")

    trades = []
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
                trades.append(simulate(entry, fwd, dir_)); cnt += 1
        if cnt:
            print(f"  {sym}: {cnt}")
        time.sleep(0.3)

    if not trades:
        print("No trades."); return

    nets = [t["net"] for t in trades]
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    longs = [t for t in trades if t["dir"] == "bull"]
    shorts = [t for t in trades if t["dir"] == "bear"]

    print(f"\n{'-'*64}")
    print(f"OVERALL: {len(trades)} trades | net total {sum(nets):+.1f}% | "
          f"avg {sum(nets)/len(trades):+.3f}%")
    print(f"Win rate: {len(wins)/len(trades)*100:.0f}%")

    print(f"\nWIN / LOSS PEAKS:")
    if wins:
        aw = sum(t['net'] for t in wins)/len(wins)
        print(f"  Avg WIN:  {aw:+.2f}%   Biggest win:  {max(t['net'] for t in wins):+.2f}%")
    if losses:
        al = sum(t['net'] for t in losses)/len(losses)
        print(f"  Avg LOSS: {al:+.2f}%   Biggest loss: {min(t['net'] for t in losses):+.2f}%")
    if wins and losses:
        rr = abs(aw/al)
        print(f"  Win/Loss size ratio: {rr:.2f}  (need > 1 to profit at <50% win rate)")

    print(f"\nLEFT ON THE TABLE (MFE — how far trades went before exit):")
    avg_mfe = sum(t['mfe'] for t in trades)/len(trades)
    win_mfe = sum(t['mfe'] for t in wins)/len(wins) if wins else 0
    print(f"  Avg peak reached: {avg_mfe:.2f}%  |  on winners: {win_mfe:.2f}%")
    captured = (sum(t['net'] for t in wins)/len(wins)) if wins else 0
    if win_mfe > 0:
        print(f"  Winners peaked {win_mfe:.2f}% but captured {captured:.2f}% "
              f"→ left ~{win_mfe-captured:.2f}% on table")
        if win_mfe - captured > 1.5:
            print(f"  >> FIX: exits too early. Widen target or loosen trail to capture more.")

    print(f"\nMAE ON WINNERS (how far winners dipped before recovering):")
    if wins:
        wmae = sum(t['mae'] for t in wins)/len(wins)
        print(f"  Avg winner dipped {wmae:.2f}% against entry first")
        if wmae > HARD_STOP*100*0.8:
            print(f"  >> Winners dip close to the {HARD_STOP*100:.0f}% stop — "
                  f"tighter stop would kill winners. Keep stop where it is.")

    print(f"\nEXIT REASONS:")
    for reason in ["target", "trail", "stop", "timeout"]:
        c = sum(1 for t in trades if t["reason"] == reason)
        print(f"  {reason:<8}: {c:>4} ({c/len(trades)*100:.0f}%)")
    stop_pct = sum(1 for t in trades if t['reason']=='stop')/len(trades)*100
    to_pct = sum(1 for t in trades if t['reason']=='timeout')/len(trades)*100
    if stop_pct > 40:
        print(f"  >> {stop_pct:.0f}% stopped out → ENTRIES are the problem (bad timing).")
    if to_pct > 40:
        print(f"  >> {to_pct:.0f}% timed out → target too far or holding too long.")

    print(f"\nLONG vs SHORT:")
    for name, grp in [("LONG", longs), ("SHORT", shorts)]:
        if grp:
            print(f"  {name}: {len(grp)} trades | avg {sum(t['net'] for t in grp)/len(grp):+.3f}% "
                  f"| win {sum(1 for t in grp if t['net']>0)/len(grp)*100:.0f}%")
        else:
            print(f"  {name}: 0 trades")

    print(f"{'='*64}\n")


if __name__ == "__main__":
    syms = sys.argv[1:] if len(sys.argv) > 1 else \
        ["NVDA", "TSLA", "AMZN", "MSFT", "GOOGL", "META", "SMCI", "CVNA", "SHOP",
         "AMD", "PLTR", "COIN", "AVGO", "CRWD", "MU", "NFLX", "UBER", "HOOD", "RIOT", "MARA"]
    run(syms)
