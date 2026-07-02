import os
import json
import time
import requests
from auth import get_valid_token

import os as _os
LEDGER_FILE = "/data/trade_ledger.json" if _os.path.exists("/data") else "trade_ledger.json"

BOT_STOCKS = [
    "SOFI", "F", "BAC", "VALE", "PLUG", "AAL", "RIOT", "MARA", "NIO", "PLTR",
    "SNAP", "INTC", "UBER", "SQ", "COIN", "RBLX", "DKNG", "PENN", "LYFT", "ABNB",
    "DASH", "RIVN", "LCID", "AFRM", "UPST", "AAPL", "GOOGL", "AMD", "PYPL", "DIS",
    "AMZN", "NVDA", "MSFT", "META", "TSLA", "NFLX", "CRM", "SHOP", "BABA", "HIMS",
    "JOBY", "OPEN", "HOOD", "CLSK", "TLRY", "SIRI", "NOK", "SPCE"
]

ETF_MIN_SWEEP = 50.0  # minimum profit before buying an ETF share


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def load_ledger() -> dict:
    default = {
        "deposits":         0.0,
        "trading_capital":  0.0,
        "profit_bucket":    0.0,
        "etf_bucket":       0.0,
        "cash_bucket":      0.0,
        "bot_bucket":       0.0,
        "total_withdrawn":  0.0,
        "total_etf_bought": 0.0,
        "open_trades":      {},
        "closed_trades":    [],
        "last_known_cash":  0.0,
        "last_synced":      ""
    }
    if not os.path.exists(LEDGER_FILE):
        return default
    with open(LEDGER_FILE) as f:
        data = json.load(f)
    # Fill in any missing keys from default (safe for updates)
    for key, val in default.items():
        data.setdefault(key, val)
    return data


def save_ledger(ledger: dict):
    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger, f, indent=2)


def sync_ledger_from_schwab(encrypted: str) -> dict:
    """
    Runs on every startup and every strategy check.
    Picks up any existing positions automatically.
    Never wipes profit bucket, closed trades, or history.
    Safe to run after every update.
    """
    try:
        resp = requests.get(
            f"https://api.schwabapi.com/trader/v1/accounts/{encrypted}?fields=positions",
            headers=headers(),
            timeout=15
        )
        resp.raise_for_status()
        account   = resp.json()
        cash      = account["securitiesAccount"]["currentBalances"]["cashBalance"]
        positions = account["securitiesAccount"].get("positions", [])
    except Exception as e:
        print(f"Ledger sync error: {e}")
        return load_ledger()

    ledger      = load_ledger()
    open_trades = ledger.get("open_trades", {})
    bot_value   = 0.0
    new_found   = []

    for p in positions:
        sym = p["instrument"]["symbol"]
        if sym not in BOT_STOCKS:
            continue
        qty  = p["longQuantity"]
        avg  = p["averagePrice"]
        cost = qty * avg
        bot_value += cost

        # Register position if not already tracked
        if sym not in open_trades:
            open_trades[sym] = {
                "quantity":   qty,
                "buy_price":  avg,
                "cost":       cost,
                "high_price": avg,  # initialize peak price at registration
                "bought_at":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            }
            new_found.append(sym)
        else:
            # Update quantity in case partial fills or adjustments
            open_trades[sym]["quantity"]  = qty
            open_trades[sym]["buy_price"] = avg
            open_trades[sym]["cost"]      = cost

    # Remove positions from ledger that no longer exist in Schwab
    symbols_in_schwab = {p["instrument"]["symbol"] for p in positions if p["instrument"]["symbol"] in BOT_STOCKS}
    stale = [s for s in open_trades if s not in symbols_in_schwab]
    for s in stale:
        print(f"Removing stale position from ledger: {s}")
        del open_trades[s]

    trading_capital = cash + bot_value
    deposits        = max(ledger.get("deposits", 0.0), trading_capital)

    ledger["open_trades"]     = open_trades
    ledger["trading_capital"] = trading_capital
    ledger["deposits"]        = deposits
    ledger["last_known_cash"] = cash
    ledger["last_synced"]     = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_ledger(ledger)

    print(f"Ledger synced — Cash: ${cash:.2f} | Positions: ${bot_value:.2f} | Capital: ${trading_capital:.2f}")
    if new_found:
        print(f"New positions registered: {new_found}")
    if stale:
        print(f"Removed stale: {stale}")

    return ledger


