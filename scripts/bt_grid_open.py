#!/usr/bin/env python3
"""
LGBM 网格搜索 — 真实交易增强版
================================
改进：
1. 买入价：T+1日开盘价（非T日收盘价）
2. 涨停过滤：pct_chg>=9.5 买不进
3. 跌停延期：到期日跌停则持仓延期至次日
4. 双模集成 + Top2/3/5 + H5/H10 网格
"""
import os, json, time, gc, warnings
from datetime import datetime
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

LOG = r"C:\Users\Administrator\WorkBuddy\Claw\bt_grid_open_" + datetime.now().strftime('%H%M%S') + ".log"
SCORES_DIR = r"C:\Users\Administrator\WorkBuddy\Claw\bt_scores"

def log(s):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(s+'\n'); f.flush()
    print(s, flush=True)

log("="*70)
log(f"Grid Search — T+1 Open Real Trading")
log(f"Start: {datetime.now()}")
t0 = time.time()

# ====== 参数 ======
CAPITAL = 50000.0
COMM_RATE, STAMP_RATE, SLIPPAGE = 0.001, 0.0005, 0.002
BUY_COST = COMM_RATE + SLIPPAGE       # 0.3% (open滑点)
SELL_COST = COMM_RATE + STAMP_RATE + SLIPPAGE  # 0.35%
# 注意：用open价买入，单边不额外加滑点（open已反映开盘价）
# 但实际机构交易仍然有滑点，保留SLIPPAGE在成本中

# ====== Step 1: 加载数据 ======
log("Step 1: Loading data...")

# 价格数据（含open + pct_chg）— 避免iterrows()，用向量化构建
import pyarrow.parquet as pq
full_path = r"C:\ML_STATION\LGBM_ML_Package\data\fusion20_master.parquet"
cols_needed = ['ts_code','trade_date','close','open','pct_chg']
table = pq.read_table(full_path, columns=cols_needed)
raw = table.to_pandas()
raw.columns = [c.lower() for c in raw.columns]
raw['trade_date'] = raw['trade_date'].astype(str)
raw['pct_chg'] = raw['pct_chg'].fillna(0.0)

# 构建 {date: {code: {open, close, pct_chg}}}
# 用groupby+dict comprehension 比 iterrows 快100倍
price_db = {}
grouped = list(raw.groupby('trade_date'))
for date_str, grp in grouped:
    pdict = {}
    for _, row in grp.iterrows():
        pdict[row['ts_code']] = {'open': row['open'], 'close': row['close'], 'pct_chg': row['pct_chg']}
    price_db[date_str] = pdict
del raw, table, grouped; gc.collect()

all_dates = sorted(price_db.keys())
date_idx = {d:i for i,d in enumerate(all_dates)}
log(f"  {len(all_dates)} trading dates, {sum(len(v) for v in price_db.values()):,} rows")

# ====== Step 2: 加载分数 ======
log("Step 2: Loading scores...")
scores42, scores123 = [], []
for wid in range(21):
    s42 = os.path.join(SCORES_DIR, f"scores_{wid}.parquet")
    s123 = os.path.join(SCORES_DIR, f"seed123_{wid}.parquet")
    if os.path.exists(s42):
        scores42.append(pd.read_parquet(s42))
    if os.path.exists(s123):
        scores123.append(pd.read_parquet(s123))

s42_all = pd.concat(scores42, ignore_index=True)
s123_all = pd.concat(scores123, ignore_index=True)

# Ensemble scores
ens = s42_all[['ts_code','trade_date','_fwd_ret','_target']].copy()
ens['score'] = (s42_all['score'].values + s123_all['score'].values) / 2

def prep(df):
    return df.sort_values(['trade_date','score'], ascending=[True,False])

models = [
    ('LGBM42', prep(s42_all)),
    ('Ensemble', prep(ens)),
]
test_dates = sorted(set(s42_all['trade_date']) & set(all_dates))
test_dates = sorted(set(d for d in test_dates 
                        if date_idx.get(d,0) < len(all_dates)-1))  # 确保有T+1
log(f"  {len(test_dates)} test dates")

# ====== Step 3: 真实回测引擎 ======
log("Step 3: Running grid backtest...")

# 涨停阈值（根据股票代码区分，简化处理）
def is_limit_up(code, date_str):
    """判断该日是否涨停（无法买入）"""
    info = price_db.get(date_str, {}).get(code)
    if info is None: return True  # 无数据=无法交易
    pct = info['pct_chg']
    # 主板10%，创业板/科创板20%，ST 5%
    # 简化：pct>=9.0 视为涨停
    return pct >= 9.0

