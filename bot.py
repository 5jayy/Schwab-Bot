import os
import sys
import json
import time
import signal
import schedule
import requests
from dotenv import load_dotenv
from auth import get_valid_token
from scanner import scan_best_stocks, scan_best_etfs
from strategy import find_best_covered_call, get_trade_stocks
from telegram import send_alert

load_dotenv()

def handle_shutdown(signum, frame):
    print("Shutdown signal received — bot stopping cleanly.")
    sys.exit(0)

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

BASE_URL       = "https://api.schwabapi.com/trader/v1"
ETF_THRESHOLD  = float(os.getenv("ETF_THRESHOLD", 1000))
ETF_BUY_AMOUNT = float(os.getenv("ETF_BUY_AMOUNT", 250))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", 30))
LEDGER_FILE    = "trade_ledger.json"


# ── Trade ledger ─────────────────────────────────────────────────────────────

def load_ledger() -> dict:
    if not os.path.exists(LEDGER_FILE):
        return {"deposits": 0.0, "profit_bucket": 0.0, "open_trades": {}, "closed_trades": [], "last_known_cash": 0.0}
    with open(LEDGER_FILE) as f:
        return json.load(f)


def save_ledger(ledger: dict):
    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger, f, indent=2)


def record_buy(symbol: str, quantity: int, price: float, cost: float):
    ledger = load_ledger()
    ledger["open_trades"][symbol] = {
        "quantity": quantity,
        "buy_price": price,
        "cost": cost,
        "bought_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    save_ledger(ledger)


def record_sell(symbol: str, quantity: int, sell_price: float, proceeds: float) -> float:
    ledger = load_ledger()
    profit = 0.0
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
        if profit > 0:
            ledger["profit_bucket"] = ledger.get("profit_bucket", 0.0) + profit
    save_ledger(ledger)
    return profit


def get_profit_bucket() -> float:
    return load_ledger().get("profit_bucket", 0.0)


def deduct_profit_bucket(amount: float):
    ledger = load_ledger()
    ledger["profit_bucket"] = max(ledger.get("profit_bucket", 0.0) - amount, 0.0)
    save_ledger(ledger)


def detect_deposit(cash: float):
    ledger = load_ledger()
    last   = ledger.get("last_known_cash", cash)
    if cash > last + 50:
        deposit = cash - last
        ledger["deposits"] = ledger.get("deposits", 0.0) + deposit
        ledger["last_known_cash"] = cash
        save_ledger(ledger)
        send_alert(
            f"*New Deposit Detected*\n"
            f"Amount: ${deposit:,.2f}\n"
            f"Added to trading capital\n"
            f"Total deposits: ${ledger['deposits']:,.2f}\n"
            f"Bot will scan for best stocks to deploy this capital"
        )
        return True
    ledger["last_known_cash"] = cash
    save_ledger(ledger)
    return False


# ── Schwab API helpers ───────────────────────────────────────────────────────

def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_account_numbers() -> list:
    resp = requests.get(f"{BASE_URL}/accounts/accountNumbers", headers=headers())
    resp.raise_for_status()
    return resp.json()


def get_account(encrypted: str) -> dict:
    resp = requests.get(
        f"{BASE_URL}/accounts/{encrypted}",
        headers=headers(),
        params={"fields": "positions"}
    )
    resp.raise_for_status()
    return resp.json()


def get_cash_balance(account: dict) -> float:
    try:
        return account["securitiesAccount"]["currentBalances"]["cashBalance"]
    except KeyError:
        return 0.0


def get_portfolio_value(account: dict) -> float:
    try:
        return account["securitiesAccount"]["currentBalances"]["liquidationValue"]
    except KeyError:
        return 0.0


def get_positions(account: dict) -> list:
    try:
        return account["securitiesAccount"]["positions"]
    except KeyError:
        return []


def get_position_for(positions: list, symbol: str) -> dict | None:
    for p in positions:
        if p["instrument"]["symbol"] == symbol:
            return p
    return None


def place_equity_order(encrypted: str, symbol: str, quantity: int, instruction: str):
    order = {
        "orderType": "MARKET",
        "session":   "NORMAL",
        "duration":  "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [{
            "instruction": instruction,
            "quantity":    quantity,
            "instrument":  {"symbol": symbol, "assetType": "EQUITY"}
        }]
    }
    resp = requests.post(
        f"{BASE_URL}/accounts/{encrypted}/orders",
        headers={**headers(), "Content-Type": "application/json"},
        json=order
    )
    resp.raise_for_status()
    return resp


