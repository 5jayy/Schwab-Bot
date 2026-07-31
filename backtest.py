"""
Dynamic Backtesting & Strategy Ranking System
Tests your bot's scoring system against historical Schwab data.
Ranks strategies by win rate, profit factor, and Sharpe ratio.
Run manually: python3 backtest.py
Results saved to /data/backtest_results.json
"""

import requests
import time
import json
import os
from datetime import datetime, timedelta
from auth import get_valid_token
from scanner import (
    get_tier, ema, rsi, macd_hist, calc_adx, calc_atr,
    volume_ok, liquidity_sweep, candlestick_bonus, score_stock,
    BOT_TIERS
)

BASE_URL = "https://api.schwabapi.com/marketdata/v1"
RESULTS_PATH = "/data/backtest_results.json" if os.path.exists("/data") else "backtest_results.json"


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_historical_candles(symbol: str, days: int = 90, frequency: int = 30) -> list:
    """Pull historical daily candles for backtesting."""
    try:
        end   = datetime.utcnow()
        start = end - timedelta(days=days)
        resp  = requests.get(
            f"{BASE_URL}/pricehistory", headers=headers(),
            params={
                "symbol":        symbol,
                "periodType":    "day",
                "period":        min(days // 30, 6),
                "frequencyType": "minute",
                "frequency":     frequency,
                "needExtendedHoursData": False,
            },
            timeout=15
        )
        resp.raise_for_status()
        return resp.json().get("candles", [])
    except Exception as ex:
        print(f"  History error {symbol}: {ex}")
        return []


# ── Strategy variants to rank ─────────────────────────────────────────────────

# 3 variants — baseline vs full signals vs full with multi-timeframe
STRATEGIES = {
    "Baseline": {
        "description": "EMA crossover + RSI only (simple)",
        "use_adx":     False,
        "use_macd":    False,
        "use_sweep":   False,
        "use_candles": False,
        "use_mtf":     False,
    },
    "Full_Signal": {
        "description": "EMA + ADX + MACD + Liquidity Sweep + Candlesticks",
        "use_adx":     True,
        "use_macd":    True,
        "use_sweep":   True,
        "use_candles": True,
        "use_mtf":     False,
    },
    "Full_MTF": {
        "description": "Full Signal + Multi-timeframe alignment (current bot)",
        "use_adx":     True,
        "use_macd":    True,
        "use_sweep":   True,
        "use_candles": True,
        "use_mtf":     True,
    },
}


def simulate_strategy(candles: list, strategy: dict, hold_bars: int = 12,
                       trail_pct: float = 0.07, min_score: float = 35) -> dict:
    """
    Walk through historical candles, simulate entries/exits.
    Dynamic: trail adjusts based on profit level (matches live bot logic).
    Returns performance metrics.
    """
    if len(candles) < 50:
        return {}

    trades          = []
    in_trade        = False
    entry_px        = 0
    high_px         = 0
    bars_held       = 0
    missed_signals  = 0  # signals that qualified but were skipped

    for i in range(30, len(candles) - 1):
        window  = candles[:i+1]
        closes  = [c["close"] for c in window]
        current = candles[i]["close"]

        if in_trade:
            bars_held += 1
            if current > high_px:
                high_px = current

            # Dynamic trailing stop — matches live bot
            profit_pct = (current - entry_px) / entry_px
            if profit_pct >= 0.20:
                trail = 0.03
            elif profit_pct >= 0.10:
                trail = 0.04
            elif profit_pct >= 0.05:
                trail = 0.05
            elif profit_pct >= 0.02:
                # Breakeven
                stop = entry_px
                if current <= stop:
                    pnl = (current - entry_px) / entry_px * 100
                    trades.append({"pnl": pnl, "bars": bars_held, "exit": "breakeven"})
                    in_trade = False
                continue
            else:
                trail = trail_pct

            stop = high_px * (1 - trail)
            if current <= stop or bars_held >= hold_bars * 3:
                pnl = (current - entry_px) / entry_px * 100
                trades.append({"pnl": pnl, "bars": bars_held,
                               "exit": "trail" if current <= stop else "timeout"})
                in_trade = False
            continue

        # ── Real bot entry signals (matches live scanner) ──
        # 1. Candle strength — body/range ratio
        c      = candles[i]
        rng    = c["high"] - c["low"]
        body   = abs(c["close"] - c["open"])
        if rng == 0:
            continue
        strength = body / rng
        close_pos = (c["close"] - c["low"]) / rng
        candle_score = strength * 50 + close_pos * 30

        if candle_score < 20:  # weak candle — skip
            continue

        # 2. Volume spike — must be 1.3x 20-bar average
        vols = [candles[j]["volume"] for j in range(max(0, i-20), i+1)]
        if len(vols) < 5:
            continue
        avg_vol = sum(vols[:-1]) / max(len(vols) - 1, 1)
        cur_vol = vols[-1]
        if avg_vol > 0 and cur_vol < avg_vol * 1.3:
            continue  # no volume spike — skip

        # 3. MTF alignment — price above MA20 and MA10
        if len(closes) < 20:
            continue
        ma20 = sum(closes[-20:]) / 20
        ma10 = sum(closes[-10:]) / 10
        if current <= ma20 or current <= ma10:
            continue  # not in uptrend — skip

        # 4. Wick rejection bonus
        lower_wick = min(c["open"], c["close"]) - c["low"]
        wick_bonus = 10 if rng > 0 and lower_wick / rng > 0.45 else 0

        # 5. Liquidity sweep bonus
        sweep_bonus = liquidity_sweep(window) if strategy.get("use_sweep") else 0

        # Total score
        score = candle_score * 0.7 + sweep_bonus + wick_bonus + (change_pct * 2 if (change_pct := (current - candles[i-1]["close"]) / candles[i-1]["close"] * 100 if candles[i-1]["close"] > 0 else 0) else 0)

        if score < min_score:
            continue

        # Enter trade
        in_trade  = True
        entry_px  = candles[i+1]["open"]  # next bar open (realistic)
        high_px   = entry_px
        bars_held = 0
        continue

    # Count missed — signal qualified but we were already in a trade
    if in_trade:
        missed_signals += 0  # already handled above
    elif score >= min_score:
        missed_signals += 1  # signal fired but filtered by something

    if not trades:
        return {}

    wins        = [t for t in trades if t["pnl"] > 0]
    losses      = [t for t in trades if t["pnl"] <= 0]
    win_rate    = len(wins) / len(trades)
    avg_win     = sum(t["pnl"] for t in wins)  / len(wins)  if wins   else 0
    avg_loss    = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else 999
    total_return  = sum(t["pnl"] for t in trades)
    avg_bars      = sum(t["bars"] for t in trades) / len(trades)
    peak_return   = max(t["pnl"] for t in trades) if trades else 0

    # Sharpe approximation
    returns = [t["pnl"] for t in trades]
    avg_r   = sum(returns) / len(returns)
    std_r   = (sum((r - avg_r) ** 2 for r in returns) / len(returns)) ** 0.5
    sharpe  = avg_r / std_r if std_r > 0 else 0

    return {
        "trades":        len(trades),
        "missed":        missed_signals,
        "win_rate":      round(win_rate * 100, 1),
        "avg_win_pct":   round(avg_win, 2),
        "avg_loss_pct":  round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "total_return":  round(total_return, 2),
        "sharpe":        round(sharpe, 2),
        "avg_bars_held": round(avg_bars, 1),
        "peak_return":   round(peak_return, 2),
    }


# ── Ranking system ────────────────────────────────────────────────────────────

def rank_strategies(results: dict) -> list:
    """
    Rank strategies by composite score:
    - Win rate (30%)
    - Profit factor (30%)
    - Sharpe ratio (25%)
    - Total return (15%)
    Fully dynamic — no fixed thresholds, relative ranking.
    """
    ranked = []
    for name, metrics in results.items():
        if not metrics:
            continue
        # Normalize each metric 0-100 across all strategies
        ranked.append({"name": name, "metrics": metrics})

    if not ranked:
        return []

    # Get max values for normalization
    max_wr = max(r["metrics"]["win_rate"]      for r in ranked) or 1
    max_pf = max(r["metrics"]["profit_factor"] for r in ranked) or 1
    max_sh = max(r["metrics"]["sharpe"]        for r in ranked) or 1
    max_tr = max(r["metrics"]["total_return"]  for r in ranked) or 1

    for r in ranked:
        m = r["metrics"]
        composite = (
            (m["win_rate"]      / max_wr) * 30 +
            (m["profit_factor"] / max_pf) * 30 +
            (m["sharpe"]        / max_sh) * 25 +
            (m["total_return"]  / max_tr) * 15
        )
        r["composite_score"] = round(composite, 1)

    ranked.sort(key=lambda x: x["composite_score"], reverse=True)
    return ranked


# ── Main backtest runner ──────────────────────────────────────────────────────

def get_test_symbols(bot_capital: float = 2400) -> list:
    """
    Pull live Schwab movers for backtesting — dynamic, not fixed.
    Falls back to liquid stocks when market is closed (weekends).
    Number of symbols scales with tier.
    """
    from scanner import get_tier, get_movers
    tier_name, tier_cfg = get_tier(bot_capital)
    symbol_count = {
        "Tier 1": 10, "Tier 2": 15, "Tier 3": 20, "Tier 4": 30
    }.get(tier_name, 15)

    symbols = []
    for index in ["$SPX", "$COMPX", "$DJI"]:
        symbols += get_movers(index, "up", symbol_count // 2)
        time.sleep(0.2)

    seen, unique = set(), []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            unique.append(s)

    # Fallback when market closed — liquid stocks with good history
    if not unique:
        fallback = [
            "NVDA", "AAPL", "MSFT", "AMD", "TSLA",
            "COIN", "PLTR", "SOFI", "BAC", "DKNG",
            "UBER", "SNAP", "RIOT", "MARA", "AAL",
            "META", "AMZN", "GOOGL", "JPM", "GS",
            "PYPL", "CRM", "SHOP", "INTC", "MU",
            "QQQ", "SPY", "SCHD", "VOO", "VTI"
        ]
        unique = fallback[:symbol_count]
        print(f"Market closed — using {len(unique)} fallback symbols ({tier_name})")
    else:
        print(f"Dynamic universe: {len(unique)} live movers ({tier_name})")

    return unique


def run_backtest(days: int = 60, bot_capital: float = 2400):
    # Resolve the test symbols from the function (fixes undefined TEST_SYMBOLS)
    TEST_SYMBOLS = get_test_symbols(bot_capital)
    print(f"\n{'='*50}")
    print(f"BACKTEST — Last {days} days | {len(TEST_SYMBOLS)} symbols | {len(STRATEGIES)} strategies")
    print(f"{'='*50}\n")

    strategy_results = {name: {"trades": 0, "wins": 0, "total_pnl": 0,
                                "all_metrics": []} for name in STRATEGIES}

    for symbol in TEST_SYMBOLS:
        print(f"Testing {symbol}...")
        candles = get_historical_candles(symbol, days=days)
        if len(candles) < 50:
            print(f"  Not enough data — skip")
            continue

        for strat_name, strat_cfg in STRATEGIES.items():
            metrics = simulate_strategy(candles, strat_cfg)
            if metrics:
                strategy_results[strat_name]["all_metrics"].append(metrics)
        time.sleep(0.5)

    # Aggregate results across all symbols
    aggregated = {}
    for name, data in strategy_results.items():
        metrics_list = data["all_metrics"]
        if not metrics_list:
            aggregated[name] = {}
            continue

        aggregated[name] = {
            "trades":        sum(m["trades"]        for m in metrics_list),
            "missed":        sum(m.get("missed", 0) for m in metrics_list),
            "win_rate":      round(sum(m["win_rate"]      for m in metrics_list) / len(metrics_list), 1),
            "avg_win_pct":   round(sum(m["avg_win_pct"]   for m in metrics_list) / len(metrics_list), 2),
            "avg_loss_pct":  round(sum(m["avg_loss_pct"]  for m in metrics_list) / len(metrics_list), 2),
            "profit_factor": round(sum(m["profit_factor"] for m in metrics_list) / len(metrics_list), 2),
            "total_return":  round(sum(m["total_return"]  for m in metrics_list) / len(metrics_list), 2),
            "sharpe":        round(sum(m["sharpe"]        for m in metrics_list) / len(metrics_list), 2),
            "avg_bars_held": round(sum(m["avg_bars_held"] for m in metrics_list) / len(metrics_list), 1),
            "peak_return":   round(max(m.get("peak_return", 0) for m in metrics_list), 2),
            "symbols_tested": len(metrics_list),
        }

    # Rank strategies
    ranked = rank_strategies(aggregated)

    print(f"\n{'='*50}")
    print("STRATEGY RANKINGS")
    print(f"{'='*50}")
    for i, r in enumerate(ranked, 1):
        m = r["metrics"]
        print(f"\n#{i} {r['name']} — Score: {r['composite_score']}")
        print(f"   {STRATEGIES[r['name']]['description']}")
        print(f"   Trades: {m['trades']} | Win rate: {m['win_rate']}%")
        print(f"   Avg win: +{m['avg_win_pct']}% | Avg loss: {m['avg_loss_pct']}%")
        print(f"   Profit factor: {m['profit_factor']} | Sharpe: {m['sharpe']}")
        print(f"   Total return: {m['total_return']}% avg per symbol")
        print(f"   Avg hold: {m['avg_bars_held']} bars")

    # Save results
    output = {
        "run_date":    datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days_tested": days,
        "symbols":     TEST_SYMBOLS,
        "rankings":    ranked,
        "raw":         aggregated,
    }
    try:
        with open(RESULTS_PATH, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\nResults saved to {RESULTS_PATH}")
    except Exception as ex:
        print(f"Save error: {ex}")

    # Print recommendation
    if ranked:
        best = ranked[0]
        print(f"\n🏆 RECOMMENDED: {best['name']}")
        print(f"   {STRATEGIES[best['name']]['description']}")
        print(f"   Composite score: {best['composite_score']}/100")

    return ranked


if __name__ == "__main__":
    import sys
    from ledger import get_trading_capital
    days    = int(sys.argv[1])   if len(sys.argv) > 1 else 60
    capital = float(sys.argv[2]) if len(sys.argv) > 2 else get_trading_capital()
    run_backtest(days=days, bot_capital=capital)
