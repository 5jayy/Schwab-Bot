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

    # Pre-bot positions — never register these (they corrupt win rate & max trades)
    # AUTO-SNAPSHOT: uses LIVE Schwab holdings (the 'positions' pulled from the
    # Schwab API this very run) as the source of truth. On first run OR a new deploy,
    # it reconciles the hands-off list against what Schwab actually shows right now.
    lg_snap = load_ledger()
    pre_bot_snapshot = set(lg_snap.get("pre_bot_snapshot", []))

    # Live Schwab holdings this run (source of truth)
    held_now = {p["instrument"]["symbol"] for p in positions}

    # Detect a new deploy via a build marker (env var set at deploy, or first run)
    import os as _os
    deploy_id = _os.getenv("FLY_MACHINE_VERSION", "") or _os.getenv("FLY_IMAGE_REF", "") or "manual"
    last_deploy = lg_snap.get("last_deploy_id", "")

    first_run    = not lg_snap.get("snapshot_taken", False)
    new_deploy   = deploy_id != last_deploy

    if first_run or new_deploy:
        # Reconcile snapshot with LIVE Schwab holdings:
        # keep tracking anything already in snapshot that's STILL held,
        # and on first run, snapshot everything currently held.
        if first_run:
            pre_bot_snapshot = set(held_now)  # everything held now = pre-existing
        else:
            # New deploy — keep existing snapshot but drop anything no longer held
            pre_bot_snapshot = pre_bot_snapshot & held_now

        lg_snap["pre_bot_snapshot"] = list(pre_bot_snapshot)
        lg_snap["snapshot_taken"]   = True
        lg_snap["last_deploy_id"]   = deploy_id
        save_ledger(lg_snap)
        tag = "first run" if first_run else "new deploy"
        print(f"Auto-snapshot ({tag}): {len(pre_bot_snapshot)} hands-off from live Schwab: {sorted(pre_bot_snapshot)}")

    # Combine hardcoded pre-bot + live-verified auto-snapshot
    PRE_BOT = {"LCID", "OPEN"} | pre_bot_snapshot

    for p in positions:
        sym = p["instrument"]["symbol"]
        if sym in PRE_BOT:
            continue  # skip pre-bot / pre-existing positions (hands-off)
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

    # SNAPSHOT RELEASE: if a pre-existing position was SOLD (no longer held),
    # remove it from the snapshot so a future bot buy of that ticker CAN be managed.
    held_symbols = {p["instrument"]["symbol"] for p in positions}
    if pre_bot_snapshot:
        released = [s for s in pre_bot_snapshot if s not in held_symbols]
        if released:
            lg_rel = load_ledger()
            snap = set(lg_rel.get("pre_bot_snapshot", []))
            snap -= set(released)
            lg_rel["pre_bot_snapshot"] = list(snap)
            save_ledger(lg_rel)
            print(f"Snapshot release: sold pre-existing positions freed for bot: {released}")

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


