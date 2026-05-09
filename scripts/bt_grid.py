#!/usr/bin/env python3
"""
LGBM 双模型集成 + 持仓网格搜索
================================
1. 补训 seed=123 模型分数（GPU WSL）
2. 网格回测：单/双模 × Top2/3/5 × 5d/10d
3. 真实资金约束：¥50k, 0.65%成本, CL风控
"""
import subprocess, os, json, time, gc, warnings
from datetime import datetime
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

LOG = r"C:\Users\Administrator\WorkBuddy\Claw\bt_grid_" + datetime.now().strftime('%H%M%S') + ".log"
SCORES_DIR = r"C:\Users\Administrator\WorkBuddy\Claw\bt_scores"
SCORES_DIR_WSL = "/mnt/c/Users/Administrator/WorkBuddy/Claw/bt_scores"
PREPROC = r"C:\ML_STATION\LGBM_ML_Package\data\bt_elite_preproc.parquet"
PREPROC_WSL = "/mnt/c/ML_STATION/LGBM_ML_Package/data/bt_elite_preproc.parquet"

os.makedirs(SCORES_DIR, exist_ok=True)

def log(s):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(s + '\n'); f.flush()
    print(s, flush=True)

def to_wsl(p):
    p = p.replace('\\', '/')
    if len(p) > 1 and p[1] == ':':
        return '/mnt/' + p[0].lower() + p[2:]
    return p

# ====== 参数 ======
CAPITAL = 50000.0
COMM_RATE, STAMP_RATE, SLIPPAGE = 0.001, 0.0005, 0.002
BUY_COST = COMM_RATE + SLIPPAGE
SELL_COST = COMM_RATE + STAMP_RATE + SLIPPAGE
EMBARGO, FORWARD, STEP, TRAIN, VAL = 5, 5, 60, 480, 60

ELITE_FACTORS = [
    'adx_14','pv_corr_20','coil_amplitude','overnight_ret',
    'gap_up_ma_bias','follow_up','surge_efficiency','burst_pattern',
    'momentum_1m','vol_breakout','intraday_volatility_5','volume_momentum',
    'amount_surge','margin_buy_ratio','intraday_ret','main_pct_5d',
    'main_pct_5d_sq','skewness_20d','kurtosis_20d','netprofit_yoy',
    'neg_debt_ratio','gross_margin','neg_pb_cs','asset_turn',
    'avg_turnover_20','amihud_illiq_20d','momentum_3m','momentum_12m',
    'downside_vol_20d','ep_cs','jump_vol_ratio','margin_balance_growth_5d',
    'no_zt_5','holder_num_chg',
]

log("="*70)
log(f"LGBM Grid Search: Ensemble + Position Grid")
log(f"Start: {datetime.now()}")
t0 = time.time()

# ====== Step 1: 加载数据 ======
log("Step 1: Loading...")
import pyarrow.parquet as pq
preproc = pq.read_table(PREPROC).to_pandas()
dates_list = sorted(set(preproc['trade_date']))

full_data = r"C:\ML_STATION\LGBM_ML_Package\data\fusion20_master.parquet"
raw = pq.read_table(full_data, columns=['ts_code','trade_date','close']).to_pandas()
raw['trade_date'] = raw['trade_date'].astype(str)
price_lookup = {}
for _, row in raw.iterrows():
    price_lookup.setdefault(row['trade_date'], {})[row['ts_code']] = row['close']
del raw; gc.collect()

# ====== Step 2: 创建窗口 ======
n_dates = len(dates_list)
windows = []
for i in range(TRAIN + EMBARGO, n_dates - VAL + 1, STEP):
    te = i - 1 - EMBARGO
    ts = max(0, te - TRAIN + 1)
    vs, ve = i, min(i + VAL - 1, n_dates - 1)
    if (te - ts) < TRAIN * 0.5: continue
    windows.append({'id': len(windows), 'ts': dates_list[ts], 'te': dates_list[te],
                    'vs': dates_list[vs], 've': dates_list[ve]})
log(f"  {len(windows)} windows")

