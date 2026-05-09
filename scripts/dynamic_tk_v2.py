#!/usr/bin/env python3
"""DTK10：波动率驱动动态仓位（Dynamic TK v2，HD=10）"""
import os, time, gc, warnings
import numpy as np, pandas as pd
import pyarrow.parquet as pq
from scipy.stats import spearmanr
from datetime import datetime
warnings.filterwarnings("ignore")

import xgboost as xgb
print(f"XGBoost {xgb.__version__} | CUDA test...", end="")
m=xgb.XGBRegressor(device='cuda',n_estimators=3,verbose=0)
m.fit(np.random.randn(100,5).astype('float32'), np.random.randn(100).astype('float32'))
print("OK")

LOG = rf"C:\Users\Administrator\WorkBuddy\Claw\dynamic_tk_v2_{datetime.now():%H%M%S}.log"
def log(s):
    with open(LOG,'a',encoding='utf-8') as f: f.write(s+'\n'); f.flush()
    print(s,flush=True)

log(f"Dynamic TK v2 Optimization -- {datetime.now()}")

K31 = ['avg_turnover_20','amihud_illiq_20d','neg_pb_cs','ep_cs','overnight_ret','gap_up_ma_bias','holder_num_chg','main_pct_5d','intraday_ret','jump_vol_ratio','main_pct_5d_sq','surge_efficiency','skewness_20d','margin_balance_growth_5d','vol_breakout','momentum_12m','amount_surge','volume_momentum','momentum_3m','momentum_1m','downside_vol_20d','intraday_volatility_5','margin_buy_ratio','follow_up','neg_debt_ratio','burst_pattern','coil_amplitude','kurtosis_20d',
'netprofit_yoy','gross_margin','asset_turn']

SDIR = r"C:\Users\Administrator\WorkBuddy\Claw\grid_scores"

# 价格数据库
log("Loading price data from master...")
raw = pq.read_table(r"C:\ML_STATION\LGBM_ML_Package\data\fusion20_master.parquet",
    columns=['ts_code','trade_date','close','open','pct_chg']).to_pandas()
raw['trade_date']=raw['trade_date'].astype(str)
raw['pct_chg']=raw['pct_chg'].fillna(0.0)
pdb={}
for (d,g) in raw.groupby('trade_date'):
    pdb.setdefault(d,{})
    for _,r in g.iterrows():
        pdb[d][r['ts_code']]={'o':r['open'],'c':r['close'],'p':r['pct_chg']}
del raw; gc.collect()
log(f"  Price DB: {len(pdb)} dates")

all_dates = sorted(pdb.keys())
dm = {d:i for i,d in enumerate(all_dates)}

# 加载评分
log("Loading scores...")
S = pd.concat([pd.read_parquet(os.path.join(SDIR,f)) for f in os.listdir(SDIR) if f.endswith('.parquet')]).sort_values(['trade_date','score'],ascending=[True,False])
td = sorted(set(S['trade_date'].unique()) & set(pdb.keys()))
td = [d for d in td if dm.get(d,0) < len(all_dates)-1]

