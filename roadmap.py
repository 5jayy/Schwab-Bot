"""
Circuit Roadmap — ETF Options Path
Smart capital routing toward ETF options income.

The roadmap tracks every ETF position, calculates:
- Days to unlock covered calls (100 shares)
- Days to unlock cash secured puts (collateral)
- Premium potential at each unlock
- Whether swing capital should route to ETF accumulation
- Tax-aware hold time tracking
- Auto-priority ETF sweep routing

Run: python3 roadmap.py
Called by: pre-market summary, strategy loop
"""

import requests
import json
import os
import time
from datetime import datetime, timedelta
from auth import get_valid_token
from ledger import load_ledger, save_ledger
from telegram import send_alert

BASE_URL   = "https://api.schwabapi.com/trader/v1"
MARKET_URL = "https://api.schwabapi.com/marketdata/v1"

# Maryland tax rates
ST_RATE = 0.3775
LT_RATE = 0.2075

# ETF options universe — premium estimates per contract
ETF_ROADMAP = {
    "SCHB": {
        "target_shares":    100,
        "approx_price":     29,
        "est_call_premium": 0.25,   # monthly covered call estimate
        "est_put_premium":  0.20,   # monthly put estimate
        "category":         "growth",
        "priority":         1,      # accumulate first (cheapest/closest)
    },
    "SCHG": {
        "target_shares":    100,
        "approx_price":     30,
        "est_call_premium": 0.25,
        "est_put_premium":  0.20,
        "category":         "growth",
        "priority":         2,
    },
    "SCHD": {
        "target_shares":    100,
        "approx_price":     31,
        "est_call_premium": 0.30,
        "est_put_premium":  0.25,
        "category":         "income",
        "priority":         3,
    },
    "JEPI": {
        "target_shares":    100,
        "approx_price":     60,
        "est_call_premium": 0.50,
        "est_put_premium":  0.45,
        "category":         "income",
        "priority":         4,
    },
    "QQQ": {
        "target_shares":    100,
        "approx_price":     565,
        "est_call_premium": 5.00,
        "est_put_premium":  4.50,
        "category":         "growth",
        "priority":         5,      # long-term goal
    },
    "VOO": {
        "target_shares":    100,
        "approx_price":     640,
        "est_call_premium": 4.50,
        "est_put_premium":  4.00,
        "category":         "growth",
        "priority":         6,
    },
}


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_etf_positions(encrypted: str) -> dict:
    """Get current ETF positions from Schwab."""
    try:
        resp = requests.get(
            f"{BASE_URL}/accounts/{encrypted}?fields=positions",
            headers=headers(), timeout=15
        )
        resp.raise_for_status()
        positions = resp.json()["securitiesAccount"].get("positions", [])
        result = {}
        for p in positions:
            sym = p["instrument"]["symbol"]
            if sym in ETF_ROADMAP:
                result[sym] = {
                    "shares":     p.get("longQuantity", 0),
                    "avg_price":  p.get("averagePrice", 0),
                    "mkt_value":  p.get("marketValue", 0),
                    "cost_basis": p.get("longQuantity", 0) * p.get("averagePrice", 0),
                }
        return result
    except Exception as ex:
        print(f"Position fetch error: {ex}")
        return {}


def get_cash_balance(encrypted: str) -> float:
    """Get current cash balance."""
    try:
        resp = requests.get(
            f"{BASE_URL}/accounts/{encrypted}?fields=positions",
            headers=headers(), timeout=15
        )
        resp.raise_for_status()
        return resp.json()["securitiesAccount"]["currentBalances"]["cashBalance"]
    except Exception:
        return 0.0