# ====== Step 3: 训练 seed=123 模型 ======
log(f"\nStep 3: Training seed=123 model (GPU)...")
ff = repr(ELITE_FACTORS)

for w in windows:
    wid = w['id']
    sf = os.path.join(SCORES_DIR, f"seed123_{wid}.parquet")
    if os.path.exists(sf):
        log(f"  Win {wid+1}/{len(windows)}: seed123 already done, skip")
        continue

    log(f"\n[{datetime.now():%H:%M:%S}] seed123 Win {wid+1}/{len(windows)}: "
        f"train {w['ts']}~{w['te']} val {w['vs']}~{w['ve']}")

    wsf = to_wsl(sf)
    wsl_tmp = f"/tmp/seed123_{wid}.parquet"

    scr = (
        'import gc, subprocess, warnings; warnings.filterwarnings("ignore"); '
        'import numpy as np, pandas as pd; '
        'from scipy.stats import spearmanr; '
        'from lightgbm import LGBMRegressor, early_stopping, log_evaluation; '
        f'df = pd.read_parquet("{PREPROC_WSL}"); '
        f'tr = df[df.trade_date.between("{w["ts"]}","{w["te"]}")]; '
        f'vl = df[df.trade_date.between("{w["vs"]}","{w["ve"]}")]; '
        'del df; gc.collect(); '
        f'facs = {ff}; '
        'Xtr = np.nan_to_num(tr[facs].values,nan=0,posinf=0,neginf=0).astype("float32"); '
        'Xvl = np.nan_to_num(vl[facs].values,nan=0,posinf=0,neginf=0).astype("float32"); '
        'm = LGBMRegressor(max_depth=5,num_leaves=23,min_data_in_leaf=500,'
        'learning_rate=0.02,n_estimators=500,feature_fraction=0.6,'
        'bagging_fraction=0.7,bagging_freq=3,lambda_l1=5.0,lambda_l2=5.0,'
        'min_gain_to_split=1.0,verbosity=-1,n_jobs=-1,random_state=123,'
        'device="cuda"); '
        'm.fit(Xtr,tr._target,'
        'eval_set=[(Xvl,vl._target.values.astype("float32"))],'
        'callbacks=[early_stopping(20),log_evaluation(0)]); '
        'pred=m.predict(Xvl); ic,_=spearmanr(pred,vl._target); '
        'sf=vl[["ts_code","trade_date","_fwd_ret","_target"]].copy(); '
        'sf["score"]=pred; '
        f'sf.to_parquet("{wsl_tmp}"); '
        f'subprocess.run(["mv","-f","{wsl_tmp}","{wsf}"]); '
        'print(f"RESULT: IC={ic:.4f} trees={m.n_estimators_} rows={len(sf)}")'
    )

    tw = time.time()
    try:
        proc = subprocess.run(['wsl', 'python3', '-c', scr],
                            capture_output=True, text=True, timeout=300)
        elap = time.time() - tw
        if proc.returncode == 0:
            lines = proc.stdout.strip().split('\n')
            rl = [l for l in lines if l.startswith('RESULT:')]
            log(f"  {rl[0] if rl else 'OK'} {elap:.0f}s")
        else:
            log(f"  FAILED({elap:.0f}s): {proc.stderr.strip()[-200:]}")
            break
    except Exception as e:
        log(f"  ERROR: {e}"); break

# ====== Step 4: 加载所有分数 ======
log(f"\nStep 4: Loading scores...")
scores42, scores123 = [], []
for w in windows:
    s42 = os.path.join(SCORES_DIR, f"scores_{w['id']}.parquet")
    s123 = os.path.join(SCORES_DIR, f"seed123_{w['id']}.parquet")
    if os.path.exists(s42):
        sdf = pd.read_parquet(s42)
        sdf['model'] = 42
        scores42.append(sdf)
    if os.path.exists(s123):
        sdf = pd.read_parquet(s123)
        sdf['model'] = 123
        scores123.append(sdf)

s42_all = pd.concat(scores42, ignore_index=True)
s123_all = pd.concat(scores123, ignore_index=True)
log(f"  Seed42: {len(s42_all):,} scores, Seed123: {len(s123_all):,} scores")

