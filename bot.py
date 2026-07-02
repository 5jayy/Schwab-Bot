import os
import sys
import signal
import schedule
import time
import requests
from datetime import datetime
import pytz
from dotenv import load_dotenv
from auth import get_valid_token
from scanner import scan_best_stocks, scan_best_etfs
from strategy import get_trade_stocks, get_signal
from options import find_best_covered_call, place_covered_call, check_covered_call_already_open, find_best_cash_secured_put, place_cash_secured_put, check_put_already_open
from dividends import get_recent_dividends
from telegram import send_alert
from token_manager import check_token_health
from ledger import (
    sync_ledger_from_schwab, load_ledger, save_ledger,
    record_buy, record_sell_and_split,
    get_profit_bucket, get_trading_capital,
    get_etf_bucket, get_cash_bucket, get_bot_bucket,
    deduct_etf_bucket, detect_deposit, detect_withdrawal,
    get_withdrawal_stats, record_dividend, get_dividend_stats,
    mark_dividend_seen, update_high_price, get_trailing_stop_info,
    BOT_STOCKS, ETF_MIN_SWEEP
)

load_dotenv()


def is_market_open() -> bool:
    """Check if US stock market is currently open."""
    et = pytz.timezone('America/New_York')
    now = datetime.now(et)
    # Monday=0, Friday=4 — no weekends
    if now.weekday() > 4:
        print(f"Market closed — weekend ({now.strftime('%A')})")
        return False
    market_open  = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if not (market_open <= now <= market_close):
        print(f"Market closed — outside hours ({now.strftime('%H:%M')} ET)")
        return False
    return True

def handle_shutdown(signum, frame):
    print("Shutdown signal received — bot stopping cleanly.")
    sys.exit(0)

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

BASE_URL       = "https://api.schwabapi.com/trader/v1"
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", 0.07))  # 7% drop from peak triggers sell
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
    """Use cash actually available for trading — excludes put collateral and unsettled funds."""
    try:
        balances = account["securitiesAccount"]["currentBalances"]
        # cashAvailableForTrading excludes collateral locked in puts and unsettled cash
        available = balances.get("cashAvailableForTrading", None)
        if available is not None:
            return max(available, 0.0)
        # Fallback to cashBalance if field not present
        cash = balances.get("cashBalance", 0.0)
        return max(cash, 0.0)
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