def record_buy(symbol: str, quantity: int, price: float, cost: float,
               bucket: str = "swing", stop_pct: float = 0.07):
    """Record buy with scale-out TP levels and R/R targets."""
    ledger = load_ledger()
    # 1:3 R/R — day tighter, swing wider
    if bucket == "day":
        tp1_pct = stop_pct * 1.5   # ⅓ out at 1.5R
        tp2_pct = stop_pct * 2.5   # ⅓ out at 2.5R
    else:
        tp1_pct = stop_pct * 2.0   # ⅓ out at 2R
        tp2_pct = stop_pct * 3.0   # ⅓ out at 3R

    ledger["open_trades"][symbol] = {
        "quantity":    quantity,
        "buy_price":   price,
        "cost":        cost,
        "high_price":  price,
        "bucket":      bucket,
        "stop_pct":    stop_pct,
        "tp1_pct":     tp1_pct,
        "tp2_pct":     tp2_pct,
        "tp1_hit":     False,
        "tp2_hit":     False,
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
    """Returns full stop info for a symbol, or None if not tracked."""
    ledger = load_ledger()
    trade = ledger.get("open_trades", {}).get(symbol)
    if not trade:
        return None
    return {
        "buy_price":  trade["buy_price"],
        "high_price": trade.get("high_price", trade["buy_price"]),
        "quantity":   trade.get("quantity", 0),
        "tp1_hit":    trade.get("tp1_hit", False),
        "tp2_hit":    trade.get("tp2_hit", False),
        "tp1_pct":    trade.get("tp1_pct", 0.07),
        "tp2_pct":    trade.get("tp2_pct", 0.12),
        "stop_pct":   trade.get("stop_pct", 0.05),
        "bucket":     trade.get("bucket", "swing"),
        "atr_pct":    trade.get("atr_pct", 2.0),
    }


def get_pressure_trail(symbol: str, buy_price: float, high_price: float,
                       current_price: float, candles: list = None,
                       base_trail: float = 0.07) -> dict:
    """
    Pressure trailing exit — trails based on candle pressure + order flow.
    Not price-based. Exits when pressure flips not when price drops.

    Pressure flip signals:
    - Candles turning red after green run
    - Upper wicks appearing (sellers rejecting highs)
    - Order flow imbalance shifting to sellers
    - Volume dropping on up candles (momentum fading)

    More aggressive than fixed trail — books profits at peak pressure.
    """
    if buy_price <= 0:
        return {"stop_price": 0, "reason": "invalid", "profit_pct": 0, "pressure": 0}

    profit_pct = (current_price - buy_price) / buy_price

    # Always breakeven at 2%
    if 0.02 <= profit_pct < 0.05:
        return {
            "stop_price":  buy_price,
            "trail_pct":   base_trail,
            "reason":      "breakeven",
            "profit_pct":  profit_pct,
            "pressure":    0
        }

    if profit_pct < 0.02:
        return {
            "stop_price": high_price * (1 - base_trail),
            "trail_pct":  base_trail,
            "reason":     "trail_base",
            "profit_pct": profit_pct,
            "pressure":   0
        }

    # Measure sell pressure from candles
    pressure_score = 0
    trail_pct      = base_trail

    if candles and len(candles) >= 5:
        recent = candles[-5:]

        # Signal 1: Upper wicks on recent candles (sellers rejecting highs)
        wick_count = 0
        for c in recent:
            rng = c["high"] - c["low"]
            if rng > 0:
                upper_wick = c["high"] - max(c["open"], c["close"])
                if upper_wick > rng * 0.4:
                    wick_count += 1
        pressure_score += wick_count * 10  # 0-50 pts

        # Signal 2: Consecutive red candles
        red_count = sum(1 for c in recent if c["close"] < c["open"])
        pressure_score += red_count * 8  # 0-40 pts

        # Signal 3: Volume declining on up candles
        up_candles = [c for c in recent if c["close"] > c["open"]]
        if len(up_candles) >= 2:
            if up_candles[-1]["volume"] < up_candles[-2]["volume"] * 0.8:
                pressure_score += 15  # volume fading on up moves

        # Pressure determines trail tightness
        if pressure_score >= 60:
            trail_pct = 0.02   # high pressure — very tight trail
            reason    = "pressure_high"
        elif pressure_score >= 35:
            trail_pct = 0.03   # moderate pressure — tighten
            reason    = "pressure_moderate"
        elif pressure_score >= 15:
            trail_pct = 0.04   # low pressure — slight tighten
            reason    = "pressure_low"
        else:
            trail_pct = 0.05   # no pressure — give room
            reason    = "pressure_none"
    else:
        reason = "trail_profit"
        trail_pct = 0.05

    stop_price = high_price * (1 - trail_pct)

    return {
        "stop_price":    stop_price,
        "trail_pct":     trail_pct,
        "reason":        reason,
        "profit_pct":    profit_pct,
        "pressure":      pressure_score
    }


# ── Tax tracker ───────────────────────────────────────────────────────────────

MARYLAND_SHORT_TERM = 0.3775   # federal + MD state short term
MARYLAND_LONG_TERM  = 0.2075   # federal + MD state long term
MARYLAND_QUALIFIED  = 0.2075   # qualified dividends rate


def record_taxable_event(symbol: str, profit: float, hold_days: int,
                          event_type: str = "stock"):
    """
    Records every taxable event for annual tax calculation.
    Types: stock, options, dividend, etf_sale
    """
    ledger = load_ledger()
    event  = {
        "symbol":     symbol,
        "profit":     profit,
        "hold_days":  hold_days,
        "type":       event_type,
        "long_term":  hold_days >= 365,
        "tax_rate":   MARYLAND_LONG_TERM if hold_days >= 365 else MARYLAND_SHORT_TERM,
        "tax_owed":   profit * (MARYLAND_LONG_TERM if hold_days >= 365 else MARYLAND_SHORT_TERM),
        "timestamp":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    ledger.setdefault("tax_events", []).append(event)
    ledger["ytd_tax_owed"] = ledger.get("ytd_tax_owed", 0.0) + max(event["tax_owed"], 0)
    save_ledger(ledger)


def get_tax_report() -> dict:
    """
    Calculate annual tax report from all recorded events.
    Shows exactly what you owe by category.
    """
    ledger = load_ledger()
    events = ledger.get("tax_events", [])

    short_gains = sum(e["profit"] for e in events if e["profit"] > 0 and not e["long_term"] and e["type"] in ("stock", "options"))
    long_gains  = sum(e["profit"] for e in events if e["profit"] > 0 and e["long_term"])
    short_loss  = sum(e["profit"] for e in events if e["profit"] < 0 and not e["long_term"])
    dividends   = sum(e["profit"] for e in events if e["type"] == "dividend")

    net_short   = short_gains + short_loss
    tax_short   = max(net_short, 0) * MARYLAND_SHORT_TERM
    tax_long    = max(long_gains, 0) * MARYLAND_LONG_TERM
    tax_div     = max(dividends, 0) * MARYLAND_QUALIFIED
    total_owed  = tax_short + tax_long + tax_div

    return {
        "short_term_gains":  round(short_gains, 2),
        "short_term_losses": round(short_loss, 2),
        "net_short_term":    round(net_short, 2),
        "long_term_gains":   round(long_gains, 2),
        "dividends":         round(dividends, 2),
        "tax_short_term":    round(tax_short, 2),
        "tax_long_term":     round(tax_long, 2),
        "tax_dividends":     round(tax_div, 2),
        "total_tax_owed":    round(total_owed, 2),
        "ytd_events":        len(events),
    }


def get_dynamic_stop(buy_price: float, high_price: float, current_price: float,
                      base_trail: float = 0.07, candles: list = None,
                      bought_at: str = None) -> dict:
    """
    Dynamic trailing stop — tightens based on candle strength not fixed % tiers.

    Breakeven always locks at +2% (never lose on a winner).
    After breakeven, trail tightens based on how strong recent candles are.

    FAST-POP RULE: if profit came FAST (mover spike), tighten immediately.
    A +4% gain in <60 min is a spike that often fades — lock it before it reverses.
    A slow +4% over days is a trend — give it room to run.
    """
    if buy_price <= 0:
        return {"stop_price": 0, "trail_pct": base_trail, "reason": "invalid", "profit_pct": 0}

    profit_pct = (current_price - buy_price) / buy_price

    # ── FAST-POP DETECTION ──
    # If we hit +4% within 60 min of entry, it's a mover spike — tighten hard
    fast_pop = False
    if bought_at and profit_pct >= 0.04:
        try:
            from datetime import datetime as _dt
            entry = _dt.strptime(bought_at[:19], "%Y-%m-%dT%H:%M:%S")
            mins_held = (_dt.utcnow() - entry).total_seconds() / 60
            if mins_held <= 60:  # fast pop — spike likely to fade
                fast_pop = True
        except Exception:
            pass

    if fast_pop:
        # Tight 2.5% trail to lock the quick pop before it fades
        return {
            "stop_price": high_price * (1 - 0.025),
            "trail_pct":  0.025,
            "reason":     "fast_pop_lock",
            "profit_pct": profit_pct
        }

    # Breakeven — always at 2%, non-negotiable
    if 0.02 <= profit_pct < 0.05:
        return {
            "stop_price": buy_price,
            "trail_pct":  base_trail,
            "reason":     "breakeven",
            "profit_pct": profit_pct
        }

    # Below breakeven — base trail
    if profit_pct < 0.02:
        return {
            "stop_price": high_price * (1 - base_trail),
            "trail_pct":  base_trail,
            "reason":     "trail_base",
            "profit_pct": profit_pct
        }

    # Above breakeven — candle strength determines trail tightness
    trail_pct = base_trail  # default

    if candles and len(candles) >= 3:
        # Measure recent candle strength
        recent = candles[-3:]
        strengths = []
        for c in recent:
            rng  = c["high"] - c["low"]
            if rng == 0:
                continue
            body     = abs(c["close"] - c["open"])
            close_up = (c["close"] - c["low"]) / rng
            strength = (body / rng * 0.6) + (close_up * 0.4)
            strengths.append(strength)

        avg_strength = sum(strengths) / len(strengths) if strengths else 0.5

        # Dynamic trail — TIGHTER for movers (bought mid-run, less room left)
        # Movers are already extended, so protect gains fast to avoid green-to-red
        if avg_strength >= 0.75:
            trail_pct = 0.02  # very strong — tight, lock gains (was 3%)
            reason    = "trail_candle_strong"
        elif avg_strength >= 0.55:
            trail_pct = 0.03  # solid — moderate tight (was 4%)
            reason    = "trail_candle_moderate"
        elif avg_strength >= 0.35:
            trail_pct = 0.035  # average — still tight (was 5%)
            reason    = "trail_candle_weak"
        else:
            trail_pct = 0.04  # weak — momentum fading, exit near peak (was 6%)
            reason    = "trail_candle_fading"
    else:
        # No candle data — use profit-based fallback
        if profit_pct >= 0.20:
            trail_pct = 0.02
            reason    = "trail_profit_20pct"
        elif profit_pct >= 0.10:
            trail_pct = 0.03
            reason    = "trail_profit_10pct"
        else:
            trail_pct = 0.035
            reason    = "trail_profit_5pct"

    stop_price = high_price * (1 - trail_pct)

    return {
        "stop_price": stop_price,
        "trail_pct":  trail_pct,
        "reason":     reason,
        "profit_pct": profit_pct
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
