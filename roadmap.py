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


def scan_cheap_optionable_etfs(max_price: float = 50.0) -> list:
    """
    Use Schwab live scanner to find cheap ETFs with options.
    Looks for ETFs under max_price with liquid options chains.
    Cheaper = faster to 100 shares = faster option unlock.
    """
    # Schwab ETF movers and known cheap optionable ETFs
    candidates = [
        # Already own — always track
        "SCHB", "SCHG", "SCHD", "VOO", "QQQ", "VTI",

        # Cheap non-leveraged ETFs under $30 — good for long-term hold
        "GDX",   # gold miners ~$40 range
        "GDXJ",  # junior gold miners ~$40
        "SLV",   # silver ~$25
        "XLF",   # financials ~$45
        "XLE",   # energy ~$90 (skip if too expensive)
        "XLV",   # healthcare ~$140 (skip if too expensive)
        "EEM",   # emerging markets ~$45
        "HYG",   # high yield bonds ~$77
        "LQD",   # investment grade bonds ~$110
        "IWM",   # small cap ~$215

        # Income ETFs with good premiums — safe long term hold
        "JEPI",  # ~$60 high income
        "JEPQ",  # ~$55 high income
        "QYLD",  # ~$16 covered call income — cheap!
        "RYLD",  # ~$18 covered call income — cheap!
        "DIVO",  # ~$45 dividend growth
        "XYLD",  # ~$44 S&P covered calls

        # Sector ETFs cheap enough
        "ARKK",  # ~$55 innovation
        "EFA",   # ~$80 international developed
        "VWO",   # ~$45 emerging markets
        "SCHF",  # ~$35 international
        "SCHA",  # ~$25 small cap — cheap!
        "SCHM",  # ~$28 mid cap — cheap!
        "SCHV",  # ~$30 value
        "SCHX",  # ~$30 large cap
    ]

    results = []
    for sym in candidates:
        try:
            resp = requests.get(
                f"{MARKET_URL}/quotes/{sym}",
                headers=headers(), timeout=8
            )
            if not resp.ok:
                continue
            data  = resp.json().get(sym, {})
            quote = data.get("quote", {})
            price = quote.get("lastPrice", 0)
            vol   = quote.get("totalVolume", 0)

            if price <= 0 or price > max_price:
                continue
            if vol < 500_000:  # need liquid ETF
                continue

            # Check if options exist
            chain_resp = requests.get(
                f"{MARKET_URL}/chains",
                headers=headers(),
                params={"symbol": sym, "strikeCount": 3,
                        "optionType": "CALL", "strategy": "SINGLE"},
                timeout=8
            )
            if not chain_resp.ok:
                continue
            chain = chain_resp.json()
            if not chain.get("callExpDateMap"):
                continue

            # Get best call premium
            best_prem = 0
            for expiry, strikes in chain.get("callExpDateMap", {}).items():
                try:
                    dte = int(expiry.split(":")[1])
                except Exception:
                    continue
                if not (14 <= dte <= 45):
                    continue
                for strike_str, opts in strikes.items():
                    strike = float(strike_str)
                    if not (price * 1.01 <= strike <= price * 1.06):
                        continue
                    opt    = opts[0] if opts else None
                    if opt:
                        bid  = opt.get("bid", 0)
                        ask  = opt.get("ask", 0)
                        prem = (bid + ask) / 2
                        if prem > best_prem:
                            best_prem = prem

            if best_prem < 0.05:
                continue

            # Cost to 100 shares
            cost_to_100  = price * 100
            premium_yield = best_prem / price * 100  # monthly yield %
            after_tax_prem = best_prem * 100 * (1 - ST_RATE)

            # Only include if it has a reasonable expense ratio signal
            # (ETFs trade near NAV, stocks don't — use volume/price ratio as proxy)
            results.append({
                "symbol":        sym,
                "price":         round(price, 2),
                "volume":        vol,
                "cost_to_100":   round(cost_to_100, 2),
                "best_premium":  round(best_prem, 2),
                "premium_yield": round(premium_yield, 2),
                "after_tax_100": round(after_tax_prem, 2),
                "score":         round(premium_yield * (1000 / cost_to_100), 2),
                "long_term_ok":  True,  # all in this list are hold-eligible
            })

        except Exception:
            continue

    # Sort by score (best yield per dollar invested)
    return sorted(results, key=lambda x: x["score"], reverse=True)


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
        after_tax_call = full_call * (1 - ST_RATE)  # use full potential not current
        after_tax_put  = full_put  * (1 - ST_RATE)

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

    # Priority ETF — closest to unlocking covered call (cheapest first)
    incomplete = [g for g in roadmap["goals"] if not g["call_unlocked"] and g["shares_needed"] > 0]
    if incomplete:
        roadmap["priority_etf"] = sorted(incomplete, key=lambda x: x["cost_needed"])[0]["symbol"]

    # Scan live for better cheap ETF opportunities
    try:
        live_etfs = scan_cheap_optionable_etfs(max_price=50.0)
        roadmap["live_opportunities"] = live_etfs[:5]

        # Add live ETFs to goals if better than current
        for etf in live_etfs[:3]:
            sym = etf["symbol"]
            if sym not in [g["symbol"] for g in roadmap["goals"]]:
                pos = positions.get(sym, {})
                shares = pos.get("shares", 0)
                shares_needed = max(0, 100 - shares)
                roadmap["goals"].append({
                    "symbol":         sym,
                    "priority":       10,
                    "category":       "live_scan",
                    "shares":         round(shares, 2),
                    "target":         100,
                    "shares_needed":  round(shares_needed, 2),
                    "cost_needed":    round(shares_needed * etf["price"], 2),
                    "pct_to_call":    round(shares / 100 * 100, 1),
                    "call_unlocked":  shares >= 100,
                    "call_premium":   etf["best_premium"] * 100 if shares >= 100 else 0,
                    "put_unlocked":   False,
                    "put_premium":    0,
                    "put_collateral": round(etf["price"] * 97, 2),
                    "days_to_call":   int(shares_needed * etf["price"] / max(avg_daily * 0.60 / etf["price"], 0.01)),
                    "days_to_put":    int(etf["price"] * 97 / max(avg_daily * 0.30, 1)),
                    "hold_days":      0,
                    "long_term":      False,
                    "after_tax_call": etf["after_tax_100"],
                    "after_tax_put":  0,
                    "full_potential": etf["best_premium"] * 100,
                    "premium_yield":  etf["premium_yield"],
                    "live_scan":      True,
                })
    except Exception as ex:
        print(f"Live scan error: {ex}")
        roadmap["live_opportunities"] = []

    # Smart priority — score by monthly ROI on capital to unlock
    # Best score = fastest path to options income per dollar spent
    for g in roadmap["goals"]:
        cost_to_unlock = g["cost_needed"] if g["cost_needed"] > 0 else 1
        monthly_prem   = g["full_potential"]
        # ROI score = premium per dollar × speed bonus for cheaper ETFs
        speed_bonus    = 1000 / max(cost_to_unlock, 1)  # cheaper = faster unlock
        g["roi_score"] = round((monthly_prem / max(cost_to_unlock, 1)) * 100 + speed_bonus, 4)

    # Sort by ROI score — best return per dollar first
    all_goals = sorted(
        [g for g in roadmap["goals"] if not g["call_unlocked"] and g["shares_needed"] > 0],
        key=lambda x: x["roi_score"],
        reverse=True
    )

    if all_goals:
        roadmap["priority_etf"] = all_goals[0]["symbol"]

    # Split sweep: 60% to #1, 25% to #2, 15% to #3
    sweep_split = {}
    weights = [0.60, 0.25, 0.15]
    for i, g in enumerate(all_goals[:3]):
        sweep_split[g["symbol"]] = weights[i]
    roadmap["sweep_split"] = sweep_split

    # Print ROI ranking for terminal output
    print("ETF Priority by ROI:")
    for g in all_goals[:6]:
        print(f"  {g['symbol']}: roi={g['roi_score']:.3f} cost=${g['cost_needed']:,.0f} prem=${g['full_potential']:.0f}/mo")

    # Smart routing decision — compare ACTUAL premium vs opportunity cost
    # Opportunity cost = 15% swing capital x avg daily rate
    swing_capital      = capital * 0.75
    redirect_amount    = swing_capital * 0.15
    opportunity_cost   = (redirect_amount / max(capital, 1)) * avg_daily  # daily opportunity cost

    # Only redirect if actual unlocked premium covers opportunity cost
    actual_daily_prem  = total_premium_now / 30  # actual monthly premium / 30 days
    redirect_worthwhile = actual_daily_prem > opportunity_cost

    if redirect_worthwhile:
        roadmap["swing_vs_etf"]     = "etf_options"
        roadmap["routing_decision"] = f"Actual premium ${actual_daily_prem:.2f}/day > opp cost ${opportunity_cost:.2f}/day — redirect ON"
    else:
        roadmap["swing_vs_etf"]     = "swing"
        roadmap["routing_decision"] = f"Swing wins — actual premium ${actual_daily_prem:.2f}/day < opp cost ${opportunity_cost:.2f}/day"

    roadmap["actual_daily_prem"]   = round(actual_daily_prem, 2)
    roadmap["opportunity_cost"]    = round(opportunity_cost, 2)
    roadmap["redirect_worthwhile"] = redirect_worthwhile

    # Save to ledger
    ledger["roadmap_priority_etf"] = roadmap["priority_etf"]
    ledger["roadmap_sweep_split"]  = sweep_split
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

    # Check upgrades and alert if any
    try:
        upgrades = check_etf_upgrades(encrypted)
        if upgrades:
            send_upgrade_alert(upgrades)
    except Exception:
        pass

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


