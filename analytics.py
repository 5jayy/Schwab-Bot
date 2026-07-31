"""
Circuit Analytics — Real Trade History
Reads actual closed trades from ledger + Schwab transactions.
100% accurate — no simulation.

Run: python3 analytics.py
"""

import requests
import json
import os
import time
from datetime import datetime, timezone, timedelta
from auth import get_valid_token

BASE_URL    = "https://api.schwabapi.com/trader/v1"
MARKET_URL  = "https://api.schwabapi.com/marketdata/v1"
LEDGER_PATH = "/data/trade_ledger.json" if os.path.exists("/data") else "trade_ledger.json"

BOT_CAPITAL  = 2220.0
TARGET_DAILY = 80.0
DAY_CEILING  = 148.0


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def load_ledger() -> dict:
    try:
        with open(LEDGER_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def get_schwab_trades(encrypted: str, days_back: int = 30) -> list:
    """Pull all TRADE transactions from Schwab."""
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)
    try:
        resp = requests.get(
            f"{BASE_URL}/accounts/{encrypted}/transactions",
            headers=headers(),
            params={
                "startDate": start.strftime("%Y-%m-%dT00:00:00.000Z"),
                "endDate":   end.strftime("%Y-%m-%dT23:59:59.000Z"),
                "types":     "TRADE"
            },
            timeout=30
        )
        return resp.json() if resp.ok and isinstance(resp.json(), list) else []
    except Exception as ex:
        print(f"Schwab fetch error: {ex}")
        return []


def get_account_hash() -> str:
    resp = requests.get(
        f"{BASE_URL}/accounts/accountNumbers",
        headers=headers(), timeout=10
    )
    return resp.json()[0]["hashValue"] if resp.ok else ""


def parse_trades_from_schwab(txns: list) -> list:
    """
    Parse buy/sell pairs from Schwab transactions.
    Schwab netAmount: negative = money out (buy), positive = money in (sell).
    Uses netAmount for accurate P&L not price calculation.
    """
    buys  = {}
    sells = []

    # Sort by date so buys come before sells
    txns_sorted = sorted(txns, key=lambda x: x.get("tradeDate", x.get("activityDate", "")))

    for txn in txns_sorted:
        items = txn.get("transferItems", [])
        date  = txn.get("tradeDate", txn.get("activityDate", ""))[:10]
        net   = txn.get("netAmount", 0)

        symbol = ""
        qty    = 0
        price  = 0

        for item in items:
            inst = item.get("instrument", {})
            if inst.get("assetType") == "EQUITY":
                symbol = inst.get("symbol", "")
                qty    = abs(item.get("amount", item.get("quantity", 0)))
                price  = abs(item.get("price", 0))
                break

        if not symbol or not qty:
            continue

        # net < 0 = money went out = BUY
        # net > 0 = money came in = SELL
        if net < 0:
            cost_per_share = abs(net) / qty if qty > 0 else price
            buys[symbol] = {
                "symbol":    symbol,
                "qty":       qty,
                "price":     cost_per_share,
                "date":      date,
                "total_cost": abs(net)
            }
        elif net > 0:
            buy = buys.get(symbol)
            if not buy:
                continue

            # Accurate P&L from actual net amounts
            profit       = net - buy["total_cost"]
            sell_per_sh  = net / qty if qty > 0 else price
            buy_per_sh   = buy["price"]
            move_pct     = (sell_per_sh - buy_per_sh) / buy_per_sh * 100 if buy_per_sh > 0 else 0

            hold_days = 0
            try:
                b = datetime.strptime(buy["date"], "%Y-%m-%d")
                s = datetime.strptime(date, "%Y-%m-%d")
                hold_days = (s - b).days
            except Exception:
                pass

            sells.append({
                "symbol":     symbol,
                "buy_price":  round(buy_per_sh, 4),
                "sell_price": round(sell_per_sh, 4),
                "qty":        qty,
                "profit":     round(profit, 2),
                "move_pct":   round(move_pct, 2),
                "hold_days":  hold_days,
                "buy_date":   buy["date"],
                "sell_date":  date,
                "cost":       buy["total_cost"],
            })
            del buys[symbol]

    return sells