# ── Stock strategy using scanner ─────────────────────────────────────────────

def run_stock_strategy(encrypted: str, positions: list, cash: float, account_value: float) -> float:
    if cash < 10:
        print("Not enough cash to trade.")
        return cash

    position_size   = cash * 0.30
    bought_this_run = set()
    tier = "Tier 1 (<$5k)" if account_value < 5000 else "Tier 2 (<$20k)" if account_value < 20000 else "Tier 3 ($20k+)"

    # Get best stocks from scanner
    top_stocks = scan_best_stocks(cash, top_n=5)

    if not top_stocks:
        print("No buy signals found in scan.")
        # Still check existing positions for sell signals
        trade_stocks = get_trade_stocks(account_value)
        for symbol in trade_stocks:
            position = get_position_for(positions, symbol)
            if not position:
                continue
            from strategy import get_signal
            sig = get_signal(symbol)
            if sig["signal"] == "SELL":
                quantity = int(position.get("longQuantity", 0))
                price    = sig.get("price", 0)
                if quantity < 1:
                    continue
                try:
                    place_equity_order(encrypted, symbol, quantity, "SELL")
                    proceeds = quantity * price
                    profit   = record_sell(symbol, quantity, price, proceeds)
                    cash    += proceeds
                    bucket   = get_profit_bucket()
                    label    = "PROFIT" if profit > 0 else "LOSS"
                    send_alert(
                        f"*Stock Sell — {label}*\n"
                        f"Symbol: {symbol}\n"
                        f"Shares: {quantity}\n"
                        f"Price: ${price:.2f}\n"
                        f"Proceeds: ${proceeds:,.2f}\n"
                        f"{'Profit' if profit > 0 else 'Loss'}: ${profit:+,.2f}\n"
                        f"Profit bucket: ${bucket:,.2f}"
                    )
                except Exception as e:
                    send_alert(f"*Sell error* {symbol}: {e}")
        return cash

    print(f"\n-- Buying top scanner picks [{tier}] | Position size: ${position_size:,.2f} --")

    for stock in top_stocks:
        symbol = stock["symbol"]
        price  = stock["price"]

        if symbol in bought_this_run:
            continue

        position = get_position_for(positions, symbol)
        if position:
            # Already own it — check for sell
            from strategy import get_signal
            sig = get_signal(symbol)
            if sig["signal"] == "SELL":
                quantity = int(position.get("longQuantity", 0))
                if quantity < 1:
                    continue
                try:
                    place_equity_order(encrypted, symbol, quantity, "SELL")
                    proceeds = quantity * price
                    profit   = record_sell(symbol, quantity, price, proceeds)
                    cash    += proceeds
                    bucket   = get_profit_bucket()
                    label    = "PROFIT" if profit > 0 else "LOSS"
                    send_alert(
                        f"*Stock Sell — {label}*\n"
                        f"Symbol: {symbol}\n"
                        f"Shares: {quantity}\n"
                        f"Price: ${price:.2f}\n"
                        f"Proceeds: ${proceeds:,.2f}\n"
                        f"{'Profit' if profit > 0 else 'Loss'}: ${profit:+,.2f}\n"
                        f"Profit bucket: ${bucket:,.2f}"
                    )
                except Exception as e:
                    send_alert(f"*Sell error* {symbol}: {e}")
            continue

        # Buy new position
        if cash < price:
            continue
        quantity = int(position_size // price)
        if quantity < 1:
            continue

        try:
            place_equity_order(encrypted, symbol, quantity, "BUY")
            cost  = quantity * price
            cash -= cost
            bought_this_run.add(symbol)
            record_buy(symbol, quantity, price, cost)
            send_alert(
                f"*Stock Buy (scanner pick)*\n"
                f"Symbol: {symbol}\n"
                f"Shares: {quantity}\n"
                f"Price: ${price:.2f}\n"
                f"Cost: ${cost:,.2f}\n"
                f"Score: {stock['score']:.1f} | RSI: {stock['rsi']:.1f}\n"
                f"Tier: {tier}"
            )
            print(f"  Bought {quantity} {symbol} @ ${price:.2f} (score: {stock['score']:.1f})")
        except Exception as e:
            send_alert(f"*Buy error* {symbol}: {e}")

    return cash


# ── Options strategy ─────────────────────────────────────────────────────────

def run_options_strategy(encrypted: str, positions: list, account_value: float):
    trade_stocks = get_trade_stocks(account_value)
    print("\n-- Covered calls --")
    for symbol in trade_stocks:
        position = get_position_for(positions, symbol)
        if not position:
            continue
        shares = int(position.get("longQuantity", 0))
        if shares < 100:
            continue
        best_call = find_best_covered_call(symbol, shares)
        if not best_call:
            continue
        send_alert(
            f"*Covered Call Opportunity*\n"
            f"Stock: {symbol} ({shares} shares)\n"
            f"Strike: ${best_call['strike']}\n"
            f"Expiry: {best_call['expiry']} ({best_call['dte']} DTE)\n"
            f"Premium: ${best_call['premium']:.2f}/share\n"
            f"Total income: ${best_call['total_premium']:,.2f}"
        )


# ── ETF sweep using scanner ───────────────────────────────────────────────────

def run_etf_sweep(encrypted: str):
    bucket = get_profit_bucket()
    print(f"\n-- ETF sweep | Profit bucket: ${bucket:,.2f} | Threshold: ${ETF_THRESHOLD:,.2f} --")

    if bucket < ETF_THRESHOLD:
        print(f"Profit bucket below threshold — holding.")
        return

    # Scan for best ETFs right now
    best_etfs = scan_best_etfs(bucket, top_n=2)

    if not best_etfs:
        print("No ETF buy signals found.")
        return

    per_etf = ETF_BUY_AMOUNT / len(best_etfs)

    for etf in best_etfs:
        symbol   = etf["symbol"]
        price    = etf["price"]
        quantity = int(per_etf // price)
        if quantity < 1:
            send_alert(f"Not enough profit for 1 share of {symbol} (${price:.2f})")
            continue
        try:
            place_equity_order(encrypted, symbol, quantity, "BUY")
            cost = quantity * price
            deduct_profit_bucket(cost)
            send_alert(
                f"*ETF Buy (profits only)*\n"
                f"Symbol: {symbol}\n"
                f"Shares: {quantity}\n"
                f"Price: ${price:.2f}\n"
                f"Total: ${cost:,.2f}\n"
                f"Score: {etf['score']:.1f}\n"
                f"Profit bucket remaining: ${get_profit_bucket():,.2f}"
            )
            print(f"Swept ${cost:,.2f} profit into {symbol}")
        except Exception as e:
            send_alert(f"*ETF sweep error* {symbol}: {e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def run_strategy():
    print("\n=== Strategy check ===")
    try:
        accounts      = get_account_numbers()
        encrypted     = accounts[0]["hashValue"]
        account       = get_account(encrypted)
        cash          = get_cash_balance(account)
        account_value = get_portfolio_value(account)
        positions     = get_positions(account)
        bucket        = get_profit_bucket()

        tier = "Tier 1 (<$5k)" if account_value < 5000 else "Tier 2 (<$20k)" if account_value < 20000 else "Tier 3 ($20k+)"
        print(f"Account: ${account_value:,.2f} | Cash: ${cash:,.2f} | Profit bucket: ${bucket:,.2f} | {tier}")

        detect_deposit(cash)
        cash = run_stock_strategy(encrypted, positions, cash, account_value)
        run_options_strategy(encrypted, positions, account_value)
        run_etf_sweep(encrypted)

    except Exception as e:
        msg = f"*Bot error*: {e}"
        print(msg)
        send_alert(msg)


def main():
    try:
        accounts      = get_account_numbers()
        encrypted     = accounts[0]["hashValue"]
        account       = get_account(encrypted)
        cash          = get_cash_balance(account)
        account_value = get_portfolio_value(account)
        bucket        = get_profit_bucket()
        tier = "Tier 1 (<$5k)" if account_value < 5000 else "Tier 2 (<$20k)" if account_value < 20000 else "Tier 3 ($20k+)"

        send_alert(
            f"*Schwab Bot Started*\n"
            f"Account: ${account_value:,.2f}\n"
            f"Cash (trading capital): ${cash:,.2f}\n"
            f"Profit bucket: ${bucket:,.2f}\n"
            f"ETF sweep at: ${ETF_THRESHOLD:,.2f} profit\n"
            f"Tier: {tier}\n"
            f"Scanner active — finding best stocks every 30 min"
        )
    except Exception as e:
        print(f"Startup error: {e}")

    run_strategy()
    schedule.every(CHECK_INTERVAL).minutes.do(run_strategy)
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
