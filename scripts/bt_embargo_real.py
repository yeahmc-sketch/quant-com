#!/usr/bin/env python3
"""
ELITE 因子 严谨资金回测 -- Embargo + GPU + 真实约束
=================================================
真实模拟 ¥50k 账户：每天选Top2，持5日，成本+滑点+风控
"""
import subprocess, os, json, time, gc, warnings, glob as gl
from datetime import datetime
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

LOG = r"C:\Users\Administrator\WorkBuddy\Claw\bt_embargo_real_" + datetime.now().strftime('%H%M%S') + ".log"
OUT = r"C:\Users\Administrator\WorkBuddy\Claw\bt_embargo_real_result.json"
SCORES_DIR = r"C:\Users\Administrator\WorkBuddy\Claw\bt_scores"
SCORES_DIR_WSL = "/mnt/c/Users/Administrator/WorkBuddy/Claw/bt_scores"
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
BUY_COST = COMM_RATE + SLIPPAGE       # 0.3%
SELL_COST = COMM_RATE + STAMP_RATE + SLIPPAGE  # 0.35%
BATCH_SIZE = 2; HOLD_DAYS = 5; N_BATCHES = 1
ALLOC_PER_POS = CAPITAL / (BATCH_SIZE * N_BATCHES)
EMBARGO_DAYS, FORWARD = 5, 5
STEP, TRAIN, VAL = 60, 480, 60

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

PREPROC = r"C:\ML_STATION\LGBM_ML_Package\data\bt_elite_preproc.parquet"
PREPROC_WSL = "/mnt/c/ML_STATION/LGBM_ML_Package/data/bt_elite_preproc.parquet"

log("="*70)
log(f"ELITE Real Backtest -- EMBARGO={EMBARGO_DAYS}d + GPU + Real")
log(f"Capital=¥{CAPITAL:,.0f}, Top{BATCH_SIZE}, Cost={BUY_COST*100:.1f}%/{SELL_COST*100:.1f}%")
log(f"Start: {datetime.now()}")
t0 = time.time()

# ====== Step 1: 数据 ======
log("Step 1: Loading data...")
import pyarrow.parquet as pq
preproc = pq.read_table(PREPROC).to_pandas()
dates_list = sorted(set(preproc['trade_date']))
log(f"  {len(dates_list)} dates")

# 价格表
full_data = r"C:\ML_STATION\LGBM_ML_Package\data\fusion20_master.parquet"
raw = pq.read_table(full_data, columns=['ts_code','trade_date','close']).to_pandas()
raw['trade_date'] = raw['trade_date'].astype(str)
price_lookup = {}
for _, row in raw.iterrows():
    price_lookup.setdefault(row['trade_date'], {})[row['ts_code']] = row['close']
del raw; gc.collect()
log(f"  Price lookup OK")

# ====== Step 2: 窗口 ======
log("Step 2: Embargo windows...")
n_dates = len(dates_list)
windows = []
for i in range(TRAIN + EMBARGO_DAYS, n_dates - VAL + 1, STEP):
    te = i - 1 - EMBARGO_DAYS
    ts = max(0, te - TRAIN + 1)
    vs, ve = i, min(i + VAL - 1, n_dates - 1)
    if (te - ts) < TRAIN * 0.5: continue
    windows.append({'id': len(windows), 'ts': dates_list[ts], 'te': dates_list[te],
                    'vs': dates_list[vs], 've': dates_list[ve]})
log(f"  {len(windows)} windows")

