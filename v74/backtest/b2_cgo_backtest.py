#!/usr/bin/env python3
"""
CGO因子加入Fusion20回测验证
===========================
对比: Fusion20(+2xLGBM Top2-20d NavDD8%) vs Fusion20+CGO

CGO = (close - 252日VWAP) / close
"""
import pandas as pd, numpy as np, sqlite3, gc, warnings
warnings.filterwarnings('ignore')
from pathlib import Path
from scipy.stats import spearmanr

PROJ = Path('/Users/chenshi/WorkBuddy/Claw')
OUT = PROJ / 'output' / 'v74' / 'multi_factor'

# ===== 1. 计算CGO因子 =====
print('计算CGO因子...', flush=True)
conn = sqlite3.connect(str(PROJ / 'data/db/market.db'))

# 排除股
excl = set(r[0] for r in conn.execute("SELECT ts_code FROM stocks WHERE name LIKE '%ST%' OR ts_code LIKE '30%' OR ts_code LIKE '688%' OR ts_code LIKE '8%'").fetchall())

stk = pd.read_sql('SELECT ts_code, trade_date, close, amount FROM daily_kline WHERE trade_date >= "20190101" ORDER BY ts_code, trade_date', conn)
conn.close()
stk['trade_date'] = stk['trade_date'].astype(str)
stk = stk[~stk['ts_code'].isin(excl)]
print(f'  日线: {len(stk):,}行', flush=True)

g = stk.groupby('ts_code')
stk['vwap_252'] = g.apply(lambda x: (x['amount'] * x['close']).rolling(252).sum() / x['amount'].rolling(252).sum().clip(lower=1)).reset_index(level=0, drop=True)
stk['cgo'] = (stk['close'] - stk['vwap_252']) / stk['close'].clip(lower=1)
cgo_map = stk[['ts_code', 'trade_date', 'cgo']].copy()
del stk; gc.collect()
print(f'  CGO已计算: {len(cgo_map):,}行', flush=True)

# ===== 2. 加载回测数据 =====
print('加载回测数据...', flush=True)
FACTORS = ['neg_volatility_20','neg_ma_bias','close_to_high','rev_5','neg_pe_ttm','neg_pb','neg_ps_ttm','neg_ln_mv','netprofit_yoy','op_yoy','or_yoy','roe','avg_turnover_20','no_zt_5','alpha_16','alpha_13','alpha_40','alpha_88','main_pct','main_pct_5d']

df = pd.read_parquet(OUT/'fusion20_factors_v2.parquet', columns=['ts_code','trade_date','close','amount']+FACTORS)
df['trade_date'] = df['trade_date'].astype(str)
for c in df.select_dtypes(include='float64').columns: df[c] = df[c].astype('float32')

pred = pd.read_parquet(OUT/'lgbm_pred_lgbm_5d.parquet')
pred['trade_date'] = pred['trade_date'].astype(str); pred = pred.rename(columns={'lgbm_5d':'lgbm_score'})
df = df.merge(pred, on=['ts_code','trade_date'], how='left'); df['lgbm_score'] = df['lgbm_score'].fillna(0)

# 合并CGO
cgo_map['trade_date'] = cgo_map['trade_date'].astype(str)
cgo_map['cgo'] = cgo_map['cgo'].astype('float32')
df = df.merge(cgo_map, on=['ts_code','trade_date'], how='left')
df['cgo'] = df['cgo'].fillna(0)
del cgo_map; gc.collect()

# 只测2024-09以后（LGBM可用期）
test = df[(df['trade_date'] >= '20240901') & (df['trade_date'] <= '20260430')].copy()
del df; gc.collect()

# 验证CGO IC
g = test.groupby('ts_code')
test['fwd_5'] = g['close'].transform(lambda s: s.shift(-5) / s - 1)
test = test.dropna(subset=['fwd_5'])
dates = sorted(test['trade_date'].unique())

ics = []
for d in dates[::20]:
    day = test[test['trade_date'] == d]
    if len(day) < 100: continue
    top = day.nlargest(500, 'amount')
    v = top[['cgo', 'fwd_5']].dropna()
    if len(v) > 30:
        ic, _ = spearmanr(v['cgo'], v['fwd_5'])
        ics.append(ic)
print(f'\nCGO IC in top500: {np.mean(ics):.4f} (n={len(ics)}天)', flush=True)

# ===== 3. 回测比较 =====
avail = FACTORS + ['lgbm_score']
all_d = sorted(test['trade_date'].unique())
warmup = all_d[min(60, len(all_d)-1)]
rb = [d for d in all_d[::20] if d >= warmup]
dm = {d:i for i,d in enumerate(all_d)}

