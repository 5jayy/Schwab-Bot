"""
Circuit Options Backtest
Tests 0-7 DTE put selling strategy.
Validates 50% profit exit rule vs holding to expiry.
Tells us what actually works — simple but accurate enough.

Run: python3 backtest_options.py
"""

import requests
import time
import json
import os
from datetime import datetime, timedelta
from auth import get_valid_token

MARKET_URL  = "https://api.schwabapi.com/marketdata/v1"
COMMISSION  = 0.65

def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}

def get_price_history(symbol: str, days: int = 30, frequency: int = 1) -> list:
    """Get daily candles for backtest simulation."""
    try:
        resp = requests.get(
            f"{MARKET_URL}/pricehistory", headers=headers(),
            params={
                "symbol":        symbol,
                "periodType":    "day",
                "period":        min(days, 10),
                "frequencyType": "daily",
                "frequency":     1,
                "needExtendedHoursData": False,
            },
            timeout=15
        )
        return resp.json().get("candles", []) if resp.ok else []
    except Exception:
        return []

def simulate_put(candles: list, entry_idx: int, strike_pct: float = 0.95,
                 dte: int = 5, exit_at_pct: float = 0.50,
                 delta: float = 0.25) -> dict:
    """
    Simulate selling a cash secured put.
    
    Entry: sell put at strike = price × strike_pct
    Premium estimate: price × IV_proxy × sqrt(dte/365) × delta
    Exit rules:
      - 50% profit (premium drops to 50% of entry)
      - Stop: stock drops below strike × 0.97 (assigned risk)
      - Expiry: keep remaining premium
    """
    if entry_idx >= len(candles) - 1:
        return {}

    entry_price = candles[entry_idx]["close"]
    strike      = entry_price * strike_pct
    collateral  = strike * 100

    # Estimate premium using simplified Black-Scholes proxy
    # IV proxy from recent candle volatility
    lookback = candles[max(0, entry_idx-10):entry_idx+1]
    if len(lookback) > 2:
        returns = [(lookback[i]["close"] / lookback[i-1]["close"] - 1)
                   for i in range(1, len(lookback))]
        daily_vol = (sum(r**2 for r in returns) / len(returns)) ** 0.5
        iv_proxy  = daily_vol * (252 ** 0.5)  # annualized
    else:
        iv_proxy = 0.40  # default 40% IV

    # Premium estimate
    time_factor = (dte / 365) ** 0.5
    premium     = entry_price * iv_proxy * time_factor * delta
    premium     = max(premium, 0.05)

    net_premium    = premium - (COMMISSION / 100)
    target_exit    = premium * exit_at_pct   # 50% profit target
    stop_price     = strike * 0.97           # exit if stock near assignment
    max_loss       = premium                  # max loss = full premium if assigned

    # Simulate over DTE days
    result_type = "expiry"
    exit_premium = 0
    days_held    = 0

    for j in range(1, min(dte + 1, len(candles) - entry_idx)):
        idx        = entry_idx + j
        curr_price = candles[idx]["close"]
        days_held  = j

        # Estimate current premium (time decay)
        remaining_dte  = max(dte - j, 0)
        curr_time_fac  = (remaining_dte / 365) ** 0.5
        intrinsic      = max(0, strike - curr_price)
        curr_premium   = max(intrinsic, curr_price * iv_proxy * curr_time_fac * delta)

        # Stop: stock falling toward strike — assignment risk
        if curr_price <= stop_price:
            exit_premium = curr_premium
            result_type  = "stopped"
            break

        # 50% profit target
        if curr_premium <= target_exit:
            exit_premium = curr_premium
            result_type  = "target_hit"
            break

    if result_type == "expiry":
        # At expiry — check if ITM or OTM
        final_price = candles[min(entry_idx + dte, len(candles)-1)]["close"]
        if final_price >= strike:
            exit_premium = 0  # OTM — keep full premium
            result_type  = "expired_otm"
        else:
            exit_premium = strike - final_price  # ITM — loss
            result_type  = "expired_itm"

    profit = net_premium - exit_premium
    roi    = profit / collateral * 100

    return {
        "entry_price":  round(entry_price, 2),
        "strike":       round(strike, 2),
        "collateral":   round(collateral, 2),
        "premium":      round(premium, 2),
        "net_premium":  round(net_premium, 2),
        "exit_premium": round(exit_premium, 2),
        "profit":       round(profit, 2),
        "roi_pct":      round(roi, 3),
        "result":       result_type,
        "days_held":    days_held,
        "dte":          dte,
        "iv_proxy":     round(iv_proxy * 100, 1),
    }


