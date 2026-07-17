"""
Circuit Analytics Backtest
Answers exactly what's between you and $80/day.

Metrics:
1. Avg move captured % — catching real moves or noise?
2. Projected $/day vs $80 target
3. TO FIX section — bottleneck analysis
4. Per-conviction move % — does higher conviction = bigger moves?

Run: python3 analytics.py
"""

import requests
import time
import json
import os
from datetime import datetime, timedelta
from auth import get_valid_token

BASE_URL    = "https://api.schwabapi.com/marketdata/v1"
LEDGER_PATH = "/data/trade_ledger.json" if os.path.exists("/data") else "trade_ledger.json"

# Your current numbers
BOT_CAPITAL    = 2220.0
DAY_BUCKET     = BOT_CAPITAL * 0.25
SWING_BUCKET   = BOT_CAPITAL * 0.75
DAY_CEILING    = 148.0   # 40% of day room
SWING_CEILING  = 200.0   # room-based
STOP_PCT_DAY   = 0.05
STOP_PCT_SWING = 0.07
TARGET_DAILY   = 80.0


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_candles(symbol: str, period: int = 10, frequency: int = 30) -> list:
    try:
        resp = requests.get(
            f"{BASE_URL}/pricehistory", headers=headers(),
            params={"symbol": symbol, "periodType": "day", "period": period,
                    "frequencyType": "minute", "frequency": frequency,
                    "needExtendedHoursData": False},
            timeout=15
        )
        return resp.json().get("candles", []) if resp.ok else []
    except Exception:
        return []


def get_daily_candles(symbol: str) -> list:
    try:
        resp = requests.get(
            f"{BASE_URL}/pricehistory", headers=headers(),
            params={"symbol": symbol, "periodType": "month", "period": 1,
                    "frequencyType": "daily", "frequency": 1,
                    "needExtendedHoursData": False},
            timeout=15
        )
        return resp.json().get("candles", []) if resp.ok else []
    except Exception:
        return []


def simulate_trade(candles: list, entry_idx: int,
                   stop_pct: float, target_pct: float,
                   tp1_pct: float, tp2_pct: float) -> dict:
    """
    Simulate a trade from entry_idx.
    Uses scale-out: 1/3 at TP1, 1/3 at TP2, 1/3 trail.
    Returns move captured, MFE, MAE, exit type.
    """
    if entry_idx >= len(candles) - 1:
        return {}

    entry_px  = candles[entry_idx + 1]["open"]
    stop_px   = entry_px * (1 - stop_pct)
    tp1_px    = entry_px * (1 + tp1_pct)
    tp2_px    = entry_px * (1 + tp2_pct)
    target_px = entry_px * (1 + target_pct)

    mfe       = 0  # max favorable excursion
    mae       = 0  # max adverse excursion
    tp1_hit   = False
    tp2_hit   = False
    exit_px   = None
    exit_type = "timeout"
    trail_hi  = entry_px

    for i in range(entry_idx + 1, min(entry_idx + 40, len(candles))):
        c = candles[i]
        hi = c["high"]
        lo = c["low"]

        # Track MFE and MAE
        mfe = max(mfe, (hi - entry_px) / entry_px * 100)
        mae = max(mae, (entry_px - lo) / entry_px * 100)
        trail_hi = max(trail_hi, hi)

        # TP1
        if not tp1_hit and hi >= tp1_px:
            tp1_hit = True

        # TP2
        if tp1_hit and not tp2_hit and hi >= tp2_px:
            tp2_hit = True

        # Trail stop on remaining 1/3
        trail_stop = trail_hi * (1 - stop_pct * 0.7)

        # Stop hit
        if lo <= stop_px:
            exit_px   = stop_px
            exit_type = "stop"
            break

        # Target hit (if no scale-out)
        if hi >= target_px:
            exit_px   = target_px
            exit_type = "target"
            break

        # Trail stop (after TP2)
        if tp2_hit and lo <= trail_stop:
            exit_px   = trail_stop
            exit_type = "trail"
            break

    if exit_px is None:
        exit_px   = candles[min(entry_idx + 39, len(candles)-1)]["close"]
        exit_type = "timeout"

    # Weighted average exit (scale-out)
    if tp1_hit and tp2_hit:
        avg_exit = (tp1_px * 0.333 + tp2_px * 0.333 + exit_px * 0.334)
    elif tp1_hit:
        avg_exit = (tp1_px * 0.333 + exit_px * 0.667)
    else:
        avg_exit = exit_px

    move_pct = (avg_exit - entry_px) / entry_px * 100

    return {
        "entry":      entry_px,
        "exit":       round(avg_exit, 2),
        "move_pct":   round(move_pct, 2),
        "mfe":        round(mfe, 2),
        "mae":        round(mae, 2),
        "tp1_hit":    tp1_hit,
        "tp2_hit":    tp2_hit,
        "exit_type":  exit_type,
    }


