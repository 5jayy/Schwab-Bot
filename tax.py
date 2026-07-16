"""
Tax Tracker — Maryland rates
Tracks all taxable events persistently across deploys.
Pulls full year from Schwab API to verify history.
Calculates exact tax owed and which ETFs to sell to pay it.

Run anytime: python3 tax.py
Auto-runs in April for tax time alert.

Maryland rates:
- Short term (held <1 year): 37.75% (federal + MD state)
- Long term  (held >1 year): 20.75% (federal + MD state)
- Qualified dividends:        20.75%
"""

import requests
import json
import os
import time
from datetime import datetime, timedelta
from auth import get_valid_token
from ledger import load_ledger, save_ledger
from telegram import send_alert

BASE_URL = "https://api.schwabapi.com/trader/v1"

# Maryland tax rates
SHORT_TERM_RATE  = 0.3775
LONG_TERM_RATE   = 0.2075
DIVIDEND_RATE    = 0.2075


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_schwab_trades_for_year(encrypted: str, year: int = None) -> list:
    """
    Pull all TRADE transactions from Schwab for the given year.
    Schwab API goes back up to 1 year.
    Returns list of completed trades with buy/sell pairs.
    """
    if year is None:
        year = datetime.now().year

    start = f"{year}-01-01T00:00:00.000Z"
    end   = f"{year}-12-31T23:59:59.000Z"

    try:
        resp = requests.get(
            f"{BASE_URL}/accounts/{encrypted}/transactions",
            headers=headers(),
            params={
                "startDate": start,
                "endDate":   end,
                "types":     "TRADE"
            },
            timeout=30
        )
        resp.raise_for_status()
        return resp.json() if isinstance(resp.json(), list) else []
    except Exception as ex:
        print(f"Schwab trade history error: {ex}")
        return []


def get_schwab_dividends_for_year(encrypted: str, year: int = None) -> list:
    """Pull all dividend transactions for the year."""
    if year is None:
        year = datetime.now().year

    start = f"{year}-01-01T00:00:00.000Z"
    end   = f"{year}-12-31T23:59:59.000Z"

    try:
        resp = requests.get(
            f"{BASE_URL}/accounts/{encrypted}/transactions",
            headers=headers(),
            params={
                "startDate": start,
                "endDate":   end,
                "types":     "DIVIDEND_OR_INTEREST"
            },
            timeout=30
        )
        resp.raise_for_status()
        return resp.json() if isinstance(resp.json(), list) else []
    except Exception as ex:
        print(f"Schwab dividend history error: {ex}")
        return []


def sync_schwab_tax_history(encrypted: str):
    """
    Pull full year from Schwab and record any missing tax events.
    Run on startup in January and close to tax time.
    """
    ledger   = load_ledger()
    year     = datetime.now().year
    seen_ids = set(ledger.get("tax_seen_schwab_ids", []))

    trades    = get_schwab_trades_for_year(encrypted, year)
    dividends = get_schwab_dividends_for_year(encrypted, year)

    new_events = 0

    # Process sells from Schwab trades
    for txn in trades:
        txn_id = str(txn.get("activityId", txn.get("transactionId", "")))
        if txn_id in seen_ids:
            continue

        # Only process sells
        txn_desc = txn.get("description", "").upper()
        net      = txn.get("netAmount", 0)

        if "SOLD" in txn_desc and net > 0:
            symbol    = ""
            for item in txn.get("transferItems", []):
                if item.get("instrument", {}).get("symbol"):
                    symbol = item["instrument"]["symbol"]
                    break

            # Estimate hold time from closed_trades in ledger
            hold_days = 180  # default to short term if unknown
            for ct in ledger.get("closed_trades", []):
                if ct.get("symbol") == symbol:
                    try:
                        bought = datetime.strptime(ct["bought_at"][:10], "%Y-%m-%d")
                        sold   = datetime.strptime(ct["closed_at"][:10], "%Y-%m-%d")
                        hold_days = (sold - bought).days
                        break
                    except Exception:
                        pass

            long_term = hold_days >= 365
            rate      = LONG_TERM_RATE if long_term else SHORT_TERM_RATE
            cost_basis = abs(txn.get("netAmount", 0))  # approximate

            event = {
                "txn_id":     txn_id,
                "symbol":     symbol,
                "profit":     net,
                "hold_days":  hold_days,
                "long_term":  long_term,
                "rate":       rate,
                "tax_owed":   max(net, 0) * rate,
                "type":       "stock",
                "source":     "schwab_sync",
                "timestamp":  txn.get("tradeDate", ""),
                "year":       year
            }
            ledger.setdefault("tax_events", []).append(event)
            seen_ids.add(txn_id)
            new_events += 1

    # Process dividends
    for txn in dividends:
        txn_id = str(txn.get("activityId", txn.get("transactionId", "")))
        if txn_id in seen_ids:
            continue

        amount = abs(txn.get("netAmount", 0))
        if amount > 0:
            symbol = ""
            for item in txn.get("transferItems", []):
                if item.get("instrument", {}).get("symbol"):
                    symbol = item["instrument"]["symbol"]
                    break

            event = {
                "txn_id":    txn_id,
                "symbol":    symbol,
                "profit":    amount,
                "hold_days": 365,
                "long_term": True,
                "rate":      DIVIDEND_RATE,
                "tax_owed":  amount * DIVIDEND_RATE,
                "type":      "dividend",
                "source":    "schwab_sync",
                "timestamp": txn.get("tradeDate", ""),
                "year":      year
            }
            ledger.setdefault("tax_events", []).append(event)
            seen_ids.add(txn_id)
            new_events += 1

    ledger["tax_seen_schwab_ids"] = list(seen_ids)[-500:]
    save_ledger(ledger)
    print(f"Tax sync: {new_events} new events recorded")
    return new_events