# Ensemble: 两个模型的平均分数
ens_df = s42_all[['ts_code','trade_date','_fwd_ret','_target']].copy()
ens_df['score'] = (s42_all['score'].values + s123_all['score'].values) / 2

# 按日期和分数排序
def prep_scores(df):
    return df.sort_values(['trade_date','score'], ascending=[True,False])

s42_sorted = prep_scores(s42_all)
s123_sorted = prep_scores(s123_all)
ens_sorted = prep_scores(ens_df)

test_dates = sorted(s42_sorted['trade_date'].unique())
n_all = len(test_dates)
log(f"  {n_all} test dates")

# ====== Step 5: 网格回测 ======
log(f"\nStep 5: Grid search backtest...")

def run_backtest(scores_df, label, top_k, hold_days):
    """严谨资金回测"""
    N_BATCHES = 1
    ALLOC = CAPITAL / (top_k * N_BATCHES)
    
    cash, batches, daily_log = CAPITAL, [], []
    consec, scale = 0, 1.0
    
    for di, today in enumerate(test_dates):
        px = price_lookup.get(today, {})
        
        # 卖出到期
        sp = 0.0; keep = []
        for b in batches:
            if b['sell'] == today:
                for c, bp, fr in zip(b['codes'], b['bps'], b['fwds']):
                    cp = px.get(c)
                    actual = (cp*(1-SLIPPAGE)/bp-1) if cp else fr
                    sp += b['cost']/top_k * (1+actual) * (1-COMM_RATE-STAMP_RATE)
            else:
                keep.append(b)
        batches = keep; cash += sp
        
        # 市值
        m2m = 0.0
        for b in batches:
            for c, bp in zip(b['codes'], b['bps']):
                m2m += b['cost']/top_k * (1 + (px.get(c, bp)/bp - 1))
        nav = cash + m2m
        
        # CL风控
        if di > 0 and daily_log:
            prev = daily_log[-1]['nav']
            consec = consec+1 if nav/prev-1 < -0.001 else 0
            if consec >= 4:
                for b in batches: cash += b['cost']
                batches = []; m2m = 0.0; scale = 0.0
            elif consec >= 2:
                scale = 0.5
            else:
                scale = 1.0
        
        # 建仓
        if len(batches) < N_BATCHES and scale > 0:
            ds = scores_df[scores_df['trade_date'] == today]
            if len(ds) >= top_k:
                target = ALLOC * top_k * scale
                alloc = min(target, cash * 0.95)
                if alloc >= ALLOC * top_k * 0.5:
                    top = ds.head(top_k)
                    codes, bps, fwds = [], [], []
                    for _, r in top.iterrows():
                        cp = px.get(r['ts_code'])
                        if cp is None: continue
                        bp = cp * (1 + SLIPPAGE)
                        lots = max(1, int(alloc/top_k/bp/100))
                        if lots*100*bp*(1+COMM_RATE) <= alloc/top_k*1.2:
                            codes.append(r['ts_code'])
                            bps.append(bp); fwds.append(r['_fwd_ret'])
                    
                    enough = int(top_k * 0.5)  # 至少一半才建仓
                    if len(codes) >= enough:
                        cash -= alloc * len(codes) / top_k  # 按实际买的只数扣钱
                        sd = test_dates[di+hold_days] if di+hold_days < n_all else None
                        batches.append({'sell': sd, 'codes': codes, 'bps': bps,
                                      'fwds': fwds, 'cost': alloc * len(codes) / top_k})
        
        # 重算
        pm2m = 0.0
        for b in batches:
            for c, bp in zip(b['codes'], b['bps']):
                pm2m += b['cost']/len(b['codes']) * (1 + (px.get(c, bp)/bp - 1))
        nav = cash + pm2m
        daily_log.append({'date': today, 'nav': nav, 'n_pos': sum(len(b['codes']) for b in batches)})
    
    # 计算指标
    ldf = pd.DataFrame(daily_log)
    rets = ldf['nav'].pct_change().dropna().values
    yrs = len(rets)/250
    fin = ldf['nav'].iloc[-1]
    cagr = float((fin/CAPITAL)**(1/yrs)-1) if yrs>0 and fin>0 else 0
    vol = float(np.std(rets,ddof=1)*np.sqrt(250)) if len(rets)>1 else 0
    sharpe = cagr/vol if vol>0 else 0
    rmax = np.maximum.accumulate(ldf['nav'].values)
    mdd = float((ldf['nav'].values/rmax-1).min())
    wr = float(np.mean(rets>0)) if len(rets)>0 else 0
    avg_pos = ldf['n_pos'].mean()
    
    return {'cagr':cagr, 'sharpe':sharpe, 'maxdd':mdd, 'winrate':wr,
            'final_nav':float(fin), 'vol':vol, 'avg_pos':avg_pos,
            'n_rets':len(rets)}

