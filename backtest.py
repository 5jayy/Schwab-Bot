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

    trades   = []
    in_trade = False
    entry_px = 0
    high_px  = 0
    bars_held = 0

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

        # Entry signal scoring
        ema9  = ema(closes, 9)
        ema21 = ema(closes, 21)
        rsi14 = rsi(closes, 14)
        if not ema9 or not ema21 or not rsi14:
            continue
        if ema9 <= ema21 or rsi14 >= 72 or rsi14 <= 28:
            continue

        score = ((ema9 - ema21) / ema21) * 100 * 35 + (100 - abs(rsi14 - 55)) * 0.35

        if strategy["use_adx"]:
            adx_val = calc_adx(window)
            if adx_val is not None and adx_val < 20:
                continue
            score += min(adx_val or 0, 50) * 0.3

        if strategy["use_macd"]:
            hist = macd_hist(closes)
            if hist is not None and hist < 0:
                continue
            score += min((hist or 0) * 100, 10)

        if strategy["use_sweep"]:
            score += liquidity_sweep(window)

        if strategy["use_candles"]:
            score += candlestick_bonus(window)

        if score < min_score:
            continue

        # Enter trade
        in_trade  = True
        entry_px  = candles[i+1]["open"]  # next bar open (realistic)
        high_px   = entry_px
        bars_held = 0

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

    # Sharpe approximation
    returns = [t["pnl"] for t in trades]
    avg_r   = sum(returns) / len(returns)
    std_r   = (sum((r - avg_r) ** 2 for r in returns) / len(returns)) ** 0.5
    sharpe  = avg_r / std_r if std_r > 0 else 0

    return {
        "trades":        len(trades),
        "win_rate":      round(win_rate * 100, 1),
        "avg_win_pct":   round(avg_win, 2),
        "avg_loss_pct":  round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "total_return":  round(total_return, 2),
        "sharpe":        round(sharpe, 2),
        "avg_bars_held": round(avg_bars, 1),
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
    Number of symbols scales with tier.
    """
    from scanner import get_tier, get_movers
    tier_name, tier_cfg = get_tier(bot_capital)

    # Symbol count scales with tier
    symbol_count = {
        "Tier 1": 10,
        "Tier 2": 15,
        "Tier 3": 20,
        "Tier 4": 30,
    }.get(tier_name, 15)

    # Pull live movers from multiple indices
    symbols = []
    for index in ["$SPX", "$COMPX", "$DJI"]:
        symbols += get_movers(index, "up", symbol_count // 2)
        time.sleep(0.2)

    # Dedupe
    seen, unique = set(), []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            unique.append(s)

    result = unique[:symbol_count]
    print(f"Dynamic test universe: {len(result)} symbols ({tier_name})")
    return result


def run_backtest(days: int = 60, bot_capital: float = 2400):
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
            "win_rate":      round(sum(m["win_rate"]      for m in metrics_list) / len(metrics_list), 1),
            "avg_win_pct":   round(sum(m["avg_win_pct"]   for m in metrics_list) / len(metrics_list), 2),
            "avg_loss_pct":  round(sum(m["avg_loss_pct"]  for m in metrics_list) / len(metrics_list), 2),
            "profit_factor": round(sum(m["profit_factor"] for m in metrics_list) / len(metrics_list), 2),
            "total_return":  round(sum(m["total_return"]  for m in metrics_list) / len(metrics_list), 2),
            "sharpe":        round(sum(m["sharpe"]        for m in metrics_list) / len(metrics_list), 2),
            "avg_bars_held": round(sum(m["avg_bars_held"] for m in metrics_list) / len(metrics_list), 1),
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
