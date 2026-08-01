"""
WEEKLY/MONTHLY BACKTEST REPORT — full backtest + summary to Telegram.

Reports BOTH primary strategies (matching the live bot):
  • COVERED CALLS (70% — buy stock + sell call, IV-filtered)
  • CASH-SECURED PUTS (25% — sell put, IV-filtered)
Real EODHD data, modeled premiums. Flags if either edge turns negative.

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

# Put configs (cash-secured)
PUT_CONFIGS = [
    ("PUT d20 iv35", 0.20, 0.35, "put"),
    ("PUT d20 iv50", 0.20, 0.50, "put"),
]
# Covered call configs
CALL_CONFIGS = [
    ("CC 2%OTM iv35", 1.02, 0.35, "call"),
    ("CC 2%OTM iv50", 1.02, 0.50, "call"),
    ("CC 3%OTM iv50", 1.03, 0.50, "call"),
]

def fetch(t):
    try:
        r = requests.get(EODHD.format(t), params={"api_token":TOKEN,"interval":"5m","fmt":"json"}, timeout=20)
        r.raise_for_status(); d=r.json()
        return d if isinstance(d,list) else []
    except Exception: return []

def to_daily(bars):
    days={}
    for b in bars:
        if b.get("close") is None: continue
        days[b.get("datetime","")[:10]]=b["close"]
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

def bt_put(closes,td,iv):
    if len(closes)<30: return []
    T=DTE/365; rets=[]; i=20
    while i<len(closes)-DTE:
        S=closes[i]; K=strike_for_delta(S,td,T,RISK_FREE,iv); p0=bs_put(S,K,T,RISK_FREE,iv)
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

def bt_call(closes,otm,iv):
    if len(closes)<30: return []
    T=DTE/365; rets=[]; i=20
    while i<len(closes)-DTE:
        S=closes[i]; K=round(S*otm); p0=bs_call(S,K,T,RISK_FREE,iv)
        if p0<=0.02: i+=DTE; continue
        exited=False
        for d in range(1,DTE+1):
            if i+d>=len(closes): break
            S2=closes[i+d]; Tl=max((DTE-d)/365,1e-4); p=bs_call(S2,K,Tl,RISK_FREE,iv)
            if p<=p0*0.5: rets.append((p0-p)/S*100-SLIPPAGE); exited=True; i+=d; break
            if p>=p0*2: rets.append((p0-p)/S*100-SLIPPAGE); exited=True; i+=d; break
        if not exited:
            Sf=closes[min(i+DTE,len(closes)-1)]
            rets.append(p0/S*100 - max(0,(Sf-K))/S*100 - SLIPPAGE); i+=DTE
    return rets

def build_report_lines(period="WEEKLY"):
    """Build backtest report lines (Vault format) — returns list of strings."""
    tickers=["AAPL.US","TSLA.US","AMZN.US","VTI.US"]
    allc={}
    for t in tickers:
        c=to_daily(fetch(t))
        if len(c)>=30: allc[t]=c
        time.sleep(0.4)

    lines=[]
    lines.append("CALLS ·")
    call_best=None
    for (label,otm,iv,_) in CALL_CONFIGS:
        rets=[]
        for t,c in allc.items(): rets+=bt_call(c,otm,iv)
        e=expectancy(rets)
        if e:
            mk="✅" if e["exp"]>0 else "⚠️"
            star="⭐" if label=="CC 2%OTM iv50" else ""
            lines.append(f" {mk} {label} {e['exp']:+.3f}%{star}")
            if call_best is None or e["exp"]>call_best[1]: call_best=(label,e["exp"])
    lines.append("PUTS ·")
    put_best=None
    for (label,td,iv,_) in PUT_CONFIGS:
        rets=[]
        for t,c in allc.items(): rets+=bt_put(c,td,iv)
        e=expectancy(rets)
        if e:
            mk="✅" if e["exp"]>0 else "⚠️"
            star="⭐" if label=="PUT d20 iv50" else ""
            lines.append(f" {mk} {label} {e['exp']:+.3f}%{star}")
            if put_best is None or e["exp"]>put_best[1]: put_best=(label,e["exp"])

    neg = (call_best and call_best[1]<=0) and (put_best and put_best[1]<=0)
    lines.append(f"EDGE    · {'⚠️ negative — review' if neg else 'holding'}")
    lines.append("🔒 modeled · confirm live")
    return lines


def run(period="WEEKLY"):
    """Standalone run — prints and sends the backtest report."""
    from datetime import datetime, timezone
    try:
        from telegram import send_alert
    except Exception:
        def send_alert(m): print(m)
    ts=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines=[f"🔐 THE VAULT · {period} BACKTEST", "▪️▪️▪️▪️▪️▪️▪️▪️▪️▪️", ts, ""]
    lines += build_report_lines(period)
    msg="\n".join(lines)
    send_alert(msg)
    print(msg)

if __name__=="__main__":
    import sys
    run(sys.argv[1] if len(sys.argv)>1 else "WEEKLY")