def check_etf_upgrades(encrypted: str) -> list:
    """
    Check if any ETFs are worth upgrading after 1+ year hold.
    Only upgrades when:
    1. Held 1+ year (long term tax rate)
    2. New ETF premium > old ETF premium x 1.5
    3. After-tax proceeds cover 50+ shares of new ETF
    4. New ETF closer to 100 shares than current
    Returns list of recommended upgrades.
    """
    ledger    = load_ledger()
    positions = get_etf_positions(encrypted)
    tax_events = ledger.get("tax_events", [])
    upgrades  = []

    # Target ETFs ranked by premium potential
    upgrade_targets = [
        {"symbol": "QQQ",  "price": 565, "monthly_premium": 500, "shares_needed": 100},
        {"symbol": "VOO",  "price": 640, "monthly_premium": 450, "shares_needed": 100},
        {"symbol": "JEPI", "price": 60,  "monthly_premium": 95,  "shares_needed": 100},
        {"symbol": "IWM",  "price": 215, "monthly_premium": 180, "shares_needed": 100},
        {"symbol": "QYLD", "price": 16,  "monthly_premium": 20,  "shares_needed": 100},
        {"symbol": "RYLD", "price": 18,  "monthly_premium": 22,  "shares_needed": 100},
    ]

    for sym, pos in positions.items():
        shares    = pos.get("shares", 0)
        avg_price = pos.get("avg_price", 0)
        mkt_value = pos.get("mkt_value", 0)

        if shares < 10:
            continue

        # Check hold time
        hold_days = 0
        for ev in tax_events:
            if ev.get("symbol") == sym:
                try:
                    buy_date  = datetime.strptime(ev["timestamp"][:10], "%Y-%m-%d")
                    hold_days = (datetime.now() - buy_date).days
                except Exception:
                    pass

        if hold_days < 365:
            continue  # not long term yet

        # Current ETF premium potential
        cfg          = ETF_ROADMAP.get(sym, {})
        current_prem = cfg.get("est_call_premium", 0.10) * 100

        # Calculate after-tax proceeds
        cost_basis   = shares * avg_price
        gain         = mkt_value - cost_basis
        tax_owed     = max(gain, 0) * LT_RATE
        net_proceeds = mkt_value - tax_owed

        # Check each upgrade target
        for target in upgrade_targets:
            tsym  = target["symbol"]
            tprem = target["monthly_premium"]

            # Skip if same ETF or already own 100 shares
            if tsym == sym:
                continue
            target_pos = positions.get(tsym, {})
            if target_pos.get("shares", 0) >= 100:
                continue

            # Worth it checks
            premium_improvement = tprem / max(current_prem, 1)
            if premium_improvement < 1.5:
                continue  # not 50% better

            # Can afford — need proceeds to cover 50+ shares
            shares_can_buy = int(net_proceeds // target["price"])
            if shares_can_buy < 50:
                continue  # can't afford meaningful position

            # ROI check — new premium per dollar > old premium per dollar
            new_roi = tprem / (shares_can_buy * target["price"])
            old_roi = current_prem / max(mkt_value, 1)
            if new_roi <= old_roi:
                continue  # no improvement

            upgrades.append({
                "sell_symbol":    sym,
                "sell_shares":    int(shares),
                "sell_proceeds":  round(net_proceeds, 2),
                "tax_owed":       round(tax_owed, 2),
                "hold_days":      hold_days,
                "buy_symbol":     tsym,
                "shares_can_buy": shares_can_buy,
                "cost":           round(shares_can_buy * target["price"], 2),
                "old_premium":    round(current_prem, 2),
                "new_premium":    round(tprem * (shares_can_buy / 100), 2),
                "improvement":    round((premium_improvement - 1) * 100, 1),
                "worthy":         True,
            })
            break  # only recommend one upgrade per ETF

    return upgrades


def send_upgrade_alert(upgrades: list):
    """Send ETF upgrade recommendations to Telegram."""
    if not upgrades:
        return
    parts = ["[ CIRCUIT ] ETF UPGRADES"]
    for u in upgrades:
        parts.append("SELL  " + u["sell_symbol"] + " x" + str(u["sell_shares"]))
        parts.append("TAX   $" + f"{u['tax_owed']:,.2f}" + " (" + str(u["hold_days"]) + "d LT)")
        parts.append("NET   $" + f"{u['sell_proceeds']:,.2f}")
        parts.append("BUY   " + u["buy_symbol"] + " x" + str(u["shares_can_buy"]))
        parts.append("PREM  $" + f"{u['old_premium']:.0f}" + " to $" + f"{u['new_premium']:.0f}" + "/mo (+" + f"{u['improvement']:.0f}" + "%)")
    send_alert("\n".join(parts))


# ── Roadmap Brain — influences all bot decisions ─────────────────────────────

def get_brain_state(encrypted: str = None) -> dict:
    """
    Returns current brain state for bot to use in every decision.
    Called by bot.py before every strategy check.
    """
    ledger  = load_ledger()
    capital = ledger.get("trading_capital", 2220)

    # Tier progress
    tiers = [
        {"name": "Tier 1", "threshold": 0,     "ceiling": 200},
        {"name": "Tier 2", "threshold": 5000,  "ceiling": 400},
        {"name": "Tier 3", "threshold": 15000, "ceiling": 600},
        {"name": "Tier 4", "threshold": 50000, "ceiling": 1000},
    ]
    current_tier  = tiers[0]
    next_tier     = tiers[1]
    for i, t in enumerate(tiers):
        if capital >= t["threshold"]:
            current_tier = t
            next_tier    = tiers[i+1] if i+1 < len(tiers) else t

    tier_gap     = next_tier["threshold"] - capital
    tier_pct     = min(capital / max(next_tier["threshold"], 1) * 100, 100)
    near_upgrade = tier_gap < capital * 0.20  # within 20% of next tier

    # ETF unlock progress
    priority_etf   = ledger.get("roadmap_priority_etf", "SCHB")
    sweep_split    = ledger.get("roadmap_sweep_split", {priority_etf: 0.70})
    redirect_on    = ledger.get("roadmap_swing_vs_etf", "swing") == "etf_options"

    # Daily P&L history
    daily_history  = ledger.get("daily_pnl_history", [])
    avg_daily      = sum(daily_history) / len(daily_history) if daily_history else 79.98
    today_pnl      = ledger.get("daily_profit", 0)
    losing_today   = today_pnl < -50

    # Weekly P&L
    weekly_history = ledger.get("weekly_pnl_history", [])
    avg_weekly     = sum(weekly_history) / len(weekly_history) if weekly_history else avg_daily * 5

    # ETF bucket proximity to sweep threshold
    etf_bucket     = ledger.get("etf_bucket", 0)
    near_sweep     = etf_bucket >= 30  # close to $50 threshold

    # Profit split adjustment based on roadmap
    # Near tier upgrade → push more to bot bucket
    # Near ETF unlock → push more to ETF bucket
    if near_upgrade:
        profit_split = {"etf": 0.50, "cash": 0.25, "bot": 0.25}  # more to bot
        split_reason = "near_tier_upgrade"
    elif near_sweep:
        profit_split = {"etf": 0.70, "cash": 0.20, "bot": 0.10}  # more to ETF
        split_reason = "near_etf_sweep"
    else:
        profit_split = {"etf": 0.60, "cash": 0.30, "bot": 0.10}  # normal
        split_reason = "normal"

    # Position sizing adjustment
    # Losing today → conservative (80% of normal)
    # Near tier upgrade → aggressive (110% of normal)
    if losing_today:
        size_mult = 0.80
        size_reason = "losing_today_conservative"
    elif near_upgrade:
        size_mult = 1.10
        size_reason = "near_upgrade_aggressive"
    else:
        size_mult = 1.00
        size_reason = "normal"

    # ETF sweep threshold adjustment
    # Within 10 shares of unlock → lower threshold to $30
    sweep_threshold = 30 if near_sweep else 50

    return {
        "capital":         round(capital, 2),
        "current_tier":    current_tier["name"],
        "next_tier":       next_tier["name"],
        "tier_gap":        round(tier_gap, 2),
        "tier_pct":        round(tier_pct, 1),
        "near_upgrade":    near_upgrade,
        "priority_etf":    priority_etf,
        "sweep_split":     sweep_split,
        "redirect_on":     redirect_on,
        "avg_daily":       round(avg_daily, 2),
        "today_pnl":       round(today_pnl, 2),
        "losing_today":    losing_today,
        "profit_split":    profit_split,
        "split_reason":    split_reason,
        "size_mult":       size_mult,
        "size_reason":     size_reason,
        "sweep_threshold": sweep_threshold,
        "etf_bucket":      round(etf_bucket, 2),
    }


def check_milestones(brain: dict) -> list:
    """
    Check if any milestones were just hit.
    Returns list of milestone alerts to send.
    """
    ledger     = load_ledger()
    milestones = []
    seen       = set(ledger.get("seen_milestones", []))

    capital = brain["capital"]

    # Tier milestones
    tier_thresholds = {
        "tier2_unlock": (5000,  "TIER 2 UNLOCKED\nCeiling $400 | Day $1,350 | Swing $4,050"),
        "tier3_unlock": (15000, "TIER 3 UNLOCKED\nCeiling $600 | Day $3,750 | Swing $11,250"),
        "tier4_unlock": (50000, "TIER 4 UNLOCKED\nIncome phase begins"),
    }
    for key, (threshold, msg) in tier_thresholds.items():
        if capital >= threshold and key not in seen:
            milestones.append(("[ CIRCUIT ] MILESTONE\n" + msg + "\nETA Tier 2: " + str(int((5000-capital)/max(brain["avg_daily"],1))) + "d", key))

    # Capital milestones
    cap_milestones = {
        "cap_3k":  (3000,  "Capital hit $3,000"),
        "cap_4k":  (4000,  "Capital hit $4,000"),
        "cap_5k":  (5000,  "Capital hit $5,000"),
        "cap_10k": (10000, "Capital hit $10,000"),
        "cap_15k": (15000, "Capital hit $15,000"),
    }
    for key, (threshold, msg) in cap_milestones.items():
        if capital >= threshold and key not in seen:
            milestones.append(("[ CIRCUIT ] MILESTONE\n" + msg + "\nETA Tier 2: " + str(int((5000-capital)/max(brain["avg_daily"],1))) + "d", key))

    # Mark seen
    if milestones:
        new_seen = list(seen) + [m[1] for m in milestones]
        ledger["seen_milestones"] = new_seen
        save_ledger(ledger)

    return milestones


def send_weekly_report(brain: dict):
    """Weekly P&L + tier progress report. Called every Sunday."""
    ledger       = load_ledger()
    capital      = brain["capital"]
    avg_daily    = brain["avg_daily"]
    tier_gap     = brain["tier_gap"]
    next_tier    = brain["next_tier"]
    days_to_tier = int(tier_gap / max(avg_daily, 1))

    # Weekly P&L
    daily_history = ledger.get("daily_pnl_history", [])
    week_pnl      = sum(daily_history[-5:]) if len(daily_history) >= 5 else sum(daily_history)

    # ETF progress
    priority     = brain["priority_etf"]
    etf_pct      = 0
    etf_cfg      = ETF_ROADMAP.get(priority, {})

    parts = [
        "[ CIRCUIT ] WEEKLY",
        "P&L    $" + f"{week_pnl:+,.2f}" + " this week",
        "DAILY  $" + f"{avg_daily:.2f}" + " avg",
        "CAP    $" + f"{capital:,.2f}",
        f"{next_tier} gap: $" + f"{tier_gap:,.0f}" + " — " + str(days_to_tier) + "d",
        "NEXT ETF: " + priority,
    ]
    send_alert("\n".join(parts))


def record_weekly_pnl(weekly_pnl: float):
    """Record weekly P&L for trend tracking."""
    ledger = load_ledger()
    history = ledger.get("weekly_pnl_history", [])
    history.append(weekly_pnl)
    ledger["weekly_pnl_history"] = history[-12:]  # keep 12 weeks
    save_ledger(ledger)


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