def record_taxable_event(symbol: str, profit: float, hold_days: int,
                          event_type: str = "stock", txn_id: str = None):
    """
    Record every taxable event from bot trades.
    Persists to /data/trade_ledger.json — survives all deploys.
    """
    ledger    = load_ledger()
    year      = datetime.now().year
    long_term = hold_days >= 365
    rate      = LONG_TERM_RATE if long_term else SHORT_TERM_RATE

    if event_type == "dividend":
        rate = DIVIDEND_RATE

    event = {
        "txn_id":    txn_id or f"{symbol}_{int(time.time())}",
        "symbol":    symbol,
        "profit":    profit,
        "hold_days": hold_days,
        "long_term": long_term,
        "rate":      rate,
        "tax_owed":  max(profit, 0) * rate,
        "type":      event_type,
        "source":    "bot",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "year":      year
    }
    ledger.setdefault("tax_events", []).append(event)

    # Running YTD total
    ledger["ytd_tax_owed"] = sum(
        e["tax_owed"] for e in ledger["tax_events"]
        if e.get("year") == year and e["profit"] > 0
    )
    save_ledger(ledger)


def get_tax_report(year: int = None) -> dict:
    """
    Full tax report for the year.
    Pulls from persistent ledger — works after any deploy.
    """
    if year is None:
        year = datetime.now().year

    ledger = load_ledger()
    events = [e for e in ledger.get("tax_events", []) if e.get("year") == year]

    short_gains  = sum(e["profit"] for e in events if e["profit"] > 0 and not e["long_term"] and e["type"] in ("stock", "options"))
    short_losses = sum(e["profit"] for e in events if e["profit"] < 0 and not e["long_term"])
    long_gains   = sum(e["profit"] for e in events if e["profit"] > 0 and e["long_term"] and e["type"] != "dividend")
    dividends    = sum(e["profit"] for e in events if e["type"] == "dividend")
    options_inc  = sum(e["profit"] for e in events if e["type"] == "options" and e["profit"] > 0)

    net_short  = short_gains + short_losses
    tax_short  = max(net_short, 0)  * SHORT_TERM_RATE
    tax_long   = max(long_gains, 0) * LONG_TERM_RATE
    tax_div    = max(dividends, 0)  * DIVIDEND_RATE
    tax_opts   = max(options_inc, 0) * SHORT_TERM_RATE
    total_owed = tax_short + tax_long + tax_div + tax_opts

    return {
        "year":              year,
        "short_term_gains":  round(short_gains, 2),
        "short_term_losses": round(short_losses, 2),
        "net_short_term":    round(net_short, 2),
        "long_term_gains":   round(long_gains, 2),
        "options_income":    round(options_inc, 2),
        "dividends":         round(dividends, 2),
        "tax_short_term":    round(tax_short, 2),
        "tax_long_term":     round(tax_long, 2),
        "tax_options":       round(tax_opts, 2),
        "tax_dividends":     round(tax_div, 2),
        "total_tax_owed":    round(total_owed, 2),
        "ytd_events":        len(events),
        "md_rate_short":     f"{SHORT_TERM_RATE*100:.2f}%",
        "md_rate_long":      f"{LONG_TERM_RATE*100:.2f}%",
    }


