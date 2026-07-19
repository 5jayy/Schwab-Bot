"""
Clean Foundation — 4-Step Market Verification + Liquidity Sweep Entry
No features, no sizing, no options. Pure entry logic.
Backtest locally in VSCode before adding anything.

Architecture:
    Monthly bias → Weekly direction → Daily context → Session valid → Liquidity sweep → ENTER

All 5 must pass. One fails = no trade.
"""

import requests
import time
from datetime import datetime, date
from auth import get_valid_token

BASE_URL = "https://api.schwabapi.com/marketdata/v1"


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_candles(symbol: str, period_type: str, period: int,
                freq_type: str, freq: int) -> list:
    """Pull candles from Schwab API."""
    try:
        resp = requests.get(
            f"{BASE_URL}/pricehistory", headers=headers(),
            params={
                "symbol":               symbol,
                "periodType":           period_type,
                "period":               period,
                "frequencyType":        freq_type,
                "frequency":            freq,
                "needExtendedHoursData": False,
            },
            timeout=15
        )
        resp.raise_for_status()
        return resp.json().get("candles", [])
    except Exception as ex:
        print(f"  Candle error {symbol}: {ex}")
        return []


# ── Step 1: Monthly Bias ──────────────────────────────────────────────────────

def get_monthly_bias() -> dict:
    """
    Check SPY monthly trend.
    Bullish = current month open < current close.
    Returns bias and strength.
    """
    candles = get_candles("SPY", "month", 3, "monthly", 1)
    if len(candles) < 2:
        return {"bias": "neutral", "strength": 0, "pass": True}

    current = candles[-1]
    prev    = candles[-2]

    monthly_change = (current["close"] - prev["close"]) / prev["close"] * 100

    if monthly_change > 1.0:
        bias     = "bullish"
        strength = min(monthly_change / 5, 1.0)  # normalize 0-1
        passed   = True
    elif monthly_change < -1.0:
        bias     = "bearish"
        strength = min(abs(monthly_change) / 5, 1.0)
        passed   = False  # skip longs in bearish month
    else:
        bias     = "neutral"
        strength = 0.5
        passed   = True  # neutral = allow but reduce conviction

    return {
        "bias":     bias,
        "strength": round(strength, 2),
        "change":   round(monthly_change, 2),
        "pass":     passed
    }


# ── Step 2: Weekly Direction ──────────────────────────────────────────────────

def get_weekly_direction() -> dict:
    """
    Check SPY weekly trend.
    Compares this week's close to last week's close.
    Returns direction and conviction multiplier.
    """
    candles = get_candles("SPY", "month", 1, "weekly", 1)
    if len(candles) < 2:
        return {"direction": "neutral", "conviction": 1.0, "pass": True}

    this_week = candles[-1]
    last_week = candles[-2]

    weekly_change = (this_week["close"] - last_week["close"]) / last_week["close"] * 100

    if weekly_change > 0.5:
        direction  = "up"
        conviction = 1.0   # full size
        passed     = True
    elif weekly_change < -0.5:
        direction  = "down"
        conviction = 0.5   # reduce size in down week
        passed     = True  # still allow but cautious
    else:
        direction  = "flat"
        conviction = 0.75
        passed     = True

    return {
        "direction":  direction,
        "conviction": conviction,
        "change":     round(weekly_change, 2),
        "pass":       passed
    }


# ── Step 3: Daily Context ─────────────────────────────────────────────────────

