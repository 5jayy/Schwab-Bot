import os
import sys
import signal
import schedule
import time
import requests
from dotenv import load_dotenv
from auth import get_valid_token
from scanner import scan_best_stocks, scan_best_etfs
from strategy import get_trade_stocks, get_signal
from options import find_best_covered_call, place_covered_call, check_covered_call_already_open
from telegram import send_alert
from token_manager import check_token_health
from ledger import (
    sync_ledger_from_schwab, load_ledger, save_ledger,
    record_buy, record_sell_and_split,
    get_profit_bucket, get_trading_capital,
    get_etf_bucket, get_cash_bucket, get_bot_bucket,
    deduct_etf_bucket, detect_deposit,
    BOT_STOCKS, ETF_MIN_SWEEP
)

load_dotenv()

def handle_shutdown(signum, frame):
    print("Shutdown signal received — bot stopping cleanly.")
    sys.exit(0)

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

BASE_URL       = "https://api.schwabapi.com/trader/v1"
ETF_PCT        = float(os.getenv("ETF_PCT", 0.60))
CASH_PCT       = float(os.getenv("CASH_PCT", 0.30))
BOT_PCT        = float(os.getenv("BOT_PCT", 0.10))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", 30))


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


# ── Sell with immediate profit split ────────────────────────────────────────

def execute_sell(encrypted: str, symbol: str, quantity: int, price: float, cash: float) -> float:
    try:
        place_equity_order(encrypted, symbol, quantity, "SELL")
        proceeds = quantity * price
        split    = record_sell_and_split(symbol, quantity, price, proceeds, ETF_PCT, CASH_PCT, BOT_PCT)
        cash    += proceeds
        profit   = split["profit"]
        label    = "PROFIT" if profit > 0 else "LOSS"

        if profit > 0:
            send_alert(
                f"*Stock Sell — {label} 💰*\n"
                f"Symbol: {symbol}\n"
                f"Shares: {quantity}\n"
                f"Price: ${price:.2f}\n"
                f"Proceeds: ${proceeds:,.2f}\n"
                f"Profit: +${profit:,.2f}\n\n"
                f"*Split immediately:*\n"
                f"→ ETF bucket: +${split['etf_cut']:,.2f} (60%)\n"
                f"→ Your cash: +${split['cash_cut']:,.2f} (30%)\n"
                f"→ Bot capital: +${split['bot_cut']:,.2f} (10%)\n\n"
                f"ETF bucket total: ${get_etf_bucket():,.2f}\n"
                f"All-time profit: ${get_profit_bucket():,.2f}"
            )
        else:
            send_alert(
                f"*Stock Sell — {label}*\n"
                f"Symbol: {symbol}\n"
                f"Shares: {quantity}\n"
                f"Price: ${price:.2f}\n"
                f"Proceeds: ${proceeds:,.2f}\n"
                f"Loss: ${profit:,.2f}"
            )
        print(f"  Sold {quantity} {symbol} @ ${price:.2f} | P&L ${profit:+,.2f}")
    except Exception as e:
        send_alert(f"*Sell error* {symbol}: {e}")
    return cash


# ── Stock strategy ───────────────────────────────────────────────────────────

def run_stock_strategy(encrypted: str, positions: list, cash: float, account_value: float) -> float:
    tier          = "Tier 1 (<$5k)" if account_value < 5000 else "Tier 2 (<$20k)" if account_value < 20000 else "Tier 3 ($20k+)"
    position_size = cash * 0.30
    bought_this_run = set()

    # Always check existing positions for sell signals regardless of cash
    for position in positions:
        sym = position["instrument"]["symbol"]
        if sym not in BOT_STOCKS:
            continue
        sig = get_signal(sym)
        if sig["signal"] == "SELL":
            quantity = int(position.get("longQuantity", 0))
            price    = sig.get("price", 0)
            if quantity < 1:
                continue
            cash = execute_sell(encrypted, sym, quantity, price, cash)

    # Only buy if we have enough cash
    if cash < 10:
        print("Not enough cash to buy — monitoring positions only.")
        return cash

    top_stocks = scan_best_stocks(cash, top_n=5)
    if not top_stocks:
        print("No buy signals found in scan.")
        return cash

    print(f"\n-- Buying scanner picks [{tier}] | Position size: ${position_size:,.2f} --")

    for stock in top_stocks:
        symbol = stock["symbol"]
        price  = stock["price"]

        if symbol in bought_this_run:
            continue

        # Skip if already own it
        position = get_position_for(positions, symbol)
        if position:
            continue

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
            print(f"  Bought {quantity} {symbol} @ ${price:.2f}")
        except Exception as e:
            send_alert(f"*Buy error* {symbol}: {e}")

    return cash


# ── Options strategy ─────────────────────────────────────────────────────────