def check_order_filled(encrypted: str, order_location: str) -> dict:
    """
    Check if an order actually filled or got rejected/canceled.
    order_location is the Location header from the order POST response.
    Returns dict with status and statusDescription.
    """
    import time
    time.sleep(1.5)  # give Schwab a moment to process
    try:
        resp = requests.get(order_location, headers=headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {
            "status":      data.get("status", "UNKNOWN"),
            "description": data.get("statusDescription", ""),
            "filled":      data.get("status") == "FILLED"
        }
    except Exception as e:
        return {"status": "UNKNOWN", "description": str(e), "filled": False}


# ── Sell with immediate profit split ────────────────────────────────────────

def execute_sell(encrypted: str, symbol: str, quantity: int, price: float, cash: float, reason: str = "signal") -> float:
    try:
        place_equity_order(encrypted, symbol, quantity, "SELL")
        proceeds = quantity * price
        split    = record_sell_and_split(symbol, quantity, price, proceeds, ETF_PCT, CASH_PCT, BOT_PCT)
        cash    += proceeds
        profit   = split["profit"]
        label    = "PROFIT" if profit > 0 else "LOSS"
        tag      = "🛑 Trailing Stop" if reason == "trailing_stop" else "Signal"

        if profit > 0:
            send_alert(
                f"*Stock Sell — {label} 💰*\n"
                f"Symbol: {symbol}\n"
                f"Shares: {quantity}\n"
                f"Price: ${price:.2f}\n"
                f"Proceeds: ${proceeds:,.2f}\n"
                f"Profit: +${profit:,.2f}\n"
                f"Trigger: {tag}\n\n"
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
                f"Loss: ${profit:,.2f}\n"
                f"Trigger: {tag}"
            )
        print(f"  Sold {quantity} {symbol} @ ${price:.2f} | P&L ${profit:+,.2f} | {tag}")
    except Exception as e:
        send_alert(f"*Sell error* {symbol}: {e}")
    return cash


# ── Stock strategy ───────────────────────────────────────────────────────────

def run_stock_strategy(encrypted: str, positions: list, cash: float, account_value: float) -> float:
    tier          = "Tier 1 (<$5k)" if account_value < 5000 else "Tier 2 (<$20k)" if account_value < 20000 else "Tier 3 ($20k+)"
    # Position sizing by tier — matches scanner budget
    if account_value < 5000:
        pos_pct = 0.30
    elif account_value < 20000:
        pos_pct = 0.25
    else:
        pos_pct = 0.20
    position_size = cash * pos_pct
    bought_this_run = set()

    # Always check existing positions for sell signals regardless of cash
    sold_this_run = set()
    for position in positions:
        sym = position["instrument"]["symbol"]
        if sym not in BOT_STOCKS:
            continue
        if sym in sold_this_run:
            continue

        sig   = get_signal(sym)
        price = sig.get("price", 0)
        quantity = int(position.get("longQuantity", 0))
        if quantity < 1 or price <= 0:
            continue

        # Update peak price tracker for trailing stop
        update_high_price(sym, price)
        trail_info = get_trailing_stop_info(sym)

        trigger_reason = None

        if sig["signal"] == "SELL":
            trigger_reason = "signal"
        elif trail_info:
            high = trail_info["high_price"]
            drop_pct = (high - price) / high if high > 0 else 0
            if drop_pct >= TRAILING_STOP_PCT:
                trigger_reason = "trailing_stop"

        if trigger_reason:
            # Never sell a stock that has an open covered call
            if check_covered_call_already_open(encrypted, sym):
                print(f"  {sym}: skipping sell — covered call open")
                continue
            sold_this_run.add(sym)
            if trigger_reason == "trailing_stop":
                high = trail_info["high_price"]
                print(f"  {sym}: trailing stop triggered — peak ${high:.2f} now ${price:.2f} ({(high-price)/high*100:.1f}% drop)")
            cash = execute_sell(encrypted, sym, quantity, price, cash, trigger_reason)

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
            order_resp = place_equity_order(encrypted, symbol, quantity, "BUY")
            order_location = order_resp.headers.get("Location", "")
            cost = quantity * price

            if order_location:
                check = check_order_filled(encrypted, order_location)
                if not check["filled"] and check["status"] in ("REJECTED", "CANCELED"):
                    send_alert(
                        f"*Order Canceled ❌*\n"
                        f"Symbol: {symbol}\n"
                        f"Shares: {quantity}\n"
                        f"Reason: {check['description']}\n"
                        f"Cash available: ${cash:,.2f}"
                    )
                    bought_this_run.add(symbol)  # don't retry this run
                    continue

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
            cc_resp = place_covered_call(encrypted, best_call["option_symbol"], best_call["contracts"], best_call["premium"])
            cc_location = cc_resp.headers.get("Location", "")

            if cc_location:
                check = check_order_filled(encrypted, cc_location)
                if not check["filled"] and check["status"] in ("REJECTED", "CANCELED"):
                    send_alert(
                        f"*Covered Call Canceled ❌*\n"
                        f"Symbol: {symbol}\n"
                        f"Strike: ${best_call['strike']:.2f}\n"
                        f"Reason: {check['description']}"
                    )
                    continue

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



# ── Cash secured puts ────────────────────────────────────────────────────────

def run_cash_secured_puts(encrypted: str, cash: float, account_value: float):
    if cash < 200:
        print("Not enough cash for cash secured puts.")
        return

    print("\n-- Cash secured puts | Cash available: $" + f"{cash:,.2f} --")

    from scanner import scan_best_stocks
    candidates = scan_best_stocks(cash, top_n=3)

    for stock in candidates:
        symbol = stock["symbol"]
        price  = stock["price"]

        if check_put_already_open(encrypted, symbol):
            print(f"  {symbol}: put already open")
            continue

        best_put = find_best_cash_secured_put(symbol, price, cash)
        if not best_put:
            print(f"  {symbol}: no good put found")
            continue

        try:
            put_resp = place_cash_secured_put(encrypted, best_put["option_symbol"], best_put["premium"])
            put_location = put_resp.headers.get("Location", "")

            if put_location:
                check = check_order_filled(encrypted, put_location)
                if not check["filled"] and check["status"] in ("REJECTED", "CANCELED"):
                    send_alert(
                        f"*Cash Secured Put Canceled ❌*\n"
                        f"Symbol: {symbol}\n"
                        f"Strike: ${best_put['strike']:.2f}\n"
                        f"Reason: {check['description']}"
                    )
                    continue

            total    = best_put["total_premium"]
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

            msg = (
                "*Cash Secured Put Placed*\n"
                f"Stock: {symbol}\n"
                f"Price: ${best_put['underlying_price']:.2f}\n"
                f"Strike: ${best_put['strike']:.2f}\n"
                f"Expiry: {best_put['expiry']} ({best_put['dte']} DTE)\n"
                f"Premium: ${best_put['premium']:.2f}/share\n"
                f"Total: ${total:,.2f}\n"
                f"ETF bucket: +${etf_cut:,.2f} | Cash: +${cash_cut:,.2f} | Bot: +${bot_cut:,.2f}"
            )
            send_alert(msg)
        except Exception as e:
            send_alert(f"*Put error* {symbol}: {e}")


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



# ── Dividend tracking ────────────────────────────────────────────────────────

def check_dividends(encrypted: str):
    """Check for new dividend payments and record them."""
    dividends = get_recent_dividends(encrypted, days_back=2)
    seen_ids = get_dividend_stats()["seen_dividend_ids"]

    for div in dividends:
        txn_id = div["transaction_id"]
        if txn_id in seen_ids:
            continue

        record_dividend(div["symbol"], div["amount"], div["reinvested"])
        mark_dividend_seen(txn_id)

        stats = get_dividend_stats()
        label = "Reinvested 🔄" if div["reinvested"] else "Cash 💵"
        send_alert(
            f"*Dividend Received {label}*\n"
            f"Symbol: {div['symbol']}\n"
            f"Amount: ${div['amount']:,.2f}\n\n"
            f"All-time dividends: ${stats['total_dividends']:,.2f}\n"
            f"Reinvested total: ${stats['dividends_reinvested']:,.2f}\n"
            f"Cash total: ${stats['dividends_cash']:,.2f}"
        )
        print(f"  Dividend: {div['symbol']} ${div['amount']:.2f} ({'reinvested' if div['reinvested'] else 'cash'})")



# ── 24/7 Balance Monitor ─────────────────────────────────────────────────────

def check_balance_24_7():
    """
    Runs every 30 minutes around the clock.
    Only checks for deposits and withdrawals — no trading.
    """
    try:
        accounts  = get_account_numbers()
        encrypted = accounts[0]["hashValue"]
        account   = get_account(encrypted)
        cash      = get_cash_balance(account)

        deposit = detect_deposit(cash)
        if deposit > 0:
            send_alert(
                f"*New Deposit Detected 💵*\n"
                f"Amount: ${deposit:,.2f}\n"
                f"Trading capital: ${get_trading_capital():,.2f}\n"
                f"Cash now available for trading!"
            )

        withdrawal = detect_withdrawal(cash)
        if withdrawal > 0:
            stats = get_withdrawal_stats()
            send_alert(
                f"*Withdrawal Detected 🏦*\n"
                f"Amount: ${withdrawal:,.2f}\n"
                f"Total withdrawn all time: ${stats['total_withdrawn']:,.2f}\n"
                f"Remaining cash: ${cash:,.2f}"
            )
    except Exception as e:
        print(f"Balance check error: {e}")

# ── Main ─────────────────────────────────────────────────────────────────────

def run_strategy():
    if not is_market_open():
        return
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

        check_dividends(encrypted)

        cash = run_stock_strategy(encrypted, positions, cash, account_value)
        run_options_strategy(encrypted, positions, account_value)
        run_cash_secured_puts(encrypted, cash, account_value)
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

        # Run balance check BEFORE ledger sync — detects deposits/withdrawals
        # detect_deposit/withdraw compares cash vs last_known_cash saved in ledger
        from ledger import load_ledger
        ledger = load_ledger()
        last_cash = ledger.get("last_known_cash", cash)
        print(f"Balance check — last: ${last_cash:.2f} | current: ${cash:.2f}")

        if cash > last_cash + 1:
            deposit = cash - last_cash
            ledger["deposits"] = ledger.get("deposits", 0.0) + deposit
            ledger["trading_capital"] = ledger.get("trading_capital", 0.0) + deposit
            ledger["last_known_cash"] = cash
            from ledger import save_ledger
            save_ledger(ledger)
            send_alert(
                f"*New Deposit Detected 💵*\n"
                f"Amount: ${deposit:,.2f}\n"
                f"Cash now available for trading!"
            )
        elif last_cash - cash > 1:
            withdrawal = last_cash - cash
            ledger.setdefault("withdrawal_history", []).append({
                "amount": withdrawal,
                "timestamp": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
            })
            ledger["total_withdrawn"] = ledger.get("total_withdrawn", 0.0) + withdrawal
            ledger["last_known_cash"] = cash
            from ledger import save_ledger
            save_ledger(ledger)
            send_alert(
                f"*Withdrawal Detected 🏦*\n"
                f"Amount: ${withdrawal:,.2f}\n"
                f"Total withdrawn all time: ${ledger['total_withdrawn']:,.2f}\n"
                f"Remaining cash: ${cash:,.2f}"
            )
        else:
            ledger["last_known_cash"] = cash
            from ledger import save_ledger
            save_ledger(ledger)

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

    if is_market_open():
        run_strategy()

    # Trading — only during market hours (checked inside run_strategy)
    schedule.every(CHECK_INTERVAL).minutes.do(run_strategy)
    # Balance monitoring — 24/7 every 5 minutes (lightweight cash check only)
    schedule.every(5).minutes.do(check_balance_24_7)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