# ====== Step 3: GPU训练+保存分数 ======
log("Step 3: WSL GPU training...")
for w in windows:
    wid = w['id']
    sf = os.path.join(SCORES_DIR, f"scores_{wid}.parquet")
    if os.path.exists(sf):
        log(f"  Win {wid+1}/{len(windows)}: already done, skip")
        continue

    log(f"\n[{datetime.now():%H:%M:%S}] Win {wid+1}/{len(windows)}: "
        f"train {w['ts']}~{w['te']} val {w['vs']}~{w['ve']}")

    wsf = to_wsl(sf)
    # WSL pyarrow写parquet到Windows挂载盘偶尔[Errno 22]
    # 先写到WSL本地/tmp，再mv过去
    wsl_tmp = f"/tmp/scores_{wid}.parquet"

    ff = repr(ELITE_FACTORS)
    scr = ('import gc, subprocess, warnings; warnings.filterwarnings("ignore"); '
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
           'min_gain_to_split=1.0,verbosity=-1,n_jobs=-1,random_state=42,'
           'device="cuda"); '
           'm.fit(Xtr,tr._target,'
           'eval_set=[(Xvl,vl._target.values.astype("float32"))],'
           'callbacks=[early_stopping(20),log_evaluation(0)]); '
           'pred=m.predict(Xvl); ic,_=spearmanr(pred,vl._target); '
           'sf=vl[["ts_code","trade_date","_fwd_ret","_target"]].copy(); '
           'sf["score"]=pred; '
           f'sf.to_parquet("{wsl_tmp}"); '
           f'subprocess.run(["mv","-f","{wsl_tmp}","{wsf}"]); '
           'print(f"RESULT: IC={ic:.4f} trees={m.n_estimators_} rows={len(sf)}")')

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
            log(f"  FAILED ({elap:.0f}s): {proc.stderr.strip()[-300:]}")
            break
    except subprocess.TimeoutExpired:
        log(f"  TIMEOUT"); break
    except Exception as e:
        log(f"  ERROR: {e}"); break

# ====== Step 4: 收集分数 ======
log(f"\nStep 4: Collecting scores...")
all_scores = []
for w in windows:
    sf = os.path.join(SCORES_DIR, f"scores_{w['id']}.parquet")
    if os.path.exists(sf):
        all_scores.append(pd.read_parquet(sf))
if not all_scores:
    log("ERROR: No scores!"); exit(1)
scores_all = pd.concat(all_scores, ignore_index=True)
scores_all = scores_all.sort_values(['trade_date','score'], ascending=[True,False])
test_dates = sorted(scores_all['trade_date'].unique())
log(f"  {len(scores_all):,} scores, {len(test_dates)} dates")

# ====== Step 5: 严谨回测 ======
log(f"\nStep 5: Rigorous backtest...")
n_all = len(test_dates)
cash, batches, daily_log = CAPITAL, [], []
consec, scale = 0, 1.0

for di, today in enumerate(test_dates):
    px = price_lookup.get(today, {})

    # 1) 卖出到期
    sp = 0.0; keep = []
    for b in batches:
        if b['sell'] == today:
            for c, bp, fr in zip(b['codes'], b['bps'], b['fwds']):
                cp = px.get(c)
                actual = (cp*(1-SLIPPAGE)/bp-1) if cp else fr
                sp += b['cost']/BATCH_SIZE * (1+actual) * (1-COMM_RATE-STAMP_RATE)
        else:
            keep.append(b)
    batches = keep; cash += sp

    # 2) 市值
    m2m = 0.0
    for b in batches:
        for c, bp in zip(b['codes'], b['bps']):
            m2m += b['cost']/BATCH_SIZE * (1 + (px.get(c, bp)/bp - 1))
    nav = cash + m2m

    # 3) ConsecLoss
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

    # 4) 建仓
    if len(batches) < N_BATCHES and scale > 0:
        ds = scores_all[scores_all['trade_date'] == today]
        if len(ds) >= BATCH_SIZE:
            target = ALLOC_PER_POS * BATCH_SIZE * scale
            alloc = min(target, cash * 0.95)
            if alloc >= ALLOC_PER_POS * BATCH_SIZE * 0.5:
                top = ds.head(BATCH_SIZE)
                codes, bps, fwds = [], [], []
                for _, r in top.iterrows():
                    cp = px.get(r['ts_code'])
                    if cp is None: continue
                    bp = cp * (1 + SLIPPAGE)
                    lots = max(1, int(alloc/BATCH_SIZE/bp/100))
                    if lots*100*bp*(1+COMM_RATE) <= alloc/BATCH_SIZE*1.2:
                        codes.append(r['ts_code'])
                        bps.append(bp); fwds.append(r['_fwd_ret'])
                if len(codes) == BATCH_SIZE:
                    cash -= alloc
                    sd = test_dates[di+HOLD_DAYS] if di+HOLD_DAYS < n_all else None
                    batches.append({'sell': sd, 'codes': codes, 'bps': bps,
                                   'fwds': fwds, 'cost': alloc})

    # 5) 重算
    pm2m = 0.0
    for b in batches:
        for c, bp in zip(b['codes'], b['bps']):
            pm2m += b['cost']/BATCH_SIZE * (1 + (px.get(c, bp)/bp - 1))
    nav = cash + pm2m
    daily_log.append({'date': today, 'nav': nav, 'cash': cash,
                      'position': pm2m, 'n_batches': len(batches), 'scale': scale})