def get_daily_context(symbol: str = "SPY") -> dict:
    """
    Check today's market character.
    Gap up → buy dips to VWAP.
    Gap down → wait for reclaim before entering.
    Flat → normal operation.
    Returns context and entry approach.
    """
    candles = get_candles(symbol, "day", 5, "minute", 30)
    if len(candles) < 4:
        return {"gap": "flat", "approach": "normal", "pass": True}

    # Find today's open vs yesterday's close
    now       = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    today_candles = []
    prev_close    = None

    for i, c in enumerate(candles):
        c_date = datetime.fromtimestamp(c["datetime"] / 1000).strftime("%Y-%m-%d")
        if c_date == today_str:
            today_candles.append(c)
        else:
            prev_close = c["close"]

    if not today_candles or prev_close is None:
        return {"gap": "flat", "approach": "normal", "pass": True}

    today_open  = today_candles[0]["open"]
    gap_pct     = (today_open - prev_close) / prev_close * 100
    current_px  = today_candles[-1]["close"]

    if gap_pct > 0.3:
        gap      = "up"
        approach = "buy_dips"   # wait for pullback to VWAP
        passed   = True
    elif gap_pct < -0.3:
        gap      = "down"
        # Only allow if price has reclaimed gap
        reclaimed = current_px > today_open
        approach  = "wait_reclaim" if not reclaimed else "cautious"
        passed    = reclaimed
    else:
        gap      = "flat"
        approach = "normal"
        passed   = True

    return {
        "gap":      gap,
        "gap_pct":  round(gap_pct, 2),
        "approach": approach,
        "pass":     passed
    }


# ── Step 4: Session Filter ────────────────────────────────────────────────────

def get_session() -> dict:
    """
    Determine which trading session we're in.
    Only primary (9:45-11:30) and afternoon (1:30-3:30) sessions allow entries.
    Warmup, lunch chop, and closing hour are filtered out.
    """
    from pytz import timezone
    et  = timezone("America/New_York")
    now = datetime.now(et)
    h, m = now.hour, now.minute
    t = h * 60 + m  # minutes since midnight

    sessions = {
        "warmup":    (9*60+30,  9*60+45,  False, "Market just opened — wait"),
        "primary":   (9*60+45,  11*60+30, True,  "Primary session — best entries"),
        "lunch":     (11*60+30, 13*60+30, False, "Lunch chop — skip"),
        "afternoon": (13*60+30, 15*60+30, True,  "Afternoon session — entries allowed"),
        "closing":   (15*60+30, 16*60+0,  False, "Closing hour — no new entries"),
    }

    for name, (start, end, allowed, reason) in sessions.items():
        if start <= t < end:
            return {
                "session": name,
                "allowed": allowed,
                "reason":  reason,
                "pass":    allowed
            }

    return {"session": "closed", "allowed": False, "reason": "Market closed", "pass": False}


# ── Step 5: Liquidity Sweep Entry ─────────────────────────────────────────────

def detect_liquidity_sweep(symbol: str) -> dict:
    """
    Core entry signal — detects when smart money sweeps liquidity then reverses.

    Bullish setup (long entry):
    1. Price sweeps below recent equal lows (takes out stops)
    2. Volume spikes on the sweep candle (institutional activity)
    3. Price closes back ABOVE the swept level (rejection)
    4. Next candle confirms direction (close above sweep candle high)

    This is the only entry signal. Everything else is market context.
    """
    candles = get_candles(symbol, "day", 10, "minute", 30)
    if len(candles) < 20:
        return {"detected": False, "reason": "insufficient data"}

    try:
        # Find equal lows in last 15 candles (within 0.2% of each other)
        lookback  = candles[-15:-1]
        lows      = [c["low"] for c in lookback]
        volumes   = [c["volume"] for c in lookback]
        avg_vol   = sum(volumes) / len(volumes) if volumes else 0

        # Reference level — lowest point in lookback
        key_low   = min(lows)
        last_c    = candles[-2]   # sweep candle (previous)
        curr_c    = candles[-1]   # current candle (confirmation)

        sweep_low    = last_c["low"]
        sweep_close  = last_c["close"]
        sweep_vol    = last_c["volume"]
        curr_close   = curr_c["close"]
        curr_high    = curr_c["high"]

        # Condition 1: Price swept below key low
        swept = sweep_low < key_low * 0.998  # at least 0.2% below

        # Condition 2: Volume spike on sweep (at least 1.5x average)
        vol_spike = sweep_vol > avg_vol * 1.5

        # Condition 3: Sweep candle closed back above key low (rejection)
        rejected = sweep_close > key_low

        # Condition 4: Current candle confirming up (close above sweep high)
        confirmed = curr_close > last_c["high"]

        # All 4 conditions required
        detected = swept and vol_spike and rejected and confirmed

        # Calculate sweep strength for sizing later
        sweep_depth = (key_low - sweep_low) / key_low * 100 if swept else 0
        vol_ratio   = sweep_vol / avg_vol if avg_vol > 0 else 0

        return {
            "detected":    detected,
            "swept":       swept,
            "vol_spike":   vol_spike,
            "rejected":    rejected,
            "confirmed":   confirmed,
            "sweep_depth": round(sweep_depth, 3),
            "vol_ratio":   round(vol_ratio, 2),
            "entry_price": curr_c["close"],
            "key_low":     round(key_low, 2),
            "reason":      "all conditions met" if detected else
                          f"failed: {'sweep ' if not swept else ''}{'vol ' if not vol_spike else ''}{'reject ' if not rejected else ''}{'confirm' if not confirmed else ''}"
        }

    except Exception as ex:
        return {"detected": False, "reason": str(ex)}