def calculate_roadmap(encrypted: str) -> dict:
    """
    Calculate the full ETF options roadmap.
    Returns priority list, ETAs, premium potential, routing decisions.
    """
    ledger    = load_ledger()
    positions = get_etf_positions(encrypted)
    cash      = get_cash_balance(encrypted)
    capital   = ledger.get("trading_capital", 2220)
    etf_bucket = ledger.get("etf_bucket", 0)

    # Daily profit rate from ledger history
    daily_history = ledger.get("daily_pnl_history", [])
    avg_daily = sum(daily_history) / len(daily_history) if daily_history else 79.98
    avg_daily = max(avg_daily, 1.0)  # floor at $1

    # ETF sweep amount per trigger ($50 threshold, 60% of profits)
    etf_sweep_rate = avg_daily * 0.60  # 60% of daily profits go to ETF
    days_per_sweep = 50 / max(etf_sweep_rate, 1)  # days between sweeps

    roadmap = {
        "positions":       positions,
        "cash":            cash,
        "capital":         capital,
        "avg_daily":       round(avg_daily, 2),
        "etf_bucket":      etf_bucket,
        "goals":           [],
        "priority_etf":    None,
        "swing_vs_etf":    "swing",  # default
        "total_monthly_premium_now":     0,
        "total_monthly_premium_unlocked": 0,
        "routing_decision": "",
    }

    total_premium_now     = 0
    total_premium_unlocked = 0

    for sym, cfg in sorted(ETF_ROADMAP.items(), key=lambda x: x[1]["priority"]):
        pos           = positions.get(sym, {})
        shares        = pos.get("shares", 0)
        avg_price     = pos.get("avg_price", cfg["approx_price"])
        target        = cfg["target_shares"]
        shares_needed = max(0, target - shares)
        cost_needed   = shares_needed * cfg["approx_price"]

        # Call unlock
        call_unlocked = shares >= target
        call_premium  = cfg["est_call_premium"] * 100 if call_unlocked else 0
        total_premium_now += call_premium

        # Put unlock (need cash collateral)
        put_strike    = cfg["approx_price"] * 0.97
        put_collateral = put_strike * 100
        put_unlocked  = cash >= put_collateral
        put_premium   = cfg["est_put_premium"] * 100 if put_unlocked else 0
        total_premium_now += put_premium

        # Full potential if unlocked
        full_call = cfg["est_call_premium"] * 100
        full_put  = cfg["est_put_premium"] * 100
        total_premium_unlocked += full_call + full_put

        # ETA calculations
        if shares_needed > 0:
            # Days to accumulate shares via ETF sweeps
            shares_per_sweep = 50 * 0.60 / max(cfg["approx_price"], 1)
            days_to_call = int(shares_needed / max(shares_per_sweep / days_per_sweep, 0.01))
        else:
            days_to_call = 0

        if not put_unlocked:
            cash_gap      = put_collateral - cash
            days_to_put   = int(cash_gap / max(avg_daily * 0.30, 1))  # 30% goes to cash
        else:
            days_to_put = 0

        # Tax hold check
        hold_days    = 0
        buy_date     = None
        tax_events   = ledger.get("tax_events", [])
        for ev in tax_events:
            if ev.get("symbol") == sym and ev.get("type") == "etf_buy":
                try:
                    buy_date  = datetime.strptime(ev["timestamp"][:10], "%Y-%m-%d")
                    hold_days = (datetime.now() - buy_date).days
                except Exception:
                    pass

        long_term    = hold_days >= 365
        tax_rate     = LT_RATE if long_term else ST_RATE
        after_tax_call = call_premium * (1 - ST_RATE)  # call premium always ST
        after_tax_put  = put_premium * (1 - ST_RATE)

        goal = {
            "symbol":          sym,
            "priority":        cfg["priority"],
            "category":        cfg["category"],
            "shares":          round(shares, 2),
            "target":          target,
            "shares_needed":   round(shares_needed, 2),
            "cost_needed":     round(cost_needed, 2),
            "pct_to_call":     round(shares / target * 100, 1),
            "call_unlocked":   call_unlocked,
            "call_premium":    round(call_premium, 2),
            "put_unlocked":    put_unlocked,
            "put_premium":     round(put_premium, 2),
            "put_collateral":  round(put_collateral, 2),
            "days_to_call":    days_to_call,
            "days_to_put":     days_to_put,
            "hold_days":       hold_days,
            "long_term":       long_term,
            "after_tax_call":  round(after_tax_call, 2),
            "after_tax_put":   round(after_tax_put, 2),
            "full_potential":  round(full_call + full_put, 2),
        }
        roadmap["goals"].append(goal)

    roadmap["total_monthly_premium_now"]      = round(total_premium_now, 2)
    roadmap["total_monthly_premium_unlocked"] = round(total_premium_unlocked, 2)

    # Priority ETF — closest to unlocking covered call
    incomplete = [g for g in roadmap["goals"] if not g["call_unlocked"] and g["shares_needed"] > 0]
    if incomplete:
        roadmap["priority_etf"] = sorted(incomplete, key=lambda x: x["cost_needed"])[0]["symbol"]

    # Smart routing decision
    # If ETF options monthly premium > 5 days of swing trading → route swing capital to ETF
    swing_5day = avg_daily * 5
    if total_premium_unlocked > swing_5day:
        roadmap["swing_vs_etf"]      = "etf_options"
        roadmap["routing_decision"]  = f"ETF options potential ${total_premium_unlocked:.0f}/mo beats 5 swing days ${swing_5day:.0f}"
    else:
        roadmap["swing_vs_etf"]      = "swing"
        roadmap["routing_decision"]  = f"Swing trading ${avg_daily:.0f}/day beats ETF options ${total_premium_unlocked:.0f}/mo for now"

    # Save to ledger
    ledger["roadmap_priority_etf"] = roadmap["priority_etf"]
    ledger["roadmap_swing_vs_etf"] = roadmap["swing_vs_etf"]
    save_ledger(ledger)

    return roadmap