# ====== Step 6: 结果 ======
log_df = pd.DataFrame(daily_log)
rets = log_df['nav'].pct_change().dropna().values
yrs = len(rets)/250
fin = log_df['nav'].iloc[-1]
total_ret = fin/CAPITAL - 1
cagr = float((fin/CAPITAL)**(1/yrs)-1) if yrs>0 else 0
vol = float(np.std(rets,ddof=1)*np.sqrt(250))
sharpe = cagr/vol if vol>0 else 0
rmax = np.maximum.accumulate(log_df['nav'].values)
mdd = float((log_df['nav'].values/rmax-1).min())
wr = float(np.mean(rets>0))
cur = mx = 0
for r in rets: cur = cur+1 if r<0 else 0; mx = max(mx, cur)

log(f"\n{'='*55}")
log(f"  严谨回测结果 (Embargo={EMBARGO_DAYS}d+GPU+Real)")
log(f"{'='*55}")
log(f"  初始资金:       ¥{CAPITAL:,.0f}")
log(f"  最终资金:       ¥{fin:,.0f}")
log(f"  总收益:         {total_ret*100:.0f}%")
log(f"  CAGR:           {cagr*100:.1f}%")
log(f"  Sharpe:         {sharpe:.2f}")
log(f"  年化波动:       {vol*100:.1f}%")
log(f"  最大回撤:       {mdd*100:.1f}%")
log(f"  胜率:           {wr*100:.1f}%")
log(f"  最大连败:       {mx}天")
log(f"  平均仓位:       {log_df['position'].mean()/CAPITAL*100:.0f}%")

# 信号 vs 真实对比
log(f"\n{'='*55}")
log(f"  信号回测 vs 真实回测")
log(f"{'='*55}")

WIN_DIR = r"C:\Users\Administrator\WorkBuddy\Claw\bt_windows"
for label, pattern in [('Signal(no-embargo)', 'elite_*.json'),
                        ('Signal(embargo)', 'embargo_*.json')]:
    fs = sorted(gl.glob(os.path.join(WIN_DIR, pattern)))
    all_t = []
    for f in fs:
        with open(f) as fh:
            all_t.extend(json.load(fh)['all_trades'])
    if all_t:
        td = pd.DataFrame(all_t)
        daily = (1 + td['ret']) ** (1/FORWARD) - 1
        nav_s = (1 + daily).cumprod()
        ny = len(td)/250
        c = float(nav_s.iloc[-1]**(1/ny)-1) if ny>0 else 0
        v = float(daily.std()*np.sqrt(250))
        s = c/v if v>0 else 0
        d = float((nav_s/nav_s.cummax()-1).min())
        log(f"  {label:<20} CAGR={c*100:>8.1f}% Sharpe={s:>6.2f} MaxDD={d*100:>6.1f}%  (无限资金无成本)")

log(f"  {'Real(embargo)':<20} CAGR={cagr*100:>8.1f}% Sharpe={sharpe:>6.2f} MaxDD={mdd*100:>6.1f}%  (¥50k+成本+风控)")

result = dict(version='Real', capital=CAPITAL, embargo_days=EMBARGO_DAYS,
              cagr=float(cagr), sharpe=float(sharpe), max_dd=float(mdd),
              total_return=float(total_ret), annual_vol=float(vol),
              win_rate=float(wr), final_nav=float(fin))
with open(OUT, 'w') as f:
    json.dump(result, f, indent=2)
log(f"\nSaved: {OUT}")
log(f"Total: {time.time()-t0:.0f}s")
