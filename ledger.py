import os
import json
import time
import requests
from auth import get_valid_token

LEDGER_FILE = "trade_ledger.json"

BOT_STOCKS = [
    "SOFI", "F", "BAC", "VALE", "PLUG", "AAL", "RIOT", "MARA", "NIO", "PLTR",
    "SNAP", "INTC", "UBER", "SQ", "COIN", "RBLX", "DKNG", "PENN", "LYFT", "ABNB",
    "DASH", "RIVN", "LCID", "AFRM", "UPST", "AAPL", "GOOGL", "AMD", "PYPL", "DIS",
    "AMZN", "NVDA", "MSFT", "META", "TSLA", "NFLX", "CRM", "SHOP", "BABA", "HIMS",
    "JOBY", "OPEN", "HOOD", "CLSK", "TLRY", "SIRI", "NOK", "SPCE"
]


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def load_ledger() -> dict:
    if not os.path.exists(LEDGER_FILE):
        return None
    with open(LEDGER_FILE) as f:
        return json.load(f)


def save_ledger(ledger: dict):
    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger, f, indent=2)


def sync_ledger_from_schwab(encrypted: str) -> dict:
    """
    Auto-syncs ledger with actual Schwab positions on every startup.
    Picks up any positions already in account and registers them.
    Never resets profit bucket or closed trades.
    """
    resp = requests.get(
        f"https://api.schwabapi.com/trader/v1/accounts/{encrypted}?fields=positions",
        headers=headers()
    )
    resp.raise_for_status()
    account   = resp.json()
    cash      = account["securitiesAccount"]["currentBalances"]["cashBalance"]
    positions = account["securitiesAccount"].get("positions", [])

    existing = load_ledger() or {}

    # Keep existing profit bucket and closed trades
    profit_bucket    = existing.get("profit_bucket", 0.0)
    closed_trades    = existing.get("closed_trades", [])
    total_withdrawn  = existing.get("total_withdrawn", 0.0)
    total_etf_bought = existing.get("total_etf_bought", 0.0)

    # Rebuild open trades from actual Schwab positions
    open_trades    = existing.get("open_trades", {})
    bot_value      = 0.0
    new_positions  = []

    for p in positions:
        sym = p["instrument"]["symbol"]
        if sym not in BOT_STOCKS:
            continue
        qty = p["longQuantity"]
        avg = p["averagePrice"]
        cost = qty * avg
        bot_value += cost

        # Only add if not already tracked
        if sym not in open_trades:
            open_trades[sym] = {
                "quantity":  qty,
                "buy_price": avg,
                "cost":      cost,
                "bought_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            }
            new_positions.append(sym)

    # Trading capital = cash + all bot stock positions
    trading_capital = cash + bot_value

    # Deposits = max of existing deposits or current trading capital
    deposits = max(existing.get("deposits", 0.0), trading_capital)

    ledger = {
        "deposits":         deposits,
        "trading_capital":  trading_capital,
        "profit_bucket":    profit_bucket,
        "total_withdrawn":  total_withdrawn,
        "total_etf_bought": total_etf_bought,
        "open_trades":      open_trades,
        "closed_trades":    closed_trades,
        "last_known_cash":  cash,
        "last_synced":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }

    save_ledger(ledger)

    print(f"Ledger synced — Cash: ${cash:.2f} | Bot positions: ${bot_value:.2f} | Trading capital: ${trading_capital:.2f}")
    if new_positions:
        print(f"New positions registered: {new_positions}")

    return ledger


def get_profit_bucket() -> float:
    return load_ledger().get("profit_bucket", 0.0) if load_ledger() else 0.0


def get_trading_capital() -> float:
    return load_ledger().get("trading_capital", 0.0) if load_ledger() else 0.0


def record_buy(symbol: str, quantity: int, price: float, cost: float):
    ledger = load_ledger() or {}
    ledger.setdefault("open_trades", {})[symbol] = {
        "quantity":  quantity,
        "buy_price": price,
        "cost":      cost,
        "bought_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    save_ledger(ledger)


def record_sell(symbol: str, quantity: int, sell_price: float, proceeds: float) -> float:
    ledger = load_ledger() or {}
    profit = 0.0
    open_trades = ledger.get("open_trades", {})
    if symbol in open_trades:
        trade  = open_trades[symbol]
        cost   = trade["cost"]
        profit = proceeds - cost
        ledger.setdefault("closed_trades", []).append({
            "symbol":     symbol,
            "quantity":   quantity,
            "buy_price":  trade["buy_price"],
            "sell_price": sell_price,
            "cost":       cost,
            "proceeds":   proceeds,
            "profit":     profit,
            "closed_at":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        })
        del open_trades[symbol]
        if profit > 0:
            ledger["profit_bucket"] = ledger.get("profit_bucket", 0.0) + profit
        # Update trading capital
        ledger["trading_capital"] = ledger.get("trading_capital", 0.0) - trade["cost"] + proceeds
    ledger["open_trades"] = open_trades
    save_ledger(ledger)
    return profit


def deduct_profit_bucket(amount: float):
    ledger = load_ledger() or {}
    ledger["profit_bucket"] = max(ledger.get("profit_bucket", 0.0) - amount, 0.0)
    save_ledger(ledger)


def detect_deposit(cash: float) -> bool:
    ledger = load_ledger() or {}
    last   = ledger.get("last_known_cash", cash)
    if cash > last + 50:
        deposit = cash - last
        ledger["deposits"]        = ledger.get("deposits", 0.0) + deposit
        ledger["trading_capital"] = ledger.get("trading_capital", 0.0) + deposit
        ledger["last_known_cash"] = cash
        save_ledger(ledger)
        return True
    ledger["last_known_cash"] = cash
    save_ledger(ledger)
    return False