def run_options_backtest(symbols: list = None, days: int = 14,
                         dte_range: list = [0, 1, 3, 5, 7],
                         exit_pcts: list = [0.50, 0.75, 1.00]):
    """
    Backtest options across symbols, DTE, and exit percentages.
    Tells you: best DTE, best exit %, best symbols.
    """
    if symbols is None:
        # Get live movers
        try:
            resp = requests.get(
                f"{MARKET_URL}/movers/$SPX", headers=headers(),
                params={"sort": "VOLUME", "frequency": 1}, timeout=10
            )
            symbols = [m["symbol"] for m in resp.json().get("screeners", [])[:10]]
        except Exception:
            symbols = ["NVDA", "AAPL", "TSLA", "MSFT", "AMD",
                      "COIN", "MARA", "PLTR", "SOFI", "META"]

    print(f"\n{'='*60}")
    print(f"OPTIONS BACKTEST — {days}d | {len(symbols)} symbols")
    print(f"DTE tested: {dte_range}")
    print(f"Exit %: {exit_pcts}")
    print(f"{'='*60}\n")

    # Results by DTE and exit %
    results = {}
    for dte in dte_range:
        for ep in exit_pcts:
            results[(dte, ep)] = {"trades": 0, "wins": 0, "total_roi": 0,
                                   "profits": [], "results": {}}

    all_trades = []

    for sym in symbols:
        candles = get_price_history(sym, days)
        if len(candles) < 10:
            print(f"  {sym}: not enough data")
            continue

        print(f"  {sym}: {len(candles)} candles | testing {len(dte_range) * len(exit_pcts)} combos")

        for i in range(20, len(candles) - 8):  # walk every bar
            c      = candles[i]
            closes = [candles[j]["close"] for j in range(max(0, i-20), i+1)]
            if len(closes) < 20:
                continue

            # Real entry signals — only enter on qualified setups
            rng    = c["high"] - c["low"]
            body   = abs(c["close"] - c["open"])
            if rng == 0:
                continue
            strength  = body / rng
            close_pos = (c["close"] - c["low"]) / rng
            candle_sc = strength * 50 + close_pos * 30
            if candle_sc < 20:
                continue

            # Volume spike check
            vols    = [candles[j]["volume"] for j in range(max(0, i-20), i+1)]
            avg_vol = sum(vols[:-1]) / max(len(vols)-1, 1)
            if avg_vol > 0 and vols[-1] < avg_vol * 1.2:
                continue

            # MTF alignment — uptrend only
            ma20 = sum(closes[-20:]) / 20
            ma10 = sum(closes[-10:]) / 10
            if closes[-1] <= ma20 or closes[-1] <= ma10:
                continue

            for dte in dte_range:
                for ep in exit_pcts:
                    r = simulate_put(
                        candles, i,
                        strike_pct=0.95,
                        dte=max(dte, 1),
                        exit_at_pct=ep,
                        delta=0.25,
                    )
                    if not r:
                        continue

                    key = (dte, ep)
                    results[key]["trades"] += 1
                    results[key]["total_roi"] += r["roi_pct"]
                    results[key]["profits"].append(r["profit"])
                    results[key]["results"][r["result"]] = results[key]["results"].get(r["result"], 0) + 1

                    if r["profit"] > 0:
                        results[key]["wins"] += 1

                    all_trades.append({**r, "symbol": sym, "dte": dte, "exit_pct": ep})

        time.sleep(0.3)

    # Print results
    print(f"\n{'='*60}")
    print("RESULTS BY DTE + EXIT %")
    print(f"{'='*60}")

    best_combo = None
    best_score = -999

    for (dte, ep), data in sorted(results.items()):
        if data["trades"] == 0:
            continue
        wr      = data["wins"] / data["trades"] * 100
        avg_roi = data["total_roi"] / data["trades"]
        score   = avg_roi * (wr / 100)  # yield-adjusted win rate

        print(f"DTE {dte:2d} | Exit {ep:.0%} | Trades {data['trades']:3d} | "
              f"Win {wr:.0f}% | Avg ROI {avg_roi:+.3f}% | Score {score:.3f}")

        if score > best_score:
            best_score = score
            best_combo = (dte, ep, wr, avg_roi)

    print(f"\n{'='*60}")
    print("BEST COMBO:")
    if best_combo:
        dte, ep, wr, roi = best_combo
        print(f"  DTE: {dte} days")
        print(f"  Exit at: {ep:.0%} profit")
        print(f"  Win rate: {wr:.0f}%")
        print(f"  Avg ROI per trade: {roi:+.3f}%")

        # Monthly projection at $502 budget
        budget    = 502
        avg_coll  = budget
        trades_mo = 4 * (7 / max(dte, 1))  # cycles per month
        monthly   = avg_coll * (roi / 100) * trades_mo
        after_tax = monthly * (1 - 0.3775)
        print(f"\n  Monthly projection at ${budget} budget:")
        print(f"  Gross: ${monthly:,.2f}/mo")
        print(f"  After tax (37.75%): ${after_tax:,.2f}/mo")

    # Result type breakdown
    print(f"\n{'='*60}")
    print("EXIT REASON BREAKDOWN (best combo):")
    if best_combo:
        dte, ep = best_combo[0], best_combo[1]
        breakdown = results[(dte, ep)]["results"]
        total = sum(breakdown.values())
        for reason, count in sorted(breakdown.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count} ({count/total*100:.0f}%)")

    # Send to Telegram
    try:
        from telegram import send_alert
        if best_combo:
            dte, ep, wr, roi = best_combo
            budget    = 502
            trades_mo = 4 * (7 / max(dte, 1))
            monthly   = budget * (roi / 100) * trades_mo
            after_tax = monthly * (1 - 0.3775)
            msg  = "[ CIRCUIT ] OPTIONS BACKTEST\n"
            msg += "━━━━━━━━━━━━━━━━━━\n"
            msg += f"BEST: DTE {dte}d | Exit {ep:.0%}\n"
            msg += f"WIN   {wr:.0f}%\n"
            msg += f"ROI   {roi:+.3f}% per trade\n"
            msg += f"PROJ  ${monthly:.0f}/mo gross\n"
            msg += f"NET   ${after_tax:.0f}/mo after tax\n"
            msg += f"━━━━━━━━━━━━━━━━━━\n"
            msg += f"50% exit {'✅ confirmed' if ep == 0.50 else '⚠️ not best'}"
            send_alert(msg)
    except Exception as ex:
        print(f"Telegram error: {ex}")

    print(f"\n{'='*60}\n")
    return results, best_combo


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    run_options_backtest(days=days)
