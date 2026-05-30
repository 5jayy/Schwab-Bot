import os
import sys
import json
import time
import signal
import schedule
import requests
from dotenv import load_dotenv
from auth import get_valid_token
from strategy import get_signal, find_best_covered_call, get_trade_stocks, get_position_size
from telegram import send_alert

load_dotenv()

def handle_shutdown(signum, frame):
    print("Shutdown signal received — bot stopping cleanly.")
    sys.exit(0)

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

BASE_URL       = "https://api.schwabapi.com/trader/v1"
TARGET_ETFS    = [e.strip() for e in os.getenv("TARGET_ETFS", "SCHD,JEPI").split(",")]
CASH_THRESHOLD = float(os.getenv("CASH_THRESHOLD", 2000))
ETF_BUY_AMOUNT = float(os.getenv("ETF_BUY_AMOUNT", 250))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", 30))
BASELINE_FILE  = "baseline.json"


# ── Baseline ─────────────────────────────────────────────────────────────────

def load_baseline() -> float | None:
    if not os.path.exists(BASELINE_FILE):
        return None
    with open(BASELINE_FILE) as f:
        return json.load(f).get("starting_cash", None)


def save_baseline(cash: float):
    with open(BASELINE_FILE, "w") as f:
        json.dump({"starting_cash": cash, "saved_at": time.time()}, f, indent=2)
    print(f"Baseline saved: ${cash:,.2f}")


def get_profit(current_cash: float) -> float:
    baseline = load_baseline()
    if baseline is None:
        return 0.0
    return max(current_cash - baseline, 0.0)


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


# ── Stock strategy ───────────────────────────────────────────────────────────

def run_stock_strategy(encrypted: str, positions: list, cash: float, account_value: float) -> float:
    trade_stocks  = get_trade_stocks(account_value)
    position_size = get_position_size(account_value)

    tier = "Tier 1 (<$5k)" if account_value < 5000 else "Tier 2 (<$20k)" if account_value < 20000 else "Tier 3 ($20k+)"
    print(f"\n-- Stock signals [{tier}] | Position size: ${position_size:,.2f} --")

    for symbol in trade_stocks:
        sig      = get_signal(symbol)
        position = get_position_for(positions, symbol)
        print(f"  {symbol}: {sig['signal']} — {sig['reason']}")

        if sig["signal"] == "BUY" and not position:
            price = sig.get("price", 0)
            if price <= 0 or cash < price:
                continue
            quantity = int(position_size // price)
            if quantity < 1:
                continue
            try:
                place_equity_order(encrypted, symbol, quantity, "BUY")
                cost  = quantity * price
                cash -= cost
                send_alert(
                    f"*Stock Buy*\n"
                    f"Symbol: {symbol}\n"
                    f"Shares: {quantity}\n"
                    f"Price: ${price:.2f}\n"
                    f"Cost: ${cost:,.2f}\n"
                    f"Account tier: {tier}\n"
                    f"Signal: {sig['reason']}"
                )
                print(f"  Bought {quantity} {symbol} @ ${price:.2f}")
            except Exception as e:
                send_alert(f"*Buy error* {symbol}: {e}")

        elif sig["signal"] == "SELL" and position:
            quantity = int(position.get("longQuantity", 0))
            avg_cost = position.get("averagePrice", 0)
            price    = sig.get("price", 0)
            if quantity < 1:
                continue
            try:
                place_equity_order(encrypted, symbol, quantity, "SELL")
                proceeds = quantity * price
                gain     = proceeds - (avg_cost * quantity)
                cash    += proceeds
                send_alert(
                    f"*Stock Sell*\n"
                    f"Symbol: {symbol}\n"
                    f"Shares: {quantity}\n"
                    f"Price: ${price:.2f}\n"
                    f"Proceeds: ${proceeds:,.2f}\n"
                    f"P&L: ${gain:+,.2f}\n"
                    f"Signal: {sig['reason']}"
                )
                print(f"  Sold {quantity} {symbol} @ ${price:.2f} | P&L ${gain:+,.2f}")
            except Exception as e:
                send_alert(f"*Sell error* {symbol}: {e}")

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
            print(f"  {symbol}: No good covered call found")
            continue
        send_alert(
            f"*Covered Call Opportunity*\n"
            f"Stock: {symbol} ({shares} shares)\n"
            f"Strike: ${best_call['strike']}\n"
            f"Expiry: {best_call['expiry']} ({best_call['dte']} DTE)\n"
            f"Premium: ${best_call['premium']:.2f}/share\n"
            f"Total income: ${best_call['total_premium']:,.2f}"
        )


# ── ETF sweep ────────────────────────────────────────────────────────────────

def run_etf_sweep(encrypted: str, cash: float):
    profit = get_profit(cash)
    print(f"\n-- ETF sweep — cash: ${cash:,.2f} | profit: ${profit:,.2f} --")

    if cash < CASH_THRESHOLD:
        print(f"Cash below threshold ${CASH_THRESHOLD:,.2f} — no sweep.")
        return

    if profit < ETF_BUY_AMOUNT:
        print(f"Profit ${profit:,.2f} not enough — protecting capital.")
        return

    per_etf = ETF_BUY_AMOUNT / len(TARGET_ETFS)
    for etf in TARGET_ETFS:
        try:
            resp = requests.get(
                "https://api.schwabapi.com/marketdata/v1/quotes",
                headers=headers(),
                params={"symbols": etf}
            )
            resp.raise_for_status()
            price    = resp.json()[etf]["quote"]["lastPrice"]
            quantity = int(per_etf // price)
            if quantity < 1:
                send_alert(f"Not enough profit for 1 share of {etf} (${price:.2f})")
                continue
            place_equity_order(encrypted, etf, quantity, "BUY")
            send_alert(
                f"*ETF Buy (from profits)*\n"
                f"Symbol: {etf}\n"
                f"Shares: {quantity}\n"
                f"Price: ${price:.2f}\n"
                f"Total: ${quantity * price:,.2f}\n"
                f"Profit used: ${profit:,.2f}"
            )
        except Exception as e:
            send_alert(f"*ETF sweep error* {etf}: {e}")


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

        tier = "Tier 1 (<$5k)" if account_value < 5000 else "Tier 2 (<$20k)" if account_value < 20000 else "Tier 3 ($20k+)"
        print(f"Account value: ${account_value:,.2f} | Cash: ${cash:,.2f} | {tier}")

        cash = run_stock_strategy(encrypted, positions, cash, account_value)
        run_options_strategy(encrypted, positions, account_value)
        run_etf_sweep(encrypted, cash)

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

        if load_baseline() is None:
            save_baseline(cash)

        tier = "Tier 1 (<$5k)" if account_value < 5000 else "Tier 2 (<$20k)" if account_value < 20000 else "Tier 3 ($20k+)"
        baseline = load_baseline()

        send_alert(
            f"*Schwab Bot Started*\n"
            f"Account value: ${account_value:,.2f}\n"
            f"Cash: ${cash:,.2f}\n"
            f"Baseline: ${baseline:,.2f}\n"
            f"Current tier: {tier}\n"
            f"Stocks: {', '.join([s for s in __import__('strategy').get_trade_stocks(account_value)])}\n"
            f"ETFs: {', '.join(TARGET_ETFS)}\n"
            f"Checking every {CHECK_INTERVAL} min"
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
