"""
Schwab WebSocket Streamer — PHASE 1 (log-only, safe).

Connects to the Schwab streaming API, authenticates with the existing access
token, and subscribes to ACCT_ACTIVITY (account activity: fills, order updates,
position/balance changes). In Phase 1 it ONLY LOGS what it receives — it does
NOT take any trading actions and does NOT modify the ledger.

This module runs ALONGSIDE the polling bot. If the stream fails, the polling
bot is unaffected. Once Phase 1 proves the connection works, Phase 2 wires the
events into the ledger (positions, cash bucket, history) in real time.

Run standalone to test:  python3 stream.py
"""

import json
import time
import asyncio
import requests

try:
    import websockets
except ImportError:
    websockets = None  # handled at runtime with a clear message

from auth import get_valid_token

BASE_URL = "https://api.schwabapi.com/trader/v1"


def get_streamer_info() -> dict:
    """
    Fetch the streamer connection details from the user preferences endpoint.
    Returns the streamerInfo block (socket URL + credentials) plus the account.
    """
    token = get_valid_token()
    resp = requests.get(
        f"{BASE_URL}/userPreference",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    # streamerInfo is a list; take the first entry
    streamer_info = data.get("streamerInfo", [{}])[0]
    accounts      = data.get("accounts", [{}])
    account       = accounts[0] if accounts else {}

    return {
        "socket_url":       streamer_info.get("streamerSocketUrl", ""),
        "customer_id":      streamer_info.get("schwabClientCustomerId", ""),
        "correl_id":        streamer_info.get("schwabClientCorrelId", ""),
        "channel":          streamer_info.get("schwabClientChannel", ""),
        "function_id":      streamer_info.get("schwabClientFunctionId", ""),
        "account_number":   account.get("accountNumber", ""),
    }


def build_login_request(info: dict, token: str) -> dict:
    """Build the LOGIN admin command for the streamer."""
    return {
        "requests": [{
            "service":  "ADMIN",
            "command":  "LOGIN",
            "requestid": "0",
            "SchwabClientCustomerId": info["customer_id"],
            "SchwabClientCorrelId":   info["correl_id"],
            "parameters": {
                "Authorization":          token,
                "SchwabClientChannel":    info["channel"],
                "SchwabClientFunctionId": info["function_id"],
            },
        }]
    }


def build_acct_activity_request(info: dict) -> dict:
    """Build the ACCT_ACTIVITY subscription command."""
    return {
        "requests": [{
            "service":  "ACCT_ACTIVITY",
            "command":  "SUBS",
            "requestid": "1",
            "SchwabClientCustomerId": info["customer_id"],
            "SchwabClientCorrelId":   info["correl_id"],
            "parameters": {
                "keys":   "Account Activity",
                "fields": "0,1,2,3",
            },
        }]
    }


def handle_message(msg: str):
    """
    PHASE 1: just log everything received. No trading, no ledger writes.
    Later phases parse ACCT_ACTIVITY data and update positions/cash/history.
    """
    try:
        data = json.loads(msg)
    except Exception:
        print(f"[STREAM] raw (non-JSON): {msg[:200]}")
        return

    # Response messages (login/subscribe acks)
    if "response" in data:
        for r in data["response"]:
            svc  = r.get("service", "?")
            cmd  = r.get("command", "?")
            code = r.get("content", {}).get("code", "?")
            txt  = r.get("content", {}).get("msg", "")
            print(f"[STREAM] response {svc}/{cmd} code={code} {txt}")

    # Notify messages (heartbeats, connection notices)
    if "notify" in data:
        for n in data["notify"]:
            if "heartbeat" in n:
                print(f"[STREAM] heartbeat {n['heartbeat']}")
            else:
                print(f"[STREAM] notify {json.dumps(n)[:200]}")

    # Data messages (the actual account activity — fills, orders, etc.)
    if "data" in data:
        for d in data["data"]:
            svc = d.get("service", "?")
            print(f"[STREAM] DATA {svc}: {json.dumps(d)[:400]}")


async def run_stream():
    """Connect, login, subscribe, and log messages. Reconnects on drop."""
    if websockets is None:
        print("[STREAM] ERROR: 'websockets' package not installed. "
              "Add 'websockets' to requirements.txt")
        return

    backoff = 2
    while True:
        try:
            info  = get_streamer_info()
            token = get_valid_token()

            if not info["socket_url"]:
                print("[STREAM] ERROR: no streamerSocketUrl from userPreference")
                return

            print(f"[STREAM] connecting to {info['socket_url']}")
            async with websockets.connect(info["socket_url"],
                                          ping_interval=None) as ws:
                # 1. LOGIN
                await ws.send(json.dumps(build_login_request(info, token)))
                login_resp = await ws.recv()
                handle_message(login_resp)

                # 2. SUBSCRIBE to account activity
                await ws.send(json.dumps(build_acct_activity_request(info)))
                sub_resp = await ws.recv()
                handle_message(sub_resp)

                print("[STREAM] subscribed to ACCT_ACTIVITY — listening (log-only)")
                backoff = 2  # reset backoff after a successful connect

                # 3. Listen forever, logging everything
                async for message in ws:
                    handle_message(message)

        except Exception as ex:
            print(f"[STREAM] connection error: {ex}")
            print(f"[STREAM] reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)  # exponential backoff, cap 120s


if __name__ == "__main__":
    print("[STREAM] Phase 1 test — log-only, no trading actions")
    try:
        asyncio.run(run_stream())
    except KeyboardInterrupt:
        print("\n[STREAM] stopped by user")