def check_day_signal(candles: list, i: int) -> tuple:
    """Check 4H/15m/5m/1m alignment. Returns (conviction, score)."""
    if len(candles) < i + 20:
        return 0, 0
    window = candles[:i+1]
    closes = [c["close"] for c in window]
    ma20   = sum(closes[-20:]) / 20 if len(closes) >= 20 else closes[-1]
    ma10   = sum(closes[-10:]) / 10 if len(closes) >= 10 else closes[-1]

    # Candle strength
    c     = candles[i]
    rng   = c["high"] - c["low"]
    body  = abs(c["close"] - c["open"])
    strength = body / rng if rng > 0 else 0

    # Tighter conviction — require volume spike too
    vols    = [c["volume"] for c in candles[:i+1]][-20:]
    avg_vol = sum(vols[:-1]) / len(vols[:-1]) if len(vols) > 1 else 0
    vol_spike = candles[i]["volume"] > avg_vol * 1.3 if avg_vol > 0 else False

    if closes[-1] > ma20 and closes[-1] > ma10 and strength > 0.6 and vol_spike:
        return 4, strength * 10
    elif closes[-1] > ma20 and strength > 0.5 and vol_spike:
        return 3, strength * 8
    return 0, 0


def run_analytics(symbols: list = None, days: int = 10):
    if symbols is None:
        symbols = [
            "NVDA", "AAPL", "MSFT", "AMD", "TSLA",
            "COIN", "PLTR", "SOFI", "BAC", "DKNG",
            "UBER", "SNAP", "RIOT", "MARA", "META"
        ]

    print(f"\n{'='*55}")
    print(f"CIRCUIT ANALYTICS BACKTEST — {days}d | {len(symbols)} symbols")
    print(f"{'='*55}")
    print(f"Bot capital: ${BOT_CAPITAL:,.0f} | Target: ${TARGET_DAILY}/day")
    print(f"Day: ${DAY_BUCKET:,.0f} (25%) | Swing: ${SWING_BUCKET:,.0f} (75%)\n")

    all_trades  = []
    per_conv    = {4: [], 3: [], 2: []}
    missed_cnt  = 0
    taken_cnt   = 0

    for sym in symbols:
        candles = get_candles(sym, days, 30)
        if len(candles) < 30:
            print(f"  {sym}: not enough data")
            continue

        in_trade = False
        for i in range(20, len(candles) - 2):
            conv, score = check_day_signal(candles, i)
            if conv < 3:
                continue

            if in_trade:
                missed_cnt += 1
                continue

            taken_cnt += 1
            result = simulate_trade(
                candles, i,
                stop_pct   = STOP_PCT_DAY * 0.6,   # tighter stop 4.2%
                target_pct = STOP_PCT_DAY * 4.0,   # wider target
                tp1_pct    = STOP_PCT_DAY * 2.0,   # 2R
                tp2_pct    = STOP_PCT_DAY * 4.0,   # 4R
            )
            if not result:
                continue

            result["symbol"]     = sym
            result["conviction"] = conv
            all_trades.append(result)
            per_conv[conv].append(result["move_pct"])
            in_trade = result["exit_type"] in ("stop",)
            in_trade = False  # reset each candle for backtest

        time.sleep(0.2)

    if not all_trades:
        print("No trades found.")
        return

    # ── Aggregate ──
    wins   = [t for t in all_trades if t["move_pct"] > 0]
    losses = [t for t in all_trades if t["move_pct"] <= 0]
    total  = len(all_trades)

    win_rate  = len(wins) / total * 100 if total > 0 else 0
    avg_win   = sum(t["move_pct"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss  = sum(t["move_pct"] for t in losses) / len(losses) if losses else 0
    avg_move  = sum(t["move_pct"] for t in all_trades) / total
    avg_mfe   = sum(t["mfe"]      for t in all_trades) / total
    avg_mae   = sum(t["mae"]      for t in all_trades) / total
    rr        = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    pf        = abs(sum(t["move_pct"] for t in wins) / sum(t["move_pct"] for t in losses)) if losses else 999

    # ── Projected $/day ──
    avg_position  = DAY_CEILING * 0.75  # avg between 4/4 full and 3/4 half
    avg_dollar    = avg_position * (avg_move / 100)
    trades_per_day = taken_cnt / days
    projected_day  = avg_dollar * trades_per_day

    print(f"{'='*55}")
    print(f"RESULTS — {total} trades | {taken_cnt} taken | {missed_cnt} missed")
    print(f"{'='*55}")
    print(f"Win rate:       {win_rate:.1f}%")
    print(f"Avg win:        +{avg_win:.2f}%")
    print(f"Avg loss:       {avg_loss:.2f}%")
    print(f"R:R ratio:      {rr:.2f}")
    print(f"Profit factor:  {pf:.2f}")
    print(f"Avg MFE:        +{avg_mfe:.2f}% (peak move)")
    print(f"Avg MAE:        -{avg_mae:.2f}% (worst dip)")
    print(f"\n── Metric 1: Avg Move Captured ──")
    print(f"Avg move:       {avg_move:.2f}%")
    if avg_move < 0.3:
        print(f"  ⚠️  Under 0.3% — catching noise, need to hold longer")
    elif avg_move < 1.0:
        print(f"  ✅ Real moves but room to grow")
    else:
        print(f"  ✅ Strong move capture")

    print(f"\n── Metric 2: Projected $/day ──")
    print(f"Avg position:   ${avg_position:,.0f}")
    print(f"Avg $/trade:    ${avg_dollar:,.2f}")
    print(f"Trades/day:     {trades_per_day:.1f}")
    print(f"Projected/day:  ${projected_day:,.2f}")
    print(f"Target/day:     ${TARGET_DAILY:,.0f}")
    gap = TARGET_DAILY - projected_day
    if gap > 0:
        print(f"Gap to target:  ${gap:,.2f}/day")
    else:
        print(f"  ✅ ON PACE for ${TARGET_DAILY}/day target!")

    print(f"\n── Metric 3: TO FIX ──")
    bottleneck = []
    if win_rate < 50:
        bottleneck.append(f"WIN RATE ({win_rate:.0f}%) — need 55%+. Tighten entry: require FVG + 4/4 only")
    if rr < 1.5:
        bottleneck.append(f"R:R ({rr:.2f}) — need 1.5+. Let TP2 run further or tighten stop")
    if avg_move < 0.5:
        bottleneck.append(f"MOVE CAPTURE ({avg_move:.2f}%) — exits too early. Widen TP2 to 3x stop")
    if avg_position < DAY_CEILING * 0.8:
        needed = TARGET_DAILY / max(trades_per_day, 0.1) / max(avg_move/100, 0.001)
        bottleneck.append(f"SIZING — need ~${needed:,.0f} avg position. Check drawdown room first")
    if not bottleneck:
        print(f"  ✅ No major bottlenecks — scale capital to hit ${TARGET_DAILY}/day")
    else:
        for b in bottleneck:
            print(f"  ⚠️  {b}")

    print(f"\n── Metric 4: Per-Conviction Move % ──")
    for conv in [4, 3, 2]:
        moves = per_conv.get(conv, [])
        if moves:
            avg = sum(moves) / len(moves)
            print(f"  {conv}/4: avg {avg:+.2f}% ({len(moves)} trades)")
        else:
            print(f"  {conv}/4: no trades")

    if len(per_conv.get(4, [])) > 0 and len(per_conv.get(3, [])) > 0:
        avg4 = sum(per_conv[4]) / len(per_conv[4])
        avg3 = sum(per_conv[3]) / len(per_conv[3])
        if avg4 > avg3 * 1.2:
            print(f"  ✅ 4/4 captures {((avg4/avg3)-1)*100:.0f}% more — raising threshold helps")
        else:
            print(f"  ⚠️  4/4 not much better than 3/4 — threshold doesn't matter much")

    print(f"\n{'='*55}\n")

    # Save results
    output = {
        "run_date":      datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_trades":  total,
        "taken":         taken_cnt,
        "missed":        missed_cnt,
        "win_rate":      round(win_rate, 1),
        "avg_move_pct":  round(avg_move, 2),
        "avg_mfe":       round(avg_mfe, 2),
        "avg_mae":       round(avg_mae, 2),
        "rr":            round(rr, 2),
        "profit_factor": round(pf, 2),
        "projected_day": round(projected_day, 2),
        "target_day":    TARGET_DAILY,
        "per_conviction": {str(k): round(sum(v)/len(v), 2) if v else 0 for k, v in per_conv.items()},
    }
    path = "/data/analytics_results.json" if os.path.exists("/data") else "analytics_results.json"
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Results saved to {path}")
    return output


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    run_analytics(days=days)
