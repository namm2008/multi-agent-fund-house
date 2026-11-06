"""S&P 500 1950-2026 (monthly, Shiller): buy&hold vs 10-month trend filter vs drawdown ladder.
Source of the table in Fund_House_Plan_v1.md section 2. Monthly averages understate daily drawdowns."""
import pandas as pd, numpy as np
d=pd.read_csv('https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv',parse_dates=['Date']).set_index('Date')
d=d[d.index>='1950-01-01']
px=d.SP500
div=d.Dividend.replace(0,np.nan).ffill()
r=px.pct_change()+ (div/12/px).shift(1)
r=r.fillna(0)
cash=0.0 # conservative: cash earns nothing
def stats(eq,name):
    yrs=(eq.index[-1]-eq.index[0]).days/365.25
    cagr=eq.iloc[-1]**(1/yrs)-1
    dd=(eq/eq.cummax()-1).min()
    # worst DD episodes by decade
    vol=eq.pct_change().std()*np.sqrt(12)
    return dict(strategy=name,CAGR=f"{cagr:.1%}",MaxDD=f"{dd:.1%}",Vol=f"{vol:.1%}",
                calmar=round(cagr/abs(dd),2))
bh=(1+r).cumprod()
sma=px.rolling(10).mean()
sig=(px>sma).astype(float).shift(1).fillna(1)  # decide at month end, hold next month
tf=(1+sig*r).cumprod()
# drawdown ladder on top of trend filter; exposure decided by prior-month DD of strategy
def ladder(base_sig, ladder_steps):
    eq=[1.0]; hwm=1.0; exp_hist=[]
    for i in range(len(r)):
        dd=eq[-1]/hwm-1
        cap=1.0
        for thr,c in ladder_steps:
            if dd<=thr: cap=c
        e=min(base_sig.iloc[i],cap)
        exp_hist.append(e)
        eq.append(eq[-1]*(1+e*r.iloc[i])); hwm=max(hwm,eq[-1])
        # reset HWM partially after full de-risk? keep strict
    return pd.Series(eq[1:],index=r.index), pd.Series(exp_hist,index=r.index)
steps=[(-0.08,0.75),(-0.12,0.5),(-0.16,0.25)]
lad,exp=ladder(pd.Series(1.0,index=r.index),steps)
tfl,exp2=ladder(sig,steps)
rows=[stats(bh,'Buy & hold S&P (TR)'),stats(tf,'10-month trend filter'),stats(lad,'Drawdown ladder only'),stats(tfl,'Trend filter + ladder')]
print(pd.DataFrame(rows).to_string(index=False))
print('avg exposure trend+ladder',round(exp2.mean(),2))
# per-crisis drawdowns
for a,b in [('1973','1975'),('1987','1988'),('2000','2003'),('2007','2009'),('2020','2020'),('2022','2022')]:
    def mdd(eq): s=eq[a:b]; return (s/eq[:b].cummax()[a:b]-1).min()
    print(a,b,f"BH {mdd(bh):.1%}  TF {mdd(tf):.1%}  TF+L {mdd(tfl):.1%}")
# rolling 10y
print('whipsaws (switches) per decade:', int(sig.diff().abs().sum()/ (len(sig)/120)))