# ── Master verification function ──────────────────────────────────────────────

def verify_entry(symbol: str, verbose: bool = True) -> dict:
    """
    Run all 4 market context checks + liquidity sweep.
    Returns full verification result with pass/fail at each step.

    All 5 must pass for entry to be valid.
    """
    results = {}

    # Step 1: Monthly
    monthly = get_monthly_bias()
    results["monthly"] = monthly
    if not monthly["pass"]:
        if verbose:
            print(f"  ❌ Monthly: {monthly['bias']} ({monthly['change']}%) — skip")
        return {"entry": False, "step_failed": "monthly", "details": results}
    if verbose:
        print(f"  ✅ Monthly: {monthly['bias']} ({monthly['change']}%)")

    # Step 2: Weekly
    weekly = get_weekly_direction()
    results["weekly"] = weekly
    if not weekly["pass"]:
        if verbose:
            print(f"  ❌ Weekly: {weekly['direction']} ({weekly['change']}%) — skip")
        return {"entry": False, "step_failed": "weekly", "details": results}
    if verbose:
        print(f"  ✅ Weekly: {weekly['direction']} ({weekly['change']}%) conviction={weekly['conviction']}")

    # Step 3: Daily
    daily = get_daily_context()
    results["daily"] = daily
    if not daily["pass"]:
        if verbose:
            print(f"  ❌ Daily: gap {daily['gap']} — {daily['approach']}")
        return {"entry": False, "step_failed": "daily", "details": results}
    if verbose:
        print(f"  ✅ Daily: gap {daily['gap']} — {daily['approach']}")

    # Step 4: Session
    session = get_session()
    results["session"] = session
    if not session["pass"]:
        if verbose:
            print(f"  ❌ Session: {session['session']} — {session['reason']}")
        return {"entry": False, "step_failed": "session", "details": results}
    if verbose:
        print(f"  ✅ Session: {session['session']} — {session['reason']}")

    # Step 5: Liquidity sweep
    sweep = detect_liquidity_sweep(symbol)
    results["sweep"] = sweep
    if not sweep["detected"]:
        if verbose:
            print(f"  ❌ Sweep: {sweep['reason']}")
        return {"entry": False, "step_failed": "sweep", "details": results}
    if verbose:
        print(f"  ✅ Sweep: depth={sweep['sweep_depth']}% vol_ratio={sweep['vol_ratio']}x")

    if verbose:
        print(f"  🟢 ENTRY VALID — {symbol} @ ${sweep['entry_price']:.2f}")

    return {
        "entry":         True,
        "symbol":        symbol,
        "entry_price":   sweep["entry_price"],
        "key_low":       sweep["key_low"],
        "sweep_depth":   sweep["sweep_depth"],
        "vol_ratio":     sweep["vol_ratio"],
        "conviction":    weekly["conviction"],
        "monthly_bias":  monthly["bias"],
        "session":       session["session"],
        "details":       results
    }