def get_profit_bucket() -> float:
    return load_ledger().get("profit_bucket", 0.0)


def get_etf_bucket() -> float:
    return load_ledger().get("etf_bucket", 0.0)


def get_cash_bucket() -> float:
    return load_ledger().get("cash_bucket", 0.0)


def get_bot_bucket() -> float:
    return load_ledger().get("bot_bucket", 0.0)


def get_trading_capital() -> float:
    return load_ledger().get("trading_capital", 0.0)


def record_dividend(symbol: str, amount: float, reinvested: bool):
    """Records a dividend payment, tracking total dividends and whether reinvested."""
    ledger = load_ledger()
    ledger.setdefault("dividend_history", []).append({
        "symbol":     symbol,
        "amount":     amount,
        "reinvested": reinvested,
        "timestamp":  __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
    })
    ledger["total_dividends"] = ledger.get("total_dividends", 0.0) + amount
    if reinvested:
        ledger["dividends_reinvested"] = ledger.get("dividends_reinvested", 0.0) + amount
    else:
        ledger["dividends_cash"] = ledger.get("dividends_cash", 0.0) + amount
    save_ledger(ledger)


def get_dividend_stats() -> dict:
    ledger = load_ledger()
    return {
        "total_dividends":      ledger.get("total_dividends", 0.0),
        "dividends_reinvested": ledger.get("dividends_reinvested", 0.0),
        "dividends_cash":       ledger.get("dividends_cash", 0.0),
        "seen_dividend_ids":    ledger.get("seen_dividend_ids", [])
    }


def mark_dividend_seen(transaction_id: str):
    ledger = load_ledger()
    seen = ledger.setdefault("seen_dividend_ids", [])
    if transaction_id not in seen:
        seen.append(transaction_id)
        # Keep only last 200 to avoid unbounded growth
        ledger["seen_dividend_ids"] = seen[-200:]
        save_ledger(ledger)


