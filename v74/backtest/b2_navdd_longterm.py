#!/usr/bin/env python3
"""
B2 NavDD8% 长周期验证 — 分年度回测
"""
import pandas as pd, numpy as np, gc, warnings
warnings.filterwarnings('ignore')
from pathlib import Path

OUT = Path('output/v74/multi_factor')
FACTORS = ['neg_volatility_20','neg_ma_bias','close_to_high','rev_5','neg_pe_ttm','neg_pb','neg_ps_ttm','neg_ln_mv','netprofit_yoy','op_yoy','or_yoy','roe','avg_turnover_20','no_zt_5','alpha_16','alpha_13','alpha_40','alpha_88','main_pct','main_pct_5d']

print('loading 2020-2026 data (Fusion20, no LGBM)...', flush=True)
df = pd.read_parquet(OUT/'fusion20_factors_v2.parquet')
df['trade_date'] = df['trade_date'].astype(str)
for c in df.select_dtypes(include='float64').columns:
    df[c] = df[c].astype('float32')
avail = [f for f in FACTORS if f in df.columns]

def run_bt(dd, start, end, warmup_days):
    sub = df[(df['trade_date'] >= start) & (df['trade_date'] <= end)].copy()
    all_d = sorted(sub['trade_date'].unique())
    if len(all_d) < warmup_days + 5:
        return None
    warmup = all_d[min(warmup_days, len(all_d)-1)]
    rb_dates = set(d for d in all_d[::20] if d >= warmup)
    dm = {d:i for i,d in enumerate(all_d)}

    cash = 50000; pos = {}; trs = []; ec = []; peak = 50000; in_prot = False

    for rd in all_d:
        if rd < warmup: continue
        td = sub[sub['trade_date'] == rd]
        if td.empty: continue
        rb = rd in rb_dates

        nav = cash
        for sym, p in pos.items():
            row = td[td['ts_code'] == sym]
            if not row.empty:
                nav += p['shares'] * float(row.iloc[0]['close'])

        if nav > peak: peak = nav
        cur_dd = (peak - nav) / peak * 100

        if dd is not None and cur_dd >= dd and pos:
            for sym in list(pos.keys()):
                p = pos[sym]; row = td[td['ts_code'] == sym]
                if not row.empty:
                    c = float(row.iloc[0]['close'])
                    cash += p['shares'] * c * (1 - 0.00135)
                    trs.append(round((c-p['entry_price'])/p['entry_price']*100, 2))
                del pos[sym]
            in_prot = True

        if rb and pos:
            for sym in list(pos.keys()):
                p = pos[sym]
                if dm[rd] - dm.get(p['entry_date'], 0) >= 20:
                    row = td[td['ts_code'] == sym]
                    if not row.empty:
                        c = float(row.iloc[0]['close'])
                        cash += p['shares'] * c * (1 - 0.00135)
                        trs.append(round((c-p['entry_price'])/p['entry_price']*100, 2))
                    del pos[sym]

        can_buy = rb and len(pos) < 2
        if dd is not None:
            if in_prot and not rb: can_buy = False
            elif rb: in_prot = False

        if can_buy:
            vals = td[avail].fillna(0)
            for c2 in avail:
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
                        cash -= cost
                        pos[sym] = {'entry_date': rd, 'entry_price': cp, 'shares': sh}

        nav = cash
        for sym, p in pos.items():
            row = td[td['ts_code'] == sym]
            if not row.empty:
                nav += p['shares'] * float(row.iloc[0]['close'])
        ec.append(nav)

    tr = (ec[-1] - 50000) / 50000 * 100 if ec else 0
    rets = [(ec[i]-ec[i-1])/ec[i-1] for i in range(1, len(ec)) if ec[i-1] > 0]
    shp = 0
    if len(rets) > 1 and np.std(rets, ddof=1) > 0:
        shp = (np.mean(rets) * 252 - 0.02) / (np.std(rets, ddof=1) * np.sqrt(252))
    p2 = 50000; mdd = 0
    for v in ec:
        if v > p2: p2 = v
        mdd = max(mdd, (p2 - v) / p2 * 100)
    wins = sum(1 for t in trs if t > 0)
    n = len(trs)
    wr = wins / n * 100 if n else 0
    win_r = np.mean([t for t in trs if t > 0]) if wins else 0
    loss_r = abs(np.mean([t for t in trs if t <= 0])) if n > wins else 1
    pf = win_r / loss_r if loss_r > 0 else 0
    return {'ret': round(tr,2), 'sharpe': round(shp,4), 'dd': round(mdd,2), 'wr': round(wr,1), 'pf': round(pf,2), 'n': n}

print('=' * 90)
print(f"{'期段':<12} {'方案':<14} {'收益%':>8} {'Sharpe':>8} {'回撤%':>7} {'胜率%':>6} {'PF':>6} {'交易':>5}")
print('=' * 90)

periods = [
    ('2020-2026', '20200101', '20260430', 120),
    ('2020', '20200101', '20201231', 60),
    ('2021', '20210101', '20211231', 60),
    ('2022', '20220101', '20221231', 60),
    ('2023', '20230101', '20231231', 60),
    ('2024', '20240101', '20241231', 60),
    ('2025-2026', '20250101', '20260430', 60),
]

for pname, ps, pe, wp in periods:
    r_base = run_bt(None, ps, pe, wp)
    r_nav = run_bt(8, ps, pe, wp)
    if r_base is None:
        continue
    print(f"{pname:<12} {'基线(无保护)':<14} {r_base['ret']:>+7.2f}% {r_base['sharpe']:>8.4f} {r_base['dd']:>6.2f}% {r_base['wr']:>5.1f}% {r_base['pf']:>5.2f} {r_base['n']:>5d}")
    print(f"{'':<12} {'NavDD8%':<14} {r_nav['ret']:>+7.2f}% {r_nav['sharpe']:>8.4f} {r_nav['dd']:>6.2f}% {r_nav['wr']:>5.1f}% {r_nav['pf']:>5.2f} {r_nav['n']:>5d}")
    dd_delta = r_nav['dd'] - r_base['dd']
    ret_delta = r_nav['ret'] - r_base['ret']
    print(f"{'':<12} {'改善':<14} {'':>8} {'':>8} {dd_delta:+.1f}pp(回撤) {ret_delta:+.2f}pp(收益)")
    print()