def bt(use_cgo=False, navdd=8):
    """Top2-20d +2xLGBM, 可选NavDD8% + 可选CGO"""
    cash=50000; pos={}; trs=[]; ec=[]; peak=50000; in_prot=False
    factors = avail + ['lgbm_score']
    if use_cgo:
        factors = factors + ['neg_cgo']  # CGO取负（CGO越高后续越差，取负值做因子）
    
    for rd in rb:
        td = test[test['trade_date'] == rd]
        if td.empty: continue
        
        # NavDD检查（每日检查）
        nav_before = cash
        for sym, p in pos.items():
            row = td[td['ts_code'] == sym]
            if not row.empty:
                nav_before += p['shares'] * float(row.iloc[0]['close'])
        
        if nav_before > peak: peak = nav_before
        cur_dd = (peak - nav_before) / peak * 100
        
        if cur_dd >= navdd and pos:
            for sym in list(pos.keys()):
                p = pos[sym]; row = td[td['ts_code'] == sym]
                if not row.empty:
                    c = float(row.iloc[0]['close'])
                    ret = (c - p['entry_price']) / p['entry_price'] * 100
                    cash += p['shares'] * c * (1 - 0.00135)
                    trs.append(ret)
                del pos[sym]
            in_prot = True
        
        # 卖出到期
        if pos:
            for sym in list(pos.keys()):
                p = pos[sym]
                if dm[rd] - dm.get(p['entry_date'], 0) >= 20:
                    row = td[td['ts_code'] == sym]
                    if not row.empty:
                        c = float(row.iloc[0]['close'])
                        ret = (c - p['entry_price']) / p['entry_price'] * 100
                        cash += p['shares'] * c * (1 - 0.00135)
                        trs.append(ret)
                    del pos[sym]
        
        can_buy = len(pos) < 2 and (not in_prot or dm[rd])
        if rb and can_buy:
            vals = td[factors].fillna(0)
            for c2 in factors:
                s = vals[c2].std()
                if isinstance(s, (np.ndarray, pd.Series)): s = float(s.max())
                vals[c2] = (vals[c2] - vals[c2].mean()) / s if s > 1e-8 else 0
            sc = vals.mean(axis=1); sc.index = td['ts_code']
            sc = sc.sort_values(ascending=False)
            cand = sc[~sc.index.isin(pos.keys())]
            top_set = set(td.nlargest(500, 'amount')['ts_code'])
            cand = cand[cand.index.isin(top_set)]
            sel = cand.head(2).index.tolist()
            slots = 2 - len(pos)
            if slots > 0 and sel:
                ps = cash / slots
                for sym in sel[:slots]:
                    row = td[td['ts_code'] == sym]
                    if row.empty: continue
                    cp = float(row.iloc[0]['close'])
                    if cp <= 0: continue
                    sh = int(ps / cp / 100) * 100
                    if sh < 100: continue
                    cost = sh * cp * (1 + 0.00035)
                    if cost <= cash:
                        cash -= cost; pos[sym] = {'entry_date': rd, 'entry_price': cp, 'shares': sh}
        
        nav = cash
        for sym, p in pos.items():
            row = td[td['ts_code'] == sym]
            if not row.empty:
                nav += p['shares'] * float(row.iloc[0]['close'])
        
        # 调仓日解除保护
        if in_prot and rb:
            in_prot = False
        ec.append(nav)
    
    tr = (ec[-1] - 50000) / 50000 * 100 if ec else 0
    rets = [(ec[i]-ec[i-1])/ec[i-1] for i in range(1, len(ec)) if ec[i-1] > 0]
    shp = 0
    if len(rets) > 1 and np.std(rets, ddof=1) > 0:
        shp = (np.mean(rets) * 252/20 - 0.02) / (np.std(rets, ddof=1) * np.sqrt(252/20))
    p2 = 50000; mdd = 0
    for v in ec:
        if v > p2: p2 = v; mdd = max(mdd, (p2 - v) / p2 * 100)
    wins = sum(1 for t in trs if t > 0); n = len(trs)
    wr = wins / n * 100 if n else 0
    wr_v = np.mean([t for t in trs if t > 0]) if wins else 0
    lr = abs(np.mean([t for t in trs if t <= 0])) if n > wins else 1
    return {'ret': round(tr,2), 'sharpe': round(shp,4), 'dd': round(mdd,2), 'wr': round(wr,1), 'pf': round(wr_v/lr,2) if lr > 0 else 0, 'n': n}

print('\n' + '='*70)
print('B2回测: +2xLGBM Top2-20d + NavDD8%')
print('对比: Fusion20 vs Fusion20+CGO')
print('='*70)
print(f"{'方案':<40} {'收益%':>8} {'Sharpe':>8} {'回撤%':>7} {'胜率%':>6} {'PF':>6} {'交易':>5}")
print('-' * 80)

# 4种组合
tests = [
    ('Fusion20(无保护)', False, None),
    ('Fusion20+NavDD8%', False, 8),
    ('Fusion20+CGO(无保护)', True, None),
    ('Fusion20+CGO+NavDD8%', True, 8),
]

for label, cgo, nd in tests:
    r = bt(use_cgo=cgo, navdd=nd) if nd else bt(use_cgo=cgo, navdd=None)
    print(f'{label:<40} {r["ret"]:>+7.2f}% {r["sharpe"]:>8.4f} {r["dd"]:>6.2f}% {r["wr"]:>5.1f}% {r["pf"]:>5.2f} {r["n"]:>5d}')