def run_backtest(TK_fixed, HD, dynamic_tk=False, vol_window=20, vol_method='percentile', vol_threshold=0.5, min_hold_days=10):
    """
    优化版动态TK
    TK_fixed: 固定TK值
    dynamic_tk: 是否动态调整
    vol_window: 波动率计算窗口
    vol_method: 'percentile'（分位数）或 'fixed'（固定阈值）
    vol_threshold: 分位数阈值（0.5=中位数）或固定阈值（如0.03）
    min_hold_days: 最小持仓天数（防止频繁切换TK）
    """
    C=50000
    CR,SR,SL=0.001,0.0005,0.002
    
    lu = lambda c,d: pdb.get(d,{}).get(c,{}).get('p',0) >= 9.0
    ld = lambda c,d: pdb.get(d,{}).get(c,{}).get('p',0) <= -9.0
    
    cash = C
    bs = []
    dlog = []
    cn = 0
    sc_val = 1.0
    current_tk = TK_fixed
    last_tk_change = 0  # 上次TK变更的交易日的index
    
    for di,today in enumerate(td):
        ti = dm[today]
        t1 = all_dates[ti+1] if ti+1 < len(all_dates) else None
        
        # 计算动态TK
        if dynamic_tk and di >= vol_window and di - last_tk_change >= min_hold_days:
            # 计算过去vol_window天的日收益波动率
            recent_nav = [dlog[i]['nav'] for i in range(max(0,di-vol_window), di)]
            if len(recent_nav) >= 10:
                recent_ret = [np.log(recent_nav[i]/recent_nav[i-1]) for i in range(1,len(recent_nav))]
                vol = np.std(recent_ret)
                
                if vol_method == 'percentile':
                    # 用过去所有波动率的分位数
                    all_vol = []
                    for j in range(vol_window, di):
                        nav_j = [dlog[k]['nav'] for k in range(max(0,j-vol_window), j)]
                        if len(nav_j) >= 10:
                            ret_j = [np.log(nav_j[k]/nav_j[k-1]) for k in range(1,len(nav_j))]
                            all_vol.append(np.std(ret_j))
                    if len(all_vol) > 0:
                        pct = (np.array(all_vol) <= vol).sum() / len(all_vol)
                        if pct > vol_threshold:
                            new_tk = 3
                        elif pct > vol_threshold - 0.2:
                            new_tk = 4
                        else:
                            new_tk = 5
                        if new_tk != current_tk:
                            current_tk = new_tk
                            last_tk_change = di
                else:  # fixed threshold
                    if vol > vol_threshold:
                        new_tk = 3
                    elif vol > vol_threshold * 0.7:
                        new_tk = 4
                    else:
                        new_tk = 5
                    if new_tk != current_tk:
                        current_tk = new_tk
                        last_tk_change = di
        
        TK = current_tk if dynamic_tk else TK_fixed
        
        # === 卖出逻辑 ===
        sp = 0.0
        kp = []
        for b in bs:
            if b['sd'] == today:
                ok = True
                for c in b['cs']:
                    if ld(c, today):
                        ok = False
                        break
                if not ok:
                    b['sd'] = td[min(di+1,len(td)-1)] if di+1 < len(td) else None
                    kp.append(b)
                    continue
                for i,c in enumerate(b['cs']):
                    info = pdb.get(today,{}).get(c)
                    if info:
                        sp += b['al']/len(b['cs']) * (1 + (info['c']*(1-SL)/b['bp'][i]-1)) * (1-CR-SR)
                    else:
                        sp += b['al']/len(b['cs'])
            else:
                kp.append(b)
        bs = kp
        cash += sp
        
        # 计算市值
        m2m = 0.0
        for b in bs:
            for i,c in enumerate(b['cs']):
                info = pdb.get(today,{}).get(c)
                if info:
                    m2m += b['al']/len(b['cs']) * (1 + (info['c']/b['bp'][i]-1))
                else:
                    m2m += b['al']/len(b['cs'])
        nav = cash + m2m
        
        # CL2/3 风控
        if di > 0 and dlog:
            cn = cn + 1 if nav/dlog[-1]['nav'] - 1 < -0.001 else 0
            if cn >= 3:
                for b in bs:
                    cash += b['al']
                bs = []
                m2m = 0.0
                sc_val = 0.0
            elif cn >= 2:
                sc_val = 0.5
            else:
                sc_val = 1.0
        
        # 买入逻辑
        if len(bs) < TK and sc_val > 0 and t1:
            ds = S[S['trade_date']==today]
            if len(ds) >= TK:
                AP = cash / TK
                tg = AP * TK * sc_val
                al = min(tg, cash*0.95)
                if al >= AP * 0.5:
                    top = ds.head(TK*5)
                    cs, bp = [], []
                    for _,r in top.iterrows():
                        if len(cs) >= TK:
                            break
                        c = r['ts_code']
                        if lu(c, t1):
                            continue
                        info = pdb.get(t1,{}).get(c)
                        if not info:
                            continue
                        p = info['o'] * (1+SL)
                        lots = max(1, int(al/TK/p/100))
                        if lots * 100 * p * (1+CR) <= al/TK * 1.2:
                            cs.append(c)
                            bp.append(p)
                    if len(cs) >= 1:
                        ba = al * len(cs) / TK
                        cash -= ba
                        sd = td[di+HD] if di+HD < len(td) else None
                        bs.append({'sd':sd,'cs':cs,'bp':bp,'al':ba})
        
        # 记录净值
        pm2m = 0.0
        for b in bs:
            for i,c in enumerate(b['cs']):
                info = pdb.get(today,{}).get(c)
                if info:
                    pm2m += b['al']/len(b['cs']) * (1 + (info['c']/b['bp'][i]-1))
                else:
                    pm2m += b['al']/len(b['cs'])
        nav = cash + pm2m
        dlog.append({'d':today,'nav':nav,'sc_val':sc_val,'TK':TK})
    
    # ========== 计算结果 ==========
    ldf = pd.DataFrame(dlog)
    ldf['q'] = pd.to_datetime(ldf['d']).dt.to_period('Q')
    ldf['m'] = ldf['d'].str[:6]
    ldf['y'] = pd.to_datetime(ldf['d']).dt.year
    
    ret = ldf['nav'].pct_change().dropna().values
    fin = ldf['nav'].iloc[-1]
    yrs = len(ret) / 250
    ca = float((fin/C)**(1/yrs)-1)
    
    no = ret[::5]
    sh = float(np.sqrt(252/5) * np.mean(no) / max(np.std(no),1e-10))
    
    md = float((ldf['nav'].values / np.maximum.accumulate(ldf['nav'].values) - 1).min())
    
    # ========== 详细月度数据 ==========
    mode = f"Dynamic_TK_v2_{vol_method}" if dynamic_tk else f"Fixed_TK{TK_fixed}"
    log(f"\n{'='*70}")
    log(f"  {mode}, HD={HD}, threshold={vol_threshold}, min_hold={min_hold_days}d")
    log(f"  {C:,.0f} -> {fin:,.0f} | CAGR={ca*100:.1f}%  Sharpe(NO)={sh:.2f}  MaxDD={md*100:.1f}%")
    
    log(f"\n  【月度详细数据】")
    log(f"  {'月份':<8} {'月末净值':>12} {'月收益':>8} {'TK':>4} {'CL2/3':>6} {'峰值':>12} {'回撤':>7}")
    log(f"  {'-'*70}")
    
    monthly_rows = []
    for m,grp in ldf.groupby('m'):
        mr = grp['nav'].iloc[-1]/grp['nav'].iloc[0]-1
        cd2 = (grp['sc_val']<1.0).sum()
        cd3 = (grp['sc_val']==0.0).sum()
        avg_tk = grp['TK'].mean()
        month_peak = grp['nav'].max()
        month_dd = (grp['nav'].values / np.maximum.accumulate(grp['nav'].values) - 1).min() * 100
        log(f"  {m:<8} {grp['nav'].iloc[-1]:>12,.0f} {mr*100:>7.1f}% TK={avg_tk:.1f} CL2={cd2} CL3={cd3} {month_peak:>12,.0f} {month_dd:>6.1f}%")
        monthly_rows.append({
            'month': m,
            'nav_end': grp['nav'].iloc[-1],
            'ret_monthly': mr,
            'avg_tk': avg_tk,
            'cl2_count': cd2,
            'cl3_count': cd3,
            'peak': month_peak,
            'maxdd_monthly': month_dd/100
        })
    
    # 季度数据
    log(f"\n  【季度数据】")
    for q,grp in ldf.groupby('q'):
        qr = grp['nav'].iloc[-1]/grp['nav'].iloc[0]-1
        qn = grp['nav'].iloc[-1]
        qd = float((grp['nav'].values/np.maximum.accumulate(grp['nav'].values)-1).min())*100
        log(f"  {str(q):<8} {qn:>12,.0f} {qr*100:>7.1f}% {qd:>6.1f}%")
    
    # 年度数据
    log(f"\n  【年度数据】")
    for yr,grp in ldf.groupby('y'):
        yret = grp['nav'].iloc[-1]/grp['nav'].iloc[0]-1
        yc = float((grp['nav'].iloc[-1]/grp['nav'].iloc[0])**(250/max(len(grp),1))-1)
        log(f"  {yr:<8} {grp['nav'].iloc[-1]:>12,.0f} {yret*100:>7.1f}% {yc*100:>7.1f}%")
    
    # 保存月度数据
    mode_str = mode.replace('/','_')
    csv_path = rf"C:\Users\Administrator\WorkBuddy\Claw\dynamic_tk_v2_{mode_str}_monthly.csv"
    month_df = pd.DataFrame(monthly_rows)
    month_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    log(f"\n  月度数据已保存: {csv_path}")
    
    return ca, sh, md, fin, month_df