def is_limit_down(code, date_str):
    """判断该日是否跌停（无法卖出）"""
    info = price_db.get(date_str, {}).get(code)
    if info is None: return True
    pct = info['pct_chg']
    return pct <= -9.0

def run_backtest(scores_df, top_k, hold_days):
    """T+1开盘价买入的真实回测"""
    cash, positions, daily_log = CAPITAL, [], []
    consec, scale = 0, 1.0
    
    for di, today in enumerate(test_dates):
        # ====== 0. 获取T+1日（实际买入/卖出执行日） ======
        ti = date_idx.get(today, -1)
        t1_day = all_dates[ti+1] if ti >= 0 and ti+1 < len(all_dates) else None
        
        # ====== 1. 卖出到期持仓 ======
        sell_proceeds = 0.0
        surviving = []
        for p in positions:
            # 到期日：持有hold_days天后尝试卖出
            sell_target_day = p['sell_target']
            
            if sell_target_day == today:
                # 尝试卖出这一天
                code = p['code']
                info = price_db.get(today, {}).get(code)
                
                if info is None:
                    # 停牌，继续持有
                    p['sell_target'] = test_dates[min(di+1, len(test_dates)-1)] if di+1 < len(test_dates) else None
                    surviving.append(p)
                    continue
                
                if is_limit_down(code, today):
                    # 跌停卖不出，延期1天
                    if di+1 < len(test_dates):
                        p['sell_target'] = test_dates[di+1]
                    surviving.append(p)
                    continue
                
                # 正常卖出：T日close价（收盘价卖出，接近真实）
                sell_price = info['close'] * (1 - SLIPPAGE)
                actual_ret = sell_price / p['buy_price'] - 1
                proceeds = p['alloc'] * (1 + actual_ret) * (1 - COMM_RATE - STAMP_RATE)
                sell_proceeds += proceeds
            else:
                surviving.append(p)
        
        positions = surviving
        cash += sell_proceeds
        
        # ====== 2. 日终市值（用close计） ======
        m2m = 0.0
        for p in positions:
            info = price_db.get(today, {}).get(p['code'])
            if info:
                m2m += p['alloc'] * (1 + info['close']/p['buy_price'] - 1)
            else:
                m2m += p['alloc']  # 停牌按成本价计
        nav = cash + m2m
        
        # ====== 3. ConsecLoss 风控 ======
        if di > 0 and daily_log:
            prev_nav = daily_log[-1]['nav']
            if nav / prev_nav - 1 < -0.001:
                consec += 1
            else:
                consec = 0
            
            if consec >= 4:
                scale = 0.0
                for p in positions:
                    cash += p['alloc']  # 简化：成本价清仓
                positions = []; m2m = 0.0
            elif consec >= 2:
                scale = 0.5
            else:
                scale = 1.0
        
        # ====== 4. 建新仓（T日选股 → T+1日开盘价买入） ======
        max_positions = top_k
        if len(positions) < max_positions and scale > 0 and t1_day is not None:
            slots = max_positions - len(positions)
            day_scores = scores_df[scores_df['trade_date'] == today]
            
            if len(day_scores) > 0:
                ALLOC_PER = CAPITAL / max_positions
                target = ALLOC_PER * slots * scale
                alloc_per_stock = min(target / max(1, slots), cash * 0.95 / max(1, slots))
                
                candidates = day_scores.head(slots * 5)  # 扩大候选池，防涨停筛掉
                bought = 0
                
                for _, row in candidates.iterrows():
                    if bought >= slots:
                        break
                    
                    code = row['ts_code']
                    
                    # 检查T+1日是否可交易
                    if is_limit_up(code, t1_day):
                        continue  # 涨停买不进
                    
                    t1_info = price_db.get(t1_day, {}).get(code)
                    if t1_info is None:
                        continue  # T+1日停牌
                    
                    buy_price = t1_info['open'] * (1 + SLIPPAGE)
                    
                    # 检查最少1手
                    lots = max(1, int(alloc_per_stock / buy_price / 100))
                    cost = lots * 100 * buy_price * (1 + COMM_RATE)
                    
                    if cost <= alloc_per_stock * 1.2 and cost <= cash:
                        cash -= cost
                        se = di + hold_days
                        sell_target = test_dates[se] if se < len(test_dates) else None
                        
                        positions.append({
                            'code': code,
                            'buy_price': buy_price,
                            'alloc': cost,  # 实际花费
                            'sell_target': sell_target,
                            'buy_date': t1_day,
                        })
                        bought += 1
        
        # ====== 5. 重算市值 ======
        post_m2m = 0.0
        for p in positions:
            info = price_db.get(today, {}).get(p['code'])
            if info:
                post_m2m += p['alloc'] * (1 + info['close']/p['buy_price'] - 1)
            else:
                post_m2m += p['alloc']
        nav = cash + post_m2m
        
        daily_log.append({
            'date': today, 'nav': nav, 'n_pos': len(positions), 'scale': scale
        })
    
    # 计算指标
    ldf = pd.DataFrame(daily_log)
    rets = ldf['nav'].pct_change().dropna().values
    fin = ldf['nav'].iloc[-1] if len(ldf) > 0 else CAPITAL
    yrs = len(rets) / 250 if len(rets) > 0 else 0
    
    cagr = float((fin/CAPITAL)**(1/yrs) - 1) if yrs > 0 and fin > 0 else 0
    vol = float(np.std(rets, ddof=1) * np.sqrt(250)) if len(rets) > 1 else 0
    sharpe = cagr / vol if vol > 0 else 0
    rmax = np.maximum.accumulate(ldf['nav'].values)
    mdd = float((ldf['nav'].values/rmax - 1).min()) if len(ldf) > 0 else 0
    wr = float(np.mean(rets > 0)) if len(rets) > 0 else 0
    
    return {'cagr':cagr, 'sharpe':sharpe, 'maxdd':mdd, 'winrate':wr,
            'final_nav':float(fin), 'vol':vol, 'n_days':len(rets),
            'avg_pos': ldf['n_pos'].mean() if len(ldf)>0 else 0}