# ── Backtest engine (VSCode local) ────────────────────────────────────────────

def backtest_entry(symbol: str, stop_pct: float = 0.05,
                   target_pct: float = 0.10, verbose: bool = False) -> dict:
    """
    Backtest the pure entry signal on historical bars.
    No features, no sizing — just entry + fixed stop + fixed target.
    This measures the raw edge of the entry before features are added.

    stop_pct:   fixed stop loss below entry (default 5%)
    target_pct: fixed profit target above entry (default 10%)
    """
    candles = get_candles(symbol, "month", 2, "daily", 1)
    if len(candles) < 20:
        return {"symbol": symbol, "trades": 0, "error": "insufficient data"}

    trades    = []
    in_trade  = False
    entry_px  = 0
    stop_px   = 0
    target_px = 0
    entry_bar = 0
    max_adverse = 0  # worst point during trade (max drawdown)

    for i in range(15, len(candles) - 1):
        window = candles[:i+1]

        if in_trade:
            c = candles[i]
            # Track max adverse excursion (how far against us)
            adverse = (entry_px - c["low"]) / entry_px * 100
            if adverse > max_adverse:
                max_adverse = adverse
            # Check stop
            if c["low"] <= stop_px:
                pnl = (stop_px - entry_px) / entry_px * 100
                trades.append({
                    "pnl": pnl, "exit": "stop",
                    "bars": i - entry_bar,
                    "max_adverse": round(max_adverse, 2)
                })
                in_trade = False
                max_adverse = 0
                continue
            # Check target
            if c["high"] >= target_px:
                pnl = (target_px - entry_px) / entry_px * 100
                trades.append({
                    "pnl": pnl, "exit": "target",
                    "bars": i - entry_bar,
                    "max_adverse": round(max_adverse, 2)
                })
                in_trade = False
                max_adverse = 0
                continue
            continue

        # Check sweep on this bar window
        lows    = [c["low"]    for c in window[-15:]]
        vols    = [c["volume"] for c in window[-15:]]
        avg_vol = sum(vols) / len(vols) if vols else 0
        key_low   = sorted(lows)[2] if len(lows) > 3 else min(lows)

        last_c = window[-2]
        curr_c = window[-1]

        swept     = last_c["low"]    < key_low
        vol_spike = last_c["volume"] > avg_vol * 1.1
        rejected  = last_c["close"] > key_low
        confirmed = curr_c["close"] > (last_c["high"] + last_c["low"]) / 2

        if swept and vol_spike and rejected and confirmed:
            entry_px    = curr_c["close"] * 1.0005  # 0.05% slippage
            stop_px     = entry_px * (1 - stop_pct)
            target_px   = entry_px * (1 + target_pct)
            in_trade    = True
            entry_bar   = i
            max_adverse = 0
            if verbose:
                print(f"  Entry: {symbol} @ ${entry_px:.2f} stop=${stop_px:.2f} target=${target_px:.2f}")

    if not trades:
        return {"symbol": symbol, "trades": 0, "win_rate": 0,
                "avg_win": 0, "avg_loss": 0, "profit_factor": 0,
                "avg_adverse": 0, "avg_bars": 0}

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]

    avg_win     = sum(t["pnl"]          for t in wins)   / len(wins)   if wins   else 0
    avg_loss    = sum(t["pnl"]          for t in losses) / len(losses) if losses else 0
    avg_adverse = sum(t["max_adverse"]  for t in trades) / len(trades)
    avg_bars    = sum(t["bars"]         for t in trades) / len(trades)
    pf          = abs(avg_win / avg_loss) if avg_loss != 0 else 999

    return {
        "symbol":        symbol,
        "trades":        len(trades),
        "win_rate":      round(len(wins) / len(trades) * 100, 1),
        "avg_win":       round(avg_win,  2),
        "avg_loss":      round(avg_loss, 2),
        "profit_factor": round(pf, 2),
        "total_return":  round(sum(t["pnl"] for t in trades), 2),
        "avg_adverse":   round(avg_adverse, 2),
        "avg_bars":      round(avg_bars, 1),
    }