# ========== 运行对比 ==========
log("\n" + "="*70)
log("Strategy A: TK=5 (Fixed), HD=10 [Baseline]")
log("="*70)
A = run_backtest(TK_fixed=5, HD=10, dynamic_tk=False)

log("\n" + "="*70)
log("Strategy B: Dynamic TK v2 (percentile), threshold=0.5, min_hold=10d")
log("="*70)
B = run_backtest(TK_fixed=5, HD=10, dynamic_tk=True, vol_window=20, vol_method='percentile', vol_threshold=0.5, min_hold_days=10)

log("\n" + "="*70)
log("Strategy C: Dynamic TK v2 (percentile), threshold=0.6, min_hold=10d")
log("="*70)
C = run_backtest(TK_fixed=5, HD=10, dynamic_tk=True, vol_window=20, vol_method='percentile', vol_threshold=0.6, min_hold_days=10)

log("\n" + "="*70)
log("Strategy D: Dynamic TK v2 (fixed), threshold=0.03, min_hold=10d")
log("="*70)
D = run_backtest(TK_fixed=5, HD=10, dynamic_tk=True, vol_window=20, vol_method='fixed', vol_threshold=0.03, min_hold_days=10)

# 对比
log("\n" + "="*70)
log("COMPARISON")
log("="*70)
log(f"Strategy A (Fixed TK=5):    CAGR={A[0]*100:.1f}%  Sharpe={A[1]:.2f}  MaxDD={A[2]*100:.1f}%  Final={A[3]:,.0f}")
log(f"Strategy B (Pctl 0.5):      CAGR={B[0]*100:.1f}%  Sharpe={B[1]:.2f}  MaxDD={B[2]*100:.1f}%  Final={B[3]:,.0f}")
log(f"Strategy C (Pctl 0.6):      CAGR={C[0]*100:.1f}%  Sharpe={C[1]:.2f}  MaxDD={C[2]*100:.1f}%  Final={C[3]:,.0f}")
log(f"Strategy D (Fixed 0.03):     CAGR={D[0]*100:.1f}%  Sharpe={D[1]:.2f}  MaxDD={D[2]*100:.1f}%  Final={D[3]:,.0f}")

log(f"\nTotal: {time.time()-t0:.0f}s")
log(f"Log: {LOG}")