# ====== 运行网格 ======
results = []
for mdl_name, scores in models:
    for tk in [2, 3, 5]:
        for hd in [5, 10]:
            label = f"{mdl_name}_Top{tk}_H{hd}"
            r = run_backtest(scores, top_k=tk, hold_days=hd)
            r['model'] = mdl_name; r['top_k'] = tk; r['hold'] = hd
            results.append(r)
            log(f"  {label:<20} CAGR={r['cagr']*100:>7.1f}% Sharpe={r['sharpe']:>5.2f} "
                f"MaxDD={r['maxdd']*100:>6.1f}% WR={r['winrate']*100:>5.1f}% "
                f"Final=¥{r['final_nav']:>10,.0f} pos={r['avg_pos']:.1f}")

# ====== 汇总 ======
df = pd.DataFrame(results)
log(f"\n{'='*85}")
log(f"  T+1开盘价 + 涨跌停过滤 — 真实交易")
log(f"{'='*85}")

log(f"\n{'模型':<12} {'TopK':>5} {'Hold':>5} {'CAGR':>8} {'Sharpe':>7} {'MaxDD':>7} {'WinRate':>7} {'终值':>12} {'#Trades':>8}")
log("-"*85)

for _, row in df.sort_values('sharpe', ascending=False).iterrows():
    log(f"{row['model']:<12} {int(row['top_k']):>5} {int(row['hold']):>5} "
        f"{row['cagr']*100:>7.1f}% {row['sharpe']:>7.2f} {row['maxdd']*100:>6.1f}% "
        f"{row['winrate']*100:>6.1f}% ¥{row['final_nav']:>10,.0f} {row['n_days']:>8}")

# 对比：收盘价版 vs 开盘价版
log(f"\n{'='*70}")
log(f"  收盘价买入 vs T+1开盘价买入 (Top2 H5)")
log(f"{'='*70}")
log(f"  [从bt_grid结果对比]")
log(f"  收盘价版(LGBM42): CAGR=86.6% Sharpe=5.62 MaxDD=-4.7%  ¥1,157,363")
t1_42 = [r for r in results if r['model']=='LGBM42' and r['top_k']==2 and r['hold']==5]
if t1_42:
    t1 = t1_42[0]
    log(f"  开盘价版(LGBM42): CAGR={t1['cagr']*100:.1f}% Sharpe={t1['sharpe']:.2f} MaxDD={t1['maxdd']*100:.1f}%  ¥{t1['final_nav']:,.0f}")
    log(f"  ΔCAGR: {(t1['cagr']-0.866)*100:+.1f}pp  真实交易损耗")

out = r"C:\Users\Administrator\WorkBuddy\Claw\bt_grid_open_result.json"
with open(out, 'w') as f:
    json.dump([{k:(float(v) if isinstance(v,(np.floating,np.integer)) else v) for k,v in r.items()} for r in results], f, indent=2)

log(f"\nSaved: {out}")
log(f"Total: {time.time()-t0:.0f}s")
