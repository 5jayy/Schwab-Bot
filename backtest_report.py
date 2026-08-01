"""
WEEKLY/MONTHLY BACKTEST REPORT — full backtest + summary to Telegram.

Runs the put-selling + ETF-call backtest on real EODHD data, computes full
per-config expectancy, and sends the COMPLETE breakdown + summary to Telegram.
Flags if the edge turns negative. Schedule weekly and monthly.

Run:  python3 backtest_report.py WEEKLY   (or MONTHLY)
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
ETF_IV = 0.15

# Put configs (same sweep as backtest_options_real)
PUT_CONFIGS = [
    ("d15 iv35", 0.15, 0.35, 0.50, 2.0),
    ("d20 iv35", 0.20, 0.35, 0.50, 2.0),
    ("d30 iv35", 0.30, 0.35, 0.50, 2.0),
    ("d20 iv50", 0.20, 0.50, 0.50, 2.0),  # the positive live config
    ("d20 hold", 0.20, 0.35, 0.99, 9.0),
]

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
def bs_call(S,K,T,r,sig):
    if T<=0 or sig<=0: return max(0.0,S-K)
    d1=(math.log(S/K)+(r+0.5*sig**2)*T)/(sig*math.sqrt(T)); d2=d1-sig*math.sqrt(T)
    return S*_ncdf(d1)-K*math.exp(-r*T)*_ncdf(d2)
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

def backtest_puts(closes, dt, iv, ep, sm):
    if len(closes)<30: return []
    T=DTE/365; rets=[]; i=20
    while i<len(closes)-DTE:
        S=closes[i]; K=strike_for_delta(S,dt,T,RISK_FREE,iv); p0=bs_put(S,K,T,RISK_FREE,iv)
        if p0<=0.02: i+=DTE; continue
        exited=False
        for d in range(1,DTE+1):
            if i+d>=len(closes): break
            S2=closes[i+d]; Tl=max((DTE-d)/365,1e-4); p=bs_put(S2,K,Tl,RISK_FREE,iv)
            if p<=p0*(1-ep): rets.append((p0-p)/K*100-SLIPPAGE); exited=True; i+=d; break
            if p>=p0*sm: rets.append((p0-p)/K*100-SLIPPAGE); exited=True; i+=d; break
        if not exited:
            Sf=closes[min(i+DTE,len(closes)-1)]; pf=max(0,K-Sf)
            rets.append((p0-pf)/K*100-SLIPPAGE); i+=DTE
    return rets

def backtest_etf_calls(closes):
    if len(closes)<30: return []
    T=DTE/365; rets=[]; i=20
    while i<len(closes)-DTE:
        S=closes[i]; K=round(S*1.02); p0=bs_call(S,K,T,RISK_FREE,ETF_IV)
        if p0<=0.02: i+=DTE; continue
        Sf=closes[min(i+DTE,len(closes)-1)]
        rets.append(p0/S*100 - max(0,(Sf-K))/S*100 - SLIPPAGE); i+=DTE
    return rets

def run(period="WEEKLY"):
    tickers=["AAPL.US","TSLA.US","AMZN.US","VTI.US"]
    allc={}
    for t in tickers:
        c=to_daily(fetch(t))
        if len(c)>=30: allc[t]=c
        time.sleep(0.4)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"[ {period} BACKTEST + SUMMARY ] {ts}", ""]
    lines.append("── PUT SELLING (real data, modeled premium) ──")

    best = None
    for (label, dt, iv, ep, sm) in PUT_CONFIGS:
        rets=[]
        for t,c in allc.items(): rets += backtest_puts(c, dt, iv, ep, sm)
        e = expectancy(rets)
        if e:
            mark = "✅" if e["exp"]>0 else "⚠️"
            star = " ⭐LIVE" if label=="d20 iv50" else ""
            lines.append(f"{mark} {label}: {e['n']}tr {e['wr']:.0f}%win exp {e['exp']:+.3f}%{star}")
            if best is None or e["exp"]>best[1]:
                best = (label, e["exp"])

    # ETF calls
    er=[]
    for t,c in allc.items():
        if t.startswith(("VTI","SPY","QQQ","VOO")): er+=backtest_etf_calls(c)
    if er:
        e=expectancy(er)
        mark = "✅" if e["exp"]>0 else "⚠️"
        lines.append("")
        lines.append("── ETF COVERED CALLS ──")
        lines.append(f"{mark} {e['n']}tr {e['wr']:.0f}%win exp {e['exp']:+.3f}%")

    # SUMMARY
    lines.append("")
    lines.append("── SUMMARY ──")
    if best:
        if best[1] > 0:
            lines.append(f"✅ Best: {best[0]} at {best[1]:+.3f}%/trade")
            lines.append("Edge holding. Run small, track real vs modeled.")
        else:
            lines.append(f"⚠️ Best: {best[0]} at {best[1]:+.3f}% — ALL NEGATIVE")
            lines.append("Edge gone. Pause new puts, review IV filter.")
    lines.append("")
    lines.append("(Premiums modeled — confirm real premiums live.)")

    msg = "\n".join(lines)
    send_alert(msg)
    print(msg)

if __name__ == "__main__":
    import sys
    run(sys.argv[1] if len(sys.argv)>1 else "WEEKLY")
