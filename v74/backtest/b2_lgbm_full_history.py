#!/usr/bin/env python3
"""
LGBM全历史预测 + NavDD8%长周期验证
=================================
1. 在2020-01~2024-06上训练LGBM
2. 保存模型
3. 对2020-01~2026-04全量数据做预测
4. 用+2xLGBM Fusion20跑NavDD8%分年度回测
"""
import sys, time, json, gc, warnings
warnings.filterwarnings('ignore')
from pathlib import Path
import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor, early_stopping, log_evaluation
from scipy.stats import spearmanr

PROJ = Path(__file__).parent.parent.parent
OUT = PROJ / 'output' / 'v74' / 'multi_factor'
OUT.mkdir(parents=True, exist_ok=True)
FPATH = OUT / 'fusion20_factors_v2.parquet'

FACTORS = [
    'neg_volatility_20','neg_ma_bias','close_to_high','rev_5',
    'neg_pe_ttm','neg_pb','neg_ps_ttm','neg_ln_mv',
    'netprofit_yoy','op_yoy','or_yoy','roe',
    'avg_turnover_20','no_zt_5',
    'alpha_16','alpha_13','alpha_40','alpha_88',
    'main_pct','main_pct_5d',
]

MODEL_PATH = OUT / 'lgbm_model_v1.txt'
PRED_PATH = OUT / 'lgbm_pred_full.parquet'


def load():
    t0 = time.time()
    df = pd.read_parquet(FPATH)
    df['trade_date'] = df['trade_date'].astype(str)
    for c in df.select_dtypes(include='float64').columns:
        df[c] = df[c].astype('float32')
    print(f"  Loaded: {len(df):,} rows, {df['trade_date'].nunique()} days, "
          f"{df.memory_usage(deep=True).sum()/1024**3:.2f}GB, {time.time()-t0:.0f}s", flush=True)
    return df


def train(df):
    print("Training LGBM...", flush=True)
    t0 = time.time()
    train_df = df[(df['trade_date'] >= '20200101') & (df['trade_date'] <= '20240630')].copy()
    g = train_df.groupby('ts_code')
    train_df['_target'] = g['close'].transform(lambda s: (s.shift(-5) / s - 1).rank(pct=True))
    train_df = train_df.dropna(subset=['_target'])
    avail = [f for f in FACTORS if f in train_df.columns]
    for f in avail:
        train_df[f] = train_df[f].fillna(0)

    n_val = len(train_df) // 3
    X = train_df[avail].values.astype('float32')
    y = train_df['_target'].values.astype('float32')

    model = LGBMRegressor(max_depth=6, num_leaves=31, min_data_in_leaf=200,
        learning_rate=0.03, n_estimators=300, feature_fraction=0.8,
        bagging_fraction=0.8, bagging_freq=5, lambda_l1=1.0, lambda_l2=1.0,
        min_gain_to_split=0.5, verbosity=-1, n_jobs=-1, random_state=42)
    model.fit(X[:-n_val], y[:-n_val], eval_set=[(X[-n_val:], y[-n_val:])],
              callbacks=[early_stopping(20), log_evaluation(0)])

    print(f"  Trees: {model.n_estimators_}, Time: {time.time()-t0:.0f}s", flush=True)
    del train_df, X, y
    gc.collect()

    # 保存模型
    model.booster_.save_model(str(MODEL_PATH))
    print(f"  Model saved: {MODEL_PATH}", flush=True)
    return model, avail


def predict_full(model, avail, df):
    """对所有日期预测"""
    print("Predicting all dates...", flush=True)
    t0 = time.time()
    dates = sorted(df['trade_date'].unique())
    result_rows = []
    
    for d in dates:
        m = df['trade_date'] == d
        day = df.loc[m]
        day_avail = day[avail].fillna(0).values.astype('float32')
        scores = model.predict(day_avail)
        for j, idx in enumerate(day.index):
            result_rows.append({
                'ts_code': day.at[idx, 'ts_code'],
                'trade_date': d,
                'lgbm_score': float(scores[j]),
            })
        if len(result_rows) % 500000 == 0:
            print(f"  Predicted {len(result_rows):,}/{len(df):,}", flush=True)

    pred = pd.DataFrame(result_rows)
    pred.to_parquet(PRED_PATH)
    print(f"  Saved: {PRED_PATH} ({len(pred):,} rows), {time.time()-t0:.0f}s", flush=True)
    return pred