def run_analytics(days_back: int = 30):
    print(f"\n{'='*55}")
    print(f"CIRCUIT ANALYTICS — Real Trades | Last {days_back} days")
    print(f"{'='*55}")
    print(f"Bot capital: ${BOT_CAPITAL:,.0f} | Target: ${TARGET_DAILY}/day\n")

    # Get real trades from Schwab
    encrypted = get_account_hash()
    if not encrypted:
        print("Error: Could not get account hash")
        return

    print("Fetching real trades from Schwab...")
    txns   = get_schwab_trades(encrypted, days_back)
    trades = parse_trades_from_schwab(txns)

    # Also check ledger closed trades
    ledger        = load_ledger()
    ledger_trades = ledger.get("closed_trades", [])

    # Combine — prefer Schwab data, supplement with ledger
    all_trades = trades if trades else []

    # If no Schwab trades, use ledger
    if not all_trades and ledger_trades:
        print("Using ledger trade history...")
        for lt in ledger_trades:
            buy_px  = lt.get("buy_price", 0)
            sell_px = lt.get("sell_price", 0)
            profit  = lt.get("profit", 0)
            move_pct = (sell_px - buy_px) / buy_px * 100 if buy_px > 0 else 0
            all_trades.append({
                "symbol":     lt.get("symbol", ""),
                "buy_price":  buy_px,
                "sell_price": sell_px,
                "qty":        lt.get("quantity", 0),
                "profit":     profit,
                "move_pct":   round(move_pct, 2),
                "hold_days":  0,
                "cost":       buy_px * lt.get("quantity", 0),
            })

    if not all_trades:
        print("No closed trades found yet.")
        print("Bot needs to make and close trades first.")
        print("\nOpen positions in ledger:")
        for sym, pos in ledger.get("open_trades", {}).items():
            print(f"  {sym}: {pos.get('quantity')} shares @ ${pos.get('buy_price', 0):.2f}")
        return

    total   = len(all_trades)
    wins    = [t for t in all_trades if t["profit"] > 0]
    losses  = [t for t in all_trades if t["profit"] <= 0]
    trading_days = max(days_back, 1)

    win_rate  = len(wins) / total * 100
    avg_win   = sum(t["profit"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss  = sum(t["profit"] for t in losses) / len(losses) if losses else 0
    avg_move  = sum(t["move_pct"] for t in all_trades) / total
    avg_win_m = sum(t["move_pct"] for t in wins)   / len(wins)   if wins   else 0
    avg_los_m = sum(t["move_pct"] for t in losses) / len(losses) if losses else 0
    rr        = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    total_pnl = sum(t["profit"] for t in all_trades)
    avg_cost  = sum(t["cost"] for t in all_trades) / total
    avg_daily = total_pnl / trading_days
    pf        = abs(sum(t["profit"] for t in wins) / sum(t["profit"] for t in losses)) if losses else 999

    print(f"REAL RESULTS — {total} trades | {len(wins)} wins | {len(losses)} losses")
    print(f"{'='*55}")
    print(f"Win rate:       {win_rate:.1f}%")
    print(f"Avg win $:      +${avg_win:,.2f}")
    print(f"Avg loss $:     -${abs(avg_loss):,.2f}")
    print(f"R:R ratio:      {rr:.2f}")
    print(f"Profit factor:  {pf:.2f}")
    print(f"Total P&L:      ${total_pnl:+,.2f}")
    print(f"Avg position:   ${avg_cost:,.2f}")

    print(f"\n── Metric 1: Avg Move Captured ──")
    print(f"Avg move:       {avg_move:+.2f}%")
    print(f"Avg win move:   +{avg_win_m:.2f}%")
    print(f"Avg loss move:  {avg_los_m:.2f}%")
    if avg_move < 0.3:
        print(f"  ⚠️  Under 0.3% — catching noise or holding too short")
    elif avg_move < 1.0:
        print(f"  ✅ Real moves — room to grow with more capital")
    else:
        print(f"  ✅ Strong move capture")

    print(f"\n── Metric 2: Projected $/day vs ${TARGET_DAILY} ──")
    print(f"Avg $/trade:    ${total_pnl/total:+,.2f}")
    print(f"Trades/day:     {total/trading_days:.1f}")
    print(f"Actual/day:     ${avg_daily:+,.2f}")
    print(f"Target/day:     ${TARGET_DAILY:,.0f}")
    gap = TARGET_DAILY - avg_daily
    if gap > 0:
        print(f"Gap to target:  ${gap:,.2f}/day")
        # How to close the gap
        needed_trades = TARGET_DAILY / max(total_pnl/total, 0.01)
        needed_size   = TARGET_DAILY / max(total/trading_days, 0.1) / max(avg_move/100, 0.001)
        print(f"  → Need {needed_trades:.0f} trades/day OR ${needed_size:,.0f} avg position to hit target")
    else:
        print(f"  ✅ ON PACE — averaging ${avg_daily:+,.0f}/day!")

    print(f"\n── Metric 3: TO FIX ──")
    bottlenecks = []
    if win_rate < 50:
        bottlenecks.append(f"WIN RATE ({win_rate:.0f}%) — need 50%+. Tighten entry signals")
    if rr < 1.5:
        bottlenecks.append(f"R:R ({rr:.2f}) — need 1.5+. Let winners run longer or cut losses faster")
    if avg_move < 0.5:
        bottlenecks.append(f"MOVE CAPTURE ({avg_move:.2f}%) — exits too early. Check TP levels")
    if avg_cost < DAY_CEILING * 0.7:
        bottlenecks.append(f"SIZING (${avg_cost:.0f} avg) — position too small. Need more capital or bigger position %")

    if not bottlenecks:
        print(f"  ✅ No bottlenecks — scale capital to hit ${TARGET_DAILY}/day")
    else:
        for b in bottlenecks:
            print(f"  ⚠️  {b}")

    print(f"\n── Metric 4: By Symbol ──")
    sym_stats = {}
    for t in all_trades:
        s = t["symbol"]
        if s not in sym_stats:
            sym_stats[s] = {"trades": 0, "profit": 0, "moves": []}
        sym_stats[s]["trades"] += 1
        sym_stats[s]["profit"] += t["profit"]
        sym_stats[s]["moves"].append(t["move_pct"])

    sorted_syms = sorted(sym_stats.items(), key=lambda x: x[1]["profit"], reverse=True)
    for sym, stats in sorted_syms[:8]:
        avg_m = sum(stats["moves"]) / len(stats["moves"])
        print(f"  {sym}: {stats['trades']} trades | P&L ${stats['profit']:+,.2f} | avg move {avg_m:+.2f}%")

    print(f"\n{'='*55}")
    print(f"SUMMARY")
    print(f"{'='*55}")
    print(f"Real trades:    {total}")
    print(f"Win rate:       {win_rate:.1f}%")
    print(f"R:R:            {rr:.2f}")
    print(f"Total P&L:      ${total_pnl:+,.2f}")
    print(f"Daily avg:      ${avg_daily:+,.2f}")
    print(f"Gap to $80/day: ${max(gap,0):,.2f}")
    print(f"{'='*55}\n")

    # Save
    path = "/data/analytics_results.json" if os.path.exists("/data") else "analytics_results.json"
    with open(path, "w") as f:
        json.dump({
            "run_date":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "days_back":   days_back,
            "total":       total,
            "win_rate":    round(win_rate, 1),
            "rr":          round(rr, 2),
            "avg_move":    round(avg_move, 2),
            "total_pnl":   round(total_pnl, 2),
            "avg_daily":   round(avg_daily, 2),
            "gap_to_80":   round(max(gap, 0), 2),
        }, f, indent=2)
    print(f"Saved to {path}")


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    run_analytics(days_back=days)