def get_etf_tax_exit_plan(encrypted: str, tax_owed: float) -> list:
    """
    Calculate minimum ETF shares to sell to cover tax bill.
    Prioritizes ETFs held 1+ year (long term rate = lower taxes on the sale).
    Never sells ETFs held less than 1 year.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/accounts/{encrypted}?fields=positions",
            headers=headers(),
            timeout=15
        )
        resp.raise_for_status()
        positions = resp.json()["securitiesAccount"].get("positions", [])
    except Exception as ex:
        print(f"Position fetch error: {ex}")
        return []

    etf_symbols = {"VOO", "QQQ", "SCHG", "SCHB", "VTI", "SCHD", "JEPI",
                   "JEPQ", "VYM", "HDV", "SGOV", "USFR", "JPST", "VEA", "VWO"}

    candidates = []
    ledger     = load_ledger()

    for pos in positions:
        sym = pos["instrument"]["symbol"]
        if sym not in etf_symbols:
            continue

        qty      = pos.get("longQuantity", 0)
        avg_cost = pos.get("averagePrice", 0)
        mkt_val  = pos.get("marketValue", 0)
        gain     = mkt_val - (qty * avg_cost)

        # Only sell ETFs with gains held 1+ year
        # Check ledger for purchase date
        buy_date   = None
        etf_trades = [e for e in ledger.get("tax_events", []) if e.get("symbol") == sym]
        if etf_trades:
            try:
                buy_date = datetime.strptime(etf_trades[0]["timestamp"][:10], "%Y-%m-%d")
            except Exception:
                pass

        hold_days = (datetime.now() - buy_date).days if buy_date else 0

        if hold_days >= 365 and gain > 0:
            price_per_share = mkt_val / qty if qty > 0 else 0
            tax_on_sale     = gain * LONG_TERM_RATE
            candidates.append({
                "symbol":          sym,
                "shares_owned":    qty,
                "price":           round(price_per_share, 2),
                "gain":            round(gain, 2),
                "hold_days":       hold_days,
                "tax_on_sale_pct": LONG_TERM_RATE,
            })

    # Sort by best gain (sell most profitable first)
    candidates.sort(key=lambda x: x["gain"], reverse=True)

    # Calculate how many shares to sell
    plan        = []
    remaining   = tax_owed
    for c in candidates:
        if remaining <= 0:
            break
        shares_to_sell = min(
            c["shares_owned"],
            int(remaining / c["price"]) + 1
        )
        proceeds        = shares_to_sell * c["price"]
        plan.append({
            "symbol":        c["symbol"],
            "shares_to_sell": shares_to_sell,
            "proceeds":       round(proceeds, 2),
            "hold_days":      c["hold_days"],
        })
        remaining -= proceeds

    return plan


def send_tax_alert(encrypted: str):
    """Send tax summary to Telegram. Run automatically in April."""
    report = get_tax_report()
    plan   = get_etf_tax_exit_plan(encrypted, report["total_tax_owed"])

    msg  = f"Tax Report {report['year']}\n"
    msg += f"Short term gains: ${report['short_term_gains']:,.2f}\n"
    msg += f"Long term gains:  ${report['long_term_gains']:,.2f}\n"
    msg += f"Options income:   ${report['options_income']:,.2f}\n"
    msg += f"Dividends:        ${report['dividends']:,.2f}\n"
    msg += f"TOTAL TAX OWED:   ${report['total_tax_owed']:,.2f}\n"
    msg += f"MD rates: ST {report['md_rate_short']} / LT {report['md_rate_long']}\n\n"

    if plan:
        msg += "ETF exit plan to cover taxes:\n"
        for p in plan:
            msg += f"Sell {p['shares_to_sell']} {p['symbol']} = ${p['proceeds']:,.2f}\n"
    else:
        msg += "No long-term ETF gains available yet to cover taxes.\n"

    send_alert(msg)
    return report


if __name__ == "__main__":
    import sys
    from ledger import load_ledger

    # Get account
    resp      = requests.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers",
                             headers=headers())
    encrypted = resp.json()[0]["hashValue"]

    if len(sys.argv) > 1 and sys.argv[1] == "sync":
        print("Syncing Schwab trade history...")
        sync_schwab_tax_history(encrypted)

    report = get_tax_report()
    print(f"\nTax Report {report['year']}")
    print(f"Short term gains:  ${report['short_term_gains']:,.2f}")
    print(f"Short term losses: ${report['short_term_losses']:,.2f}")
    print(f"Net short term:    ${report['net_short_term']:,.2f}")
    print(f"Long term gains:   ${report['long_term_gains']:,.2f}")
    print(f"Options income:    ${report['options_income']:,.2f}")
    print(f"Dividends:         ${report['dividends']:,.2f}")
    print(f"\nTax breakdown:")
    print(f"Short term tax:    ${report['tax_short_term']:,.2f} @ {report['md_rate_short']}")
    print(f"Long term tax:     ${report['tax_long_term']:,.2f} @ {report['md_rate_long']}")
    print(f"Options tax:       ${report['tax_options']:,.2f}")
    print(f"Dividend tax:      ${report['tax_dividends']:,.2f}")
    print(f"\nTOTAL TAX OWED:    ${report['total_tax_owed']:,.2f}")
    print(f"Events tracked:    {report['ytd_events']}")

    plan = get_etf_tax_exit_plan(encrypted, report["total_tax_owed"])
    if plan:
        print(f"\nETF exit plan:")
        for p in plan:
            print(f"  Sell {p['shares_to_sell']} {p['symbol']} @ ${p['proceeds']:,.2f} ({p['hold_days']} days held)")