def bt_longterm(df, pred, dd=None):
    """分年度回测（+2xLGBM Fusion20 Top2-20d）"""
    df = df.merge(pred, on=['ts_code', 'trade_date'], how='left')
    df['lgbm_score'] = df['lgbm_score'].fillna(0)
    avail = [f for f in FACTORS if f in df.columns]

    periods = [
        ('2020-2026全', '20200101', '20260430', 120),
        ('2020', '20200101', '20201231', 60),
        ('2021', '20210101', '20211231', 60),
        ('2022', '20220101', '20221231', 60),
        ('2023', '20230101', '20231231', 60),
        ('2024', '20240101', '20241231', 60),
        ('2025-2026', '20250101', '20260430', 60),
    ]

    results = []
    for pname, ps, pe, wp in periods:
        sub = df[(df['trade_date'] >= ps) & (df['trade_date'] <= pe)].copy()
        all_d = sorted(sub['trade_date'].unique())
        if len(all_d) < wp + 5:
            results.append({'period': pname, 'ret': 0, 'sharpe': 0, 'dd': 0, 'wr': 0, 'pf': 0, 'n': 0, 'navdd': 0})
            continue
        warmup = all_d[min(wp, len(all_d)-1)]
        rb_set = set(d for d in all_d[::20] if d >= warmup)
        dm = {d:i for i,d in enumerate(all_d)}

        cash = 50000; pos = {}; trs = []; ec = []; peak = 50000; in_prot = False
        factors = avail + ['lgbm_score', 'lgbm_score']

        for rd in all_d:
            if rd < warmup: continue
            td = sub[sub['trade_date'] == rd]
            if td.empty: continue
            rb = rd in rb_set

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
                        trs.append(round((c - p['entry_price']) / p['entry_price'] * 100, 2))
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
                            trs.append(round((c - p['entry_price']) / p['entry_price'] * 100, 2))
                        del pos[sym]

            can_buy = rb and len(pos) < 2
            if dd is not None:
                if in_prot and not rb: can_buy = False
                elif rb: in_prot = False

            if can_buy:
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
        wr_v = np.mean([t for t in trs if t > 0]) if wins else 0
        lr = abs(np.mean([t for t in trs if t <= 0])) if n > wins else 1
        pf = wr_v / lr if lr > 0 else 0
        results.append({
            'period': pname, 'ret': round(tr, 2), 'sharpe': round(shp, 4),
            'dd': round(mdd, 2), 'wr': round(wr, 1), 'pf': round(pf, 2),
            'n': n, 'navdd': sum(1 for t in trs if False),  # can't track from just rets
        })
    return results


def main():
    t0 = time.time()
    print("=" * 60)
    print("LGBM全历史预测 + NavDD8%验证")
    print(time.strftime('%Y-%m-%d %H:%M:%S'))
    print("=" * 60)

    # Step 1: 加载数据
    df = load()
    gc.collect()

    # Step 2: 训练
    model, avail = train(df)
    gc.collect()

    # Step 3: 全历史预测
    if not PRED_PATH.exists():
        pred = predict_full(model, avail, df)
    else:
        pred = pd.read_parquet(PRED_PATH)
        print(f"  Loaded existing predictions: {len(pred):,} rows", flush=True)
    gc.collect()

    # Step 4: IC验证
    df2 = df.merge(pred, on=['ts_code', 'trade_date'], how='left')
    df2['lgbm_score'] = df2['lgbm_score'].fillna(0)
    g = df2.groupby('ts_code')
    df2['_fwd'] = g['close'].transform(lambda s: s.shift(-5) / s - 1)
    v = df2[['lgbm_score', '_fwd']].dropna()
    ic, pv = spearmanr(v['lgbm_score'], v['_fwd'])
    print(f"\n  Full-range IC: {ic:.4f} (p={pv:.4e})", flush=True)

    # Step 5: 分年度回测
    print("\n" + "=" * 95)
    for dd_label, dd_val in [('基线(无保护)', None), ('NavDD8%', 8)]:
        print(f"\n--- {dd_label} ---")
        results = bt_longterm(df, pred, dd=dd_val)
        print(f"{'期段':<12} {'收益%':>8} {'Sharpe':>8} {'回撤%':>7} {'胜率%':>6} {'PF':>6} {'交易':>5}")
        print('-' * 55)
        for r in results:
            print(f"{r['period']:<12} {r['ret']:>+7.2f}% {r['sharpe']:>8.4f} {r['dd']:>6.2f}% {r['wr']:>5.1f}% {r['pf']:>5.2f} {r['n']:>5d}")

    print(f"\nTotal time: {time.time()-t0:.0f}s", flush=True)


if __name__ == '__main__':
    main()