def record_buy(symbol: str, quantity: int, price: float, cost: float):
    ledger = load_ledger()
    ledger["open_trades"][symbol] = {
        "quantity":    quantity,
        "buy_price":   price,
        "cost":        cost,
        "high_price":  price,  # track peak price for trailing stop
        "bought_at":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    save_ledger(ledger)


def update_high_price(symbol: str, current_price: float):
    """Update the tracked peak price for trailing stop calculations."""
    ledger = load_ledger()
    if symbol in ledger.get("open_trades", {}):
        trade = ledger["open_trades"][symbol]
        if current_price > trade.get("high_price", trade["buy_price"]):
            trade["high_price"] = current_price
            save_ledger(ledger)


def get_trailing_stop_info(symbol: str) -> dict | None:
    """Returns high_price and buy_price for a symbol, or None if not tracked."""
    ledger = load_ledger()
    trade = ledger.get("open_trades", {}).get(symbol)
    if not trade:
        return None
    return {
        "buy_price":  trade["buy_price"],
        "high_price": trade.get("high_price", trade["buy_price"]),
        "quantity":   trade["quantity"]
    }


def record_sell_and_split(symbol: str, quantity: int, sell_price: float, proceeds: float,
                           etf_pct: float, cash_pct: float, bot_pct: float) -> dict:
    """
    Records a sell, calculates profit, and immediately splits it 60/30/10.
    Returns split breakdown.
    """
    ledger = load_ledger()
    profit = 0.0
    cost   = 0.0

    if symbol in ledger["open_trades"]:
        trade  = ledger["open_trades"][symbol]
        cost   = trade["cost"]
        profit = proceeds - cost

        ledger["closed_trades"].append({
            "symbol":     symbol,
            "quantity":   quantity,
            "buy_price":  trade["buy_price"],
            "sell_price": sell_price,
            "cost":       cost,
            "proceeds":   proceeds,
            "profit":     profit,
            "closed_at":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        })
        del ledger["open_trades"][symbol]

    # Split profit immediately on every sell
    if profit > 0:
        etf_cut  = profit * etf_pct
        cash_cut = profit * cash_pct
        bot_cut  = profit * bot_pct

        ledger["profit_bucket"]    = ledger.get("profit_bucket", 0.0) + profit
        ledger["etf_bucket"]       = ledger.get("etf_bucket", 0.0) + etf_cut
        ledger["cash_bucket"]      = ledger.get("cash_bucket", 0.0) + cash_cut
        ledger["bot_bucket"]       = ledger.get("bot_bucket", 0.0) + bot_cut
        ledger["trading_capital"]  = ledger.get("trading_capital", 0.0) + bot_cut
        ledger["total_withdrawn"]  = ledger.get("total_withdrawn", 0.0) + cash_cut
    else:
        etf_cut  = 0.0
        cash_cut = 0.0
        bot_cut  = 0.0

    save_ledger(ledger)

    return {
        "profit":   profit,
        "cost":     cost,
        "etf_cut":  etf_cut,
        "cash_cut": cash_cut,
        "bot_cut":  bot_cut,
    }


def deduct_etf_bucket(amount: float):
    ledger = load_ledger()
    ledger["etf_bucket"]       = max(ledger.get("etf_bucket", 0.0) - amount, 0.0)
    ledger["total_etf_bought"] = ledger.get("total_etf_bought", 0.0) + amount
    save_ledger(ledger)


def detect_deposit(cash: float) -> float:
    """Returns deposit amount if detected, 0 otherwise."""
    ledger = load_ledger()
    last   = ledger.get("last_known_cash", cash)
    if cash > last + 1:
        deposit = cash - last
        ledger["deposits"]        = ledger.get("deposits", 0.0) + deposit
        ledger["trading_capital"] = ledger.get("trading_capital", 0.0) + deposit
        ledger["last_known_cash"] = cash
        save_ledger(ledger)
        return deposit
    ledger["last_known_cash"] = cash
    save_ledger(ledger)
    return 0.0


def detect_withdrawal(cash: float) -> float:
    """
    Detects when cash drops significantly — user withdrew money.
    Returns withdrawal amount if detected, 0 otherwise.
    Persists withdrawal history across deploys via /data volume.
    """
    ledger = load_ledger()
    last   = ledger.get("last_known_cash", cash)

    # Only count as withdrawal if cash dropped more than 0
    # and it wasnt just the bot spending on trades
    if last - cash > 20:
        withdrawal = last - cash

        # Track withdrawal history
        ledger.setdefault("withdrawal_history", []).append({
            "amount":      withdrawal,
            "cash_before": last,
            "cash_after":  cash,
            "timestamp":   __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
        })
        ledger["total_withdrawn"] = ledger.get("total_withdrawn", 0.0) + withdrawal
        ledger["last_known_cash"] = cash
        save_ledger(ledger)
        return withdrawal

    ledger["last_known_cash"] = cash
    save_ledger(ledger)
    return 0.0


def get_withdrawal_stats() -> dict:
    """Returns withdrawal history and totals."""
    ledger = load_ledger()
    return {
        "total_withdrawn":    ledger.get("total_withdrawn", 0.0),
        "cash_bucket":        ledger.get("cash_bucket", 0.0),
        "withdrawal_history": ledger.get("withdrawal_history", [])
    }
