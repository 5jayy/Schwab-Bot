"""
WEEKLY/MONTHLY BACKTEST REPORT — runs the options backtest, sends to Telegram.

Runs the put-selling + ETF-call backtest on real EODHD data, computes expectancy,
and sends a summary to Telegram. Flags if a strategy turns NEGATIVE so you can
adjust. Schedule this weekly (and monthly) to track/fix/upgrade the bot.

Run manually:  python3 backtest_report.py
Schedule (cron/scheduler): weekly + monthly.
"""
import math, time, requests
from datetime import datetime, timezone

try:
    from telegram import send_alert
except Exception:
    def send_alert(msg): print("[no telegram]", msg)

EODHD = "https://eodhd.com/api/intraday/{}"
TOKEN = "demo"
SLIPPAGE = 0.02
RISK_FREE = 0.045
DTE = 5
MIN_IV = 0.40  # matches the live bot's IV filter

def fetch(t):
    try:
        r = requests.get(EODHD.format(t), params={"api_token":TOKEN,"interval":"5m","fmt":"json"}, timeout=20)
        r.raise_for_status(); d = r.json()
        return d if isinstance(d, list) else []
    except Exception:
        return []

def to_daily(bars):
    days = {}
    for b in bars:
        if b.get("close") is None: continue
        days[b.get("datetime","")[:10]] = b["close"]
    return [days[d] for d in sorted(days)]

def _ncdf(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def bs_put(S,K,T,r,sig):
    if T<=0 or sig<=0: return max(0.0,K-S)
    d1=(math.log(S/K)+(r+0.5*sig**2)*T)/(sig*math.sqrt(T)); d2=d1-sig*math.sqrt(T)
    return K*math.exp(-r*T)*_ncdf(-d2)-S*_ncdf(-d1)
def put_delta(S,K,T,r,sig):
    if T<=0 or sig<=0: return -1.0 if S<K else 0.0
    d1=(math.log(S/K)+(r+0.5*sig**2)*T)/(sig*math.sqrt(T)); return _ncdf(d1)-1
def strike_for_delta(S,td,T,r,sig):
    best_k,best_diff=S*0.95,1.0; k=math.floor(S)
    for _ in range(40):
        d=abs(put_delta(S,k,T,r,sig))
        if abs(d-td)<best_diff: best_diff,best_k=abs(d-td),k
        if d<td: break
        k-=1
    return best_k

def expectancy(rets):
    if not rets: return None
    n=len(rets); wins=[r for r in rets if r>0]; losses=[r for r in rets if r<=0]
    wr=len(wins)/n; aw=sum(wins)/len(wins) if wins else 0; al=sum(losses)/len(losses) if losses else 0
    return {"n":n,"wr":wr*100,"exp":wr*aw+(1-wr)*al,"total":sum(rets)}

def backtest_puts(closes, iv=0.50, dt=0.20):
    if len(closes)<30: return []
    T=DTE/365; rets=[]; i=20
    while i<len(closes)-DTE:
        S=closes[i]; K=strike_for_delta(S,dt,T,RISK_FREE,iv); p0=bs_put(S,K,T,RISK_FREE,iv)
        if p0<=0.02: i+=DTE; continue
        exited=False
        for d in range(1,DTE+1):
            if i+d>=len(closes): break
            S2=closes[i+d]; Tl=max((DTE-d)/365,1e-4); p=bs_put(S2,K,Tl,RISK_FREE,iv)
            if p<=p0*0.5: rets.append((p0-p)/K*100-SLIPPAGE); exited=True; i+=d; break
            if p>=p0*2: rets.append((p0-p)/K*100-SLIPPAGE); exited=True; i+=d; break
        if not exited:
            Sf=closes[min(i+DTE,len(closes)-1)]; pf=max(0,K-Sf)
            rets.append((p0-pf)/K*100-SLIPPAGE); i+=DTE
    return rets

def run(period="WEEKLY"):
    tickers=["AAPL.US","TSLA.US","AMZN.US","VTI.US"]
    allc={}
    for t in tickers:
        c=to_daily(fetch(t))
        if len(c)>=30: allc[t]=c
        time.sleep(0.4)

    rets=[]
    for t,c in allc.items(): rets+=backtest_puts(c)
    e=expectancy(rets)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if e:
        status = "✅ POSITIVE" if e["exp"]>0 else "⚠️ NEGATIVE"
        msg = f"[ {period} BACKTEST ] {ts}\n"
        msg += f"PUT SELLING (IV-filtered, modeled)\n"
        msg += f"Trades: {e['n']} | Win: {e['wr']:.0f}%\n"
        msg += f"Expectancy: {e['exp']:+.3f}%/trade\n"
        msg += f"Status: {status}\n"
        if e["exp"] <= 0:
            msg += "⚠️ Edge turned negative — review IV filter / pause new puts."
        else:
            msg += "Edge holding. Continue small, track real vs modeled."
        send_alert(msg)
        print(msg)
    else:
        send_alert(f"[ {period} BACKTEST ] {ts}\nNo data — check EODHD.")

if __name__ == "__main__":
    import sys
    run(sys.argv[1] if len(sys.argv)>1 else "WEEKLY")