def run_options_strategy(encrypted: str, positions: list, account_value: float):
    print("\n-- Covered calls --")
    for position in positions:
        symbol = position["instrument"]["symbol"]
        if position["instrument"].get("assetType") != "EQUITY":
            continue
        shares = int(position.get("longQuantity", 0))
        if shares < 100:
            continue

        # Skip if already have open covered call on this stock
        if check_covered_call_already_open(encrypted, symbol):
            print(f"  {symbol}: covered call already open")
            continue

        best_call = find_best_covered_call(symbol, shares)
        if not best_call:
            print(f"  {symbol}: no good covered call found")
            continue

        print(f"  {symbol}: placing covered call strike ${best_call['strike']} exp {best_call['expiry']} premium ${best_call['premium']:.2f}")

        try:
            place_covered_call(encrypted, best_call["option_symbol"], best_call["contracts"], best_call["premium"])
            total    = best_call["total_premium"]
            etf_cut  = total * 0.60
            cash_cut = total * 0.30
            bot_cut  = total * 0.10

            from ledger import load_ledger, save_ledger
            ledger = load_ledger()
            ledger["profit_bucket"]   = ledger.get("profit_bucket", 0.0) + total
            ledger["etf_bucket"]      = ledger.get("etf_bucket", 0.0) + etf_cut
            ledger["cash_bucket"]     = ledger.get("cash_bucket", 0.0) + cash_cut
            ledger["bot_bucket"]      = ledger.get("bot_bucket", 0.0) + bot_cut
            ledger["trading_capital"] = ledger.get("trading_capital", 0.0) + bot_cut
            ledger["total_withdrawn"] = ledger.get("total_withdrawn", 0.0) + cash_cut
            save_ledger(ledger)

            send_alert(
                f"*Covered Call Placed 📈*\n"
                f"Stock: {symbol} ({shares} shares)\n"
                f"Strike: ${best_call['strike']:.2f}\n"
                f"Expiry: {best_call['expiry']} ({best_call['dte']} DTE)\n"
                f"Premium: ${best_call['premium']:.2f}/share\n"
                f"Total income: ${total:,.2f}\n\n"
                f"*Split immediately:*\n"
                f"→ ETF bucket: +${etf_cut:,.2f} (60%)\n"
                f"→ Your cash: +${cash_cut:,.2f} (30%)\n"
                f"→ Bot capital: +${bot_cut:,.2f} (10%)"
            )
        except Exception as e:
            send_alert(f"*Covered call error* {symbol}: {e}")


# ── ETF sweep from ETF bucket ────────────────────────────────────────────────

def run_etf_sweep(encrypted: str):
    etf_bucket = get_etf_bucket()
    print(f"\n-- ETF bucket: ${etf_bucket:,.2f} | Min to sweep: ${ETF_MIN_SWEEP:,.2f} --")

    if etf_bucket < ETF_MIN_SWEEP:
        print("ETF bucket below minimum — accumulating.")
        return

    best_etfs = scan_best_etfs(etf_bucket, top_n=2)
    if not best_etfs:
        print("No ETF signals found.")
        return

    per_etf = etf_bucket / len(best_etfs)
    for etf in best_etfs:
        symbol   = etf["symbol"]
        price    = etf["price"]
        quantity = int(per_etf // price)
        if quantity < 1:
            continue
        try:
            place_equity_order(encrypted, symbol, quantity, "BUY")
            cost = quantity * price
            deduct_etf_bucket(cost)
            send_alert(
                f"*ETF Buy (from profits)*\n"
                f"Symbol: {symbol}\n"
                f"Shares: {quantity}\n"
                f"Price: ${price:.2f}\n"
                f"Total: ${cost:,.2f}\n"
                f"Score: {etf['score']:.1f}\n"
                f"ETF bucket remaining: ${get_etf_bucket():,.2f}"
            )
            print(f"Swept ${cost:,.2f} into {symbol}")
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

        # Always sync ledger with Schwab — picks up portfolio after every update
        sync_ledger_from_schwab(encrypted)

        capital    = get_trading_capital()
        bucket     = get_profit_bucket()
        etf_bucket = get_etf_bucket()
        cash_bucket = get_cash_bucket()
        tier       = "Tier 1 (<$5k)" if account_value < 5000 else "Tier 2 (<$20k)" if account_value < 20000 else "Tier 3 ($20k+)"

        print(f"Account: ${account_value:,.2f} | Cash: ${cash:,.2f} | Capital: ${capital:,.2f} | Profit: ${bucket:,.2f} | ETF bucket: ${etf_bucket:,.2f} | {tier}")

        # Detect new deposits
        deposit = detect_deposit(cash)

        # Check token health every run
        check_token_health()
        if deposit > 0:
            send_alert(
                f"*New Deposit Detected 💵*\n"
                f"Amount: ${deposit:,.2f}\n"
                f"Trading capital: ${get_trading_capital():,.2f}\n"
                f"Scanning for best stocks now..."
            )

        cash = run_stock_strategy(encrypted, positions, cash, account_value)
        run_options_strategy(encrypted, positions, account_value)
        run_etf_sweep(encrypted)

        # Remind about cash payout if accumulated
        if cash_bucket > 20:
            send_alert(
                f"*Your Cash Payout Available 💵*\n"
                f"${cash_bucket:,.2f} earned from profits\n"
                f"Sitting in your Schwab cash balance\n"
                f"Transfer to bank or funded account anytime!"
            )

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

        print("Syncing ledger with Schwab account...")
        sync_ledger_from_schwab(encrypted)

        capital    = get_trading_capital()
        bucket     = get_profit_bucket()
        etf_bucket = get_etf_bucket()
        tier       = "Tier 1 (<$5k)" if account_value < 5000 else "Tier 2 (<$20k)" if account_value < 20000 else "Tier 3 ($20k+)"

        send_alert(
            f"*Schwab Bot Started ✅*\n"
            f"Account: ${account_value:,.2f}\n"
            f"Cash: ${cash:,.2f}\n"
            f"Trading capital: ${capital:,.2f}\n"
            f"All-time profit: ${bucket:,.2f}\n"
            f"ETF bucket: ${etf_bucket:,.2f}\n"
            f"Split: 60% ETFs / 30% Cash / 10% Bot\n"
            f"Splits on every profitable sell\n"
            f"Tier: {tier}\n"
            f"Scanner active every {CHECK_INTERVAL} min"
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