# 网格参数
models = [
    ('LGBM42', s42_sorted),
    ('LGBM123', s123_sorted),
    ('Ensemble', ens_sorted),
]
top_ks = [2, 3, 5]
hold_days_list = [5, 10]

results = []
for mdl_name, scores in models:
    for tk in top_ks:
        for hd in hold_days_list:
            label = f"{mdl_name}_Top{tk}_H{hd}"
            r = run_backtest(scores, label, top_k=tk, hold_days=hd)
            r['model'] = mdl_name; r['top_k'] = tk; r['hold'] = hd
            results.append(r)
            log(f"  {label:<20} CAGR={r['cagr']*100:>7.1f}% Sharpe={r['sharpe']:>5.2f} "
                f"MaxDD={r['maxdd']*100:>6.1f}% WR={r['winrate']*100:>5.1f}% "
                f"Final=¥{r['final_nav']:>10,.0f} pos={r['avg_pos']:.1f}")

# ====== Step 6: 汇总表 ======
df = pd.DataFrame(results)

log(f"\n{'='*85}")
log(f"  网格回测汇总 (¥50k + 0.65%成本 + CL风控)")
log(f"{'='*85}")

# 按 Sharpe 排序
df['sharpe_rank'] = df['sharpe'].rank(ascending=False)

log(f"\n{'模型':<12} {'TopK':>5} {'Hold':>5} {'CAGR':>8} {'Sharpe':>7} {'MaxDD':>7} {'WinRate':>7} {'终值':>12} {'#Trades':>8}")
log("-"*85)

for _, row in df.sort_values('sharpe', ascending=False).iterrows():
    log(f"{row['model']:<12} {int(row['top_k']):>5} {int(row['hold']):>5} "
        f"{row['cagr']*100:>7.1f}% {row['sharpe']:>7.2f} {row['maxdd']*100:>6.1f}% "
        f"{row['winrate']*100:>6.1f}% ¥{row['final_nav']:>10,.0f} {row['n_rets']:>8}")

# 分组对比 Ensemble vs Single
log(f"\n{'='*70}")
log(f"  Ensemble(双模) vs Single(单模) 对比")
log(f"{'='*70}")

for tk in top_ks:
    for hd in hold_days_list:
        s42 = df[(df['model']=='LGBM42')&(df['top_k']==tk)&(df['hold']==hd)]
        ens = df[(df['model']=='Ensemble')&(df['top_k']==tk)&(df['hold']==hd)]
        if len(s42) and len(ens):
            dc = ens.iloc[0]['cagr'] - s42.iloc[0]['cagr']
            ds = ens.iloc[0]['sharpe'] - s42.iloc[0]['sharpe']
            dm = ens.iloc[0]['maxdd'] - s42.iloc[0]['maxdd']
            log(f"  Top{tk} H{hd}: ΔCAGR={dc*100:+.1f}pp ΔSharpe={ds:+.2f} ΔMaxDD={dm*100:+.1f}pp")

# 保存
out = r"C:\Users\Administrator\WorkBuddy\Claw\bt_grid_result.json"
df_out = df.drop('sharpe_rank', axis=1).to_dict('records')
with open(out, 'w') as f:
    json.dump(df_out, f, indent=2, default=float)

log(f"\nSaved: {out}")
log(f"Total: {time.time()-t0:.0f}s")