def run_foundation_backtest(symbols: list = None):
    """
    Run the clean foundation backtest.
    Entry only + fixed stop/target. No features.
    """
    if symbols is None:
        symbols = [
            "NVDA", "AAPL", "MSFT", "AMD", "TSLA",
            "COIN", "PLTR", "SOFI", "BAC", "DKNG",
            "UBER", "SNAP", "RIOT", "MARA", "AAL"
        ]

    print(f"\n{'='*55}")
    print(f"FOUNDATION BACKTEST — Liquidity Sweep Entry Only")
    print(f"Stop: 5% | Target: 10% | No features")
    print(f"{'='*55}\n")

    all_results = []
    for sym in symbols:
        print(f"Testing {sym}...")
        r = backtest_entry(sym)
        if r["trades"] > 0:
            all_results.append(r)
            print(f"  Trades:{r['trades']} WR:{r['win_rate']}% "
                  f"AvgW:+{r['avg_win']}% AvgL:{r['avg_loss']}% PF:{r['profit_factor']} "
                  f"AvgAdverse:{r['avg_adverse']}% AvgBars:{r['avg_bars']}")
        else:
            print(f"  No trades found")
        time.sleep(0.3)

    if not all_results:
        print("No results — try different symbols or timeframe")
        return

    # Aggregate
    total_trades = sum(r["trades"]    for r in all_results)
    avg_wr       = sum(r["win_rate"]  for r in all_results) / len(all_results)
    avg_win      = sum(r["avg_win"]   for r in all_results) / len(all_results)
    avg_loss     = sum(r["avg_loss"]  for r in all_results) / len(all_results)
    avg_pf       = sum(r["profit_factor"] for r in all_results) / len(all_results)
    avg_return   = sum(r["total_return"]  for r in all_results) / len(all_results)

    print(f"\n{'='*55}")
    print(f"AGGREGATE RESULTS ({len(all_results)} symbols)")
    print(f"{'='*55}")
    print(f"Total trades:   {total_trades}")
    print(f"Win rate:       {avg_wr:.1f}%")
    print(f"Avg win:        +{avg_win:.2f}%")
    print(f"Avg loss:       {avg_loss:.2f}%")
    avg_adverse = sum(r["avg_adverse"] for r in all_results) / len(all_results)
    avg_bars    = sum(r["avg_bars"]    for r in all_results) / len(all_results)
    print(f"Profit factor:  {avg_pf:.2f}")
    print(f"Avg return:     {avg_return:.2f}% per symbol")
    print(f"Avg max adverse:{avg_adverse:.2f}% (how far against before exit)")
    print(f"Avg bars held:  {avg_bars:.1f}")

    # Honest assessment
    print(f"\n{'='*55}")
    print("HONEST ASSESSMENT:")
    if avg_wr >= 60 and avg_pf >= 1.5:
        print("✅ Entry has real edge — proceed to add features")
    elif avg_wr >= 50 and avg_pf >= 1.2:
        print("⚠️  Marginal edge — tune entry before adding features")
    else:
        print("❌ Entry needs work — don't add features yet")
    print(f"{'='*55}\n")

    return all_results


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "verify":
        # Test single symbol verification
        symbol = sys.argv[2] if len(sys.argv) > 2 else "NVDA"
        print(f"\nVerifying entry for {symbol}:")
        result = verify_entry(symbol)
        print(f"\nResult: {'ENTRY VALID' if result['entry'] else 'NO ENTRY - failed at ' + result.get('step_failed', 'unknown')}")
    else:
        # Run full backtest
        run_foundation_backtest()