def get_priority_etf() -> str:
    """Returns which ETF to prioritize in sweeps. Called by bot.py."""
    ledger = load_ledger()
    return ledger.get("roadmap_priority_etf", "SCHB")


def send_roadmap_alert(encrypted: str):
    """Send roadmap status to Telegram. Called in pre-market."""
    roadmap = calculate_roadmap(encrypted)
    goals   = roadmap["goals"]

    msg  = "[ CIRCUIT ] ROADMAP\n"
    msg += "━━━━━━━━━━━━━━━━━━\n"

    # Tier 2 progress
    capital    = roadmap["capital"]
    tier2_gap  = max(5400 - capital, 0)
    tier2_pct  = min(capital / 5400 * 100, 100)
    tier2_days = int(tier2_gap / max(roadmap["avg_daily"], 1))
    msg += f"TIER 2  ${capital:,.0f}/$5,400  {tier2_pct:.0f}%  {tier2_days}d\n"
    msg += "━━━━━━━━━━━━━━━━━━\n"

    # ETF options goals
    for g in goals[:4]:  # top 4
        sym  = g["symbol"]
        pct  = g["pct_to_call"]
        days = g["days_to_call"]
        lock = "✓" if g["call_unlocked"] else f"{days}d"
        msg += f"{sym}  {g['shares']:.0f}/100  {pct:.0f}%  {lock}\n"

    msg += "━━━━━━━━━━━━━━━━━━\n"

    # Cash secured put progress
    next_put = next((g for g in goals if not g["put_unlocked"]), None)
    if next_put:
        cash     = roadmap["cash"]
        collat   = next_put["put_collateral"]
        put_pct  = min(cash / collat * 100, 100)
        put_days = next_put["days_to_put"]
        msg += f"{next_put['symbol']} PUT  ${cash:,.0f}/${collat:,.0f}  {put_pct:.0f}%  {put_days}d\n"
        msg += "━━━━━━━━━━━━━━━━━━\n"

    # Premium potential
    msg += f"PREM NOW  ${roadmap['total_monthly_premium_now']:,.0f}/mo\n"
    msg += f"PREM FULL ${roadmap['total_monthly_premium_unlocked']:,.0f}/mo\n"
    msg += "━━━━━━━━━━━━━━━━━━\n"

    # Next goal
    priority = roadmap["priority_etf"]
    if priority:
        msg += f"NEXT: {priority} covered call"
    else:
        msg += "ALL CALLS UNLOCKED"

    send_alert(msg)
    return roadmap


def get_etf_sweep_priority() -> list:
    """
    Returns ordered list of ETFs to buy in sweeps.
    Bot.py calls this to route ETF bucket purchases.
    Prioritizes by closest to covered call unlock.
    """
    ledger  = load_ledger()
    goals   = []

    # Load last calculated roadmap from ledger
    priority = ledger.get("roadmap_priority_etf", "SCHB")

    # Return priority order
    order = sorted(ETF_ROADMAP.items(), key=lambda x: x[1]["priority"])
    return [sym for sym, _ in order]


if __name__ == "__main__":
    import sys
    resp      = requests.get(
        f"{BASE_URL}/accounts/accountNumbers",
        headers=headers(), timeout=10
    )
    encrypted = resp.json()[0]["hashValue"]

    roadmap = calculate_roadmap(encrypted)

    print(f"\n{'='*55}")
    print(f"CIRCUIT ROADMAP — ETF Options Path")
    print(f"{'='*55}")
    print(f"Capital: ${roadmap['capital']:,.0f} | Avg daily: ${roadmap['avg_daily']:.2f}")
    print(f"Premium NOW:  ${roadmap['total_monthly_premium_now']:,.0f}/month")
    print(f"Premium FULL: ${roadmap['total_monthly_premium_unlocked']:,.0f}/month")
    print(f"Routing: {roadmap['routing_decision']}\n")

    for g in roadmap["goals"]:
        print(f"{g['symbol']} — Priority {g['priority']}")
        print(f"  Shares:    {g['shares']:.1f}/100 ({g['pct_to_call']:.0f}%)")
        print(f"  Need:      {g['shares_needed']:.0f} shares = ${g['cost_needed']:,.0f}")
        print(f"  Call ETA:  {g['days_to_call']} days")
        print(f"  Put ETA:   {g['days_to_put']} days (${g['put_collateral']:,.0f} collateral)")
        print(f"  Premium:   ${g['full_potential']:.0f}/mo potential (after tax: ${g['after_tax_call']+g['after_tax_put']:.0f})")
        print(f"  Hold:      {g['hold_days']} days {'(LT rate)' if g['long_term'] else '(ST rate)'}")
        print()

    print(f"Priority ETF: {roadmap['priority_etf']}")
    print(f"{'='*55}\n")

    # Send to Telegram
    send_roadmap_alert(encrypted)
