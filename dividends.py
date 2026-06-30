import requests
from datetime import datetime, timedelta
from auth import get_valid_token

BASE_URL = "https://api.schwabapi.com/trader/v1"


def headers():
    return {"Authorization": f"Bearer {get_valid_token()}"}


def get_recent_dividends(encrypted: str, days_back: int = 2) -> list:
    """
    Fetch recent DIVIDEND_OR_INTEREST transactions from Schwab.
    Returns list of dividend events with symbol, amount, and whether reinvested.
    """
    end = datetime.utcnow()
    start = end - timedelta(days=days_back)

    try:
        resp = requests.get(
            f"{BASE_URL}/accounts/{encrypted}/transactions",
            headers=headers(),
            params={
                "startDate": start.strftime("%Y-%m-%dT00:00:00.000Z"),
                "endDate":   end.strftime("%Y-%m-%dT23:59:59.000Z"),
                "types":     "DIVIDEND_OR_INTEREST"
            },
            timeout=15
        )
        resp.raise_for_status()
        transactions = resp.json()
    except Exception as e:
        print(f"Dividend fetch error: {e}")
        return []

    # Also check for reinvestment buy transactions (often tagged as DIVIDEND_REINVESTMENT)
    try:
        resp2 = requests.get(
            f"{BASE_URL}/accounts/{encrypted}/transactions",
            headers=headers(),
            params={
                "startDate": start.strftime("%Y-%m-%dT00:00:00.000Z"),
                "endDate":   end.strftime("%Y-%m-%dT23:59:59.000Z"),
                "types":     "DIVIDEND_REINVESTMENT"
            },
            timeout=15
        )
        if resp2.ok:
            transactions += resp2.json()
    except Exception:
        pass

    results = []
    for t in transactions:
        try:
            txn_id = str(t.get("activityId", t.get("transactionId", "")))
            amount = abs(t.get("netAmount", 0))
            symbol = ""
            for leg in t.get("transferItems", []):
                instrument = leg.get("instrument", {})
                if instrument.get("symbol"):
                    symbol = instrument["symbol"]
                    break

            txn_type = t.get("type", "")
            reinvested = "REINVEST" in txn_type.upper()

            if amount > 0:
                results.append({
                    "transaction_id": txn_id,
                    "symbol":         symbol or "Unknown",
                    "amount":         amount,
                    "reinvested":     reinvested,
                    "type":           txn_type
                })
        except Exception as e:
            print(f"Error parsing dividend transaction: {e}")
            continue

    return results
