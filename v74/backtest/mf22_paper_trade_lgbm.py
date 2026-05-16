#!/usr/bin/env python3
"""
MF v2.3 + LGBM因子集成版
========================
在原有MF v2.3的基础上, 加载LGBM预测分数, 以+2xLGBM权重加入Fusion20因子池。

用法:
  python3 mf22_paper_trade_lgbm.py          # 正常执行交易
  python3 mf22_paper_trade_lgbm.py --notify  # 仅发送邮件通知
  
数据依赖:
  output/v74/multi_factor/lgbm_pred_lgbm_5d.parquet
"""
import sys, json, time, gc
from pathlib import Path
import numpy as np
import pandas as pd

PROJECT_DIR = Path(__file__).parent.parent.parent
OUT_DIR = PROJECT_DIR / "output" / "v74" / "portfolio"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ===== 配置参数 =====
INITIAL_CAPITAL = 50000
HOLD_DAYS = 10
TOP_N = 3  # LGBM用Top3
POOL_SIZE = 500
COST_BUY = 0.00035
COST_SELL = 0.00135
START_DATE = '20240901'
END_DATE = '20260430'
WARMUP_DAYS = 60

STATE_FILE = OUT_DIR / "lgbm_trade_state.json"
LGBM_PRED_PATH = PROJECT_DIR / "output" / "v74" / "multi_factor" / "lgbm_pred_lgbm_5d.parquet"
DATE_PATH = PROJECT_DIR / "data" / "db" / "market.db"

# ===== 因子定义 =====
FUSION20_FACTORS = [
    'neg_volatility_20', 'neg_ma_bias', 'close_to_high', 'rev_5',
    'neg_pe_ttm', 'neg_pb', 'neg_ps_ttm', 'neg_ln_mv',
    'netprofit_yoy', 'op_yoy', 'or_yoy', 'roe',
    'avg_turnover_20', 'no_zt_5',
    'alpha_16', 'alpha_13', 'alpha_40', 'alpha_88',
    'main_pct', 'main_pct_5d',
]


def load_data():
    """加载当日数据 + LGBM预测分数"""
    import sqlite3 as sql
    conn = sql.connect(str(DATE_PATH))
    
    # 加载股票
    stocks = pd.read_sql('SELECT ts_code FROM stocks', conn)
    for p in ['688', '30', '8', '4', '920', 'bj']:
        stocks = stocks[~stocks['ts_code'].str.startswith(p)]
    valid = set(stocks['ts_code'])
    del stocks
    
    # 加载当日K线
    kline = pd.read_sql(f'''
        SELECT ts_code, trade_date, close, amount, pct_chg, pre_close, vol, high
        FROM daily_kline
        WHERE trade_date >= '{START_DATE}' AND trade_date <= '{END_DATE}'
    ''', conn)
    kline['trade_date'] = kline['trade_date'].astype(str)
    kline = kline[kline['ts_code'].isin(valid)]
    
    # 资金流向（定义池子）
    ff = pd.read_sql(f'''
        SELECT ts_code, trade_date, main_pct
        FROM fund_flow
        WHERE trade_date >= '{START_DATE}' AND trade_date <= '{END_DATE}'
    ''', conn)
    ff['trade_date'] = ff['trade_date'].astype(str)
    ff_codes = set(ff['ts_code'].unique())
    kline = kline[kline['ts_code'].isin(ff_codes)]
    
    # 基本面
    basic = pd.read_sql(f'''
        SELECT ts_code, trade_date, pe_ttm, pb, ps_ttm, total_mv
        FROM daily_basic
        WHERE trade_date >= '{START_DATE}' AND trade_date <= '{END_DATE}'
    ''', conn)
    basic['trade_date'] = basic['trade_date'].astype(str)
    basic = basic[basic['ts_code'].isin(valid) & basic['ts_code'].isin(ff_codes)]
    
    # 财务指标
    fina = pd.read_sql('''
        SELECT ts_code, ann_date, netprofit_yoy, op_yoy, or_yoy, roe
        FROM fina_indicator WHERE ann_date >= "20230101"
        ORDER BY ts_code, ann_date
    ''', conn)
    conn.close()
    
    fina = fina[fina['ts_code'].isin(valid) & fina['ts_code'].isin(ff_codes)].dropna(subset=['ann_date'])
    fina['_dt'] = pd.to_datetime(fina['ann_date'], format='%Y%m%d') + pd.Timedelta(days=1)
    kline['_dt'] = pd.to_datetime(kline['trade_date'], format='%Y%m%d')
    
    def ffill(g):
        sf = fina[fina['ts_code'] == g.name].sort_values('_dt')
        if sf.empty:
            for c in ['netprofit_yoy', 'op_yoy', 'or_yoy', 'roe']: g[c] = np.nan
            return g
        return pd.merge_asof(g.sort_values('_dt'), sf[['_dt','netprofit_yoy','op_yoy','or_yoy','roe']], on='_dt', direction='backward')
    
    kline = kline.groupby('ts_code', group_keys=False).apply(ffill, include_groups=True)
    kline.drop(columns=['_dt'], inplace=True)
    
    # 计算因子
    g = kline.groupby('ts_code')
    kline['ma20'] = g['close'].transform(lambda s: s.rolling(20, min_periods=10).mean())
    kline['ma_bias'] = (kline['close'] - kline['ma20']) / (kline['ma20'] + 1e-8)
    kline['c2h'] = kline['close'] / g['high'].transform(lambda s: s.rolling(20).max())
    kline['ret_5'] = g['pct_chg'].transform(lambda s: s.rolling(5).sum()) / 100
    kline['nv20'] = -g['pct_chg'].transform(lambda s: s.rolling(20, min_periods=10).std())
    kline['at20'] = -g['vol'].transform(lambda s: s.rolling(20).mean())
    kline['is_limit'] = (kline['pct_chg'] >= 9.5) & (kline['pre_close'] > 0)
    kline['no_zt_5'] = 1 - 2 * g['is_limit'].transform(lambda s: s.rolling(5).max().fillna(0))
    
    kline = kline.merge(basic, on=['ts_code', 'trade_date'], how='left')
    kline = kline.merge(ff, on=['ts_code', 'trade_date'], how='left')
    
    # 合成因子
    fmap = {
        'neg_volatility_20': kline['nv20'].astype('float32'),
        'neg_ma_bias': (-kline['ma_bias']).astype('float32'),
        'close_to_high': kline['c2h'].astype('float32'),
        'rev_5': (-kline['ret_5']).astype('float32'),
        'neg_pe_ttm': (-kline['pe_ttm'].clip(upper=200).fillna(0)).astype('float32'),
        'neg_pb': (-kline['pb'].clip(upper=50).fillna(0)).astype('float32'),
        'neg_ps_ttm': (-kline['ps_ttm'].clip(upper=50).fillna(0)).astype('float32'),
        'neg_ln_mv': (-np.log(kline['total_mv'].clip(lower=1).fillna(1e9))).astype('float32'),
        'netprofit_yoy': (kline['netprofit_yoy'].fillna(0).clip(-500, 500) / 100).astype('float32'),
        'op_yoy': (kline['op_yoy'].fillna(0).clip(-500, 500) / 100).astype('float32'),
        'or_yoy': (kline['or_yoy'].fillna(0).clip(-500, 500) / 100).astype('float32'),
        'roe': (kline['roe'].fillna(0).clip(-100, 100) / 100).astype('float32'),
        'avg_turnover_20': kline['at20'].astype('float32'),
        'no_zt_5': kline['no_zt_5'].astype('float32'),
        'main_pct': kline['main_pct'].fillna(0).astype('float32'),
        'main_pct_5d': kline.groupby('ts_code')['main_pct'].transform(lambda s: s.rolling(5, min_periods=3).mean()).fillna(0).astype('float32'),
    }
    for k, v in fmap.items():
        kline[k] = v
    
    # 合并LGBM预测分数
    pred = pd.read_parquet(LGBM_PRED_PATH)
    kline = kline.merge(pred, on=['ts_code', 'trade_date'], how='left')
    # 统一列名: lgbm_5d → lgbm_score
    if 'lgbm_5d' in kline.columns:
        kline['lgbm_score'] = kline['lgbm_5d'].fillna(0)
    
    return kline


def run_trade(data):
    """执行交易逻辑：+2xLGBM Top3-10d"""
    avail = [f for f in FUSION20_FACTORS if f in data.columns]
    factor_cols = avail + ['lgbm_score', 'lgbm_score']  # 2x LGBM
    
    all_dates = sorted(data['trade_date'].unique())
    warmup_end = all_dates[min(WARMUP_DAYS, len(all_dates)-1)]
    rebalance_dates = [d for d in all_dates[::HOLD_DAYS] if d >= warmup_end]
    date_map = {d: i for i, d in enumerate(all_dates)}
    
    capital = INITIAL_CAPITAL
    cash = capital
    positions = {}
    trades = []
    equity_curve = []
    
    for rd in rebalance_dates:
        today = data[data['trade_date'] == rd]
        if today.empty:
            continue
        
        # 卖出到期
        if positions:
            for sym in list(positions.keys()):
                p = positions[sym]
                if date_map[rd] - date_map.get(p['entry_date'], 0) >= HOLD_DAYS:
                    row = today[today['ts_code'] == sym]
                    if not row.empty:
                        close = row.iloc[0]['close']
                        ret = (close - p['entry_price']) / p['entry_price'] * 100
                        cash += p['shares'] * close * (1 - COST_SELL)
                        trades.append({'ret': round(ret, 2)})
                    del positions[sym]
        
        # 选股
        cols = [f for f in factor_cols if f in today.columns]
        if cols:
            vals = today[cols].fillna(0)
            for c in cols:
                std_v = vals[c].std()
                if isinstance(std_v, (np.ndarray, pd.Series)):
                    std_v = float(std_v.max())
                if std_v > 1e-8:
                    vals[c] = (vals[c] - vals[c].mean()) / std_v
                else:
                    vals[c] = 0
            score = vals.mean(axis=1)
            score.index = today['ts_code']
            score = score.sort_values(ascending=False)
            
            cand = score[~score.index.isin(positions.keys())]
            top_set = set(today.nlargest(POOL_SIZE, 'amount')['ts_code'])
            cand = cand[cand.index.isin(top_set)]
            selected = cand.head(TOP_N).index.tolist()
            
            slots = TOP_N - len(positions)
            if slots > 0 and selected:
                per_stock = cash / slots
                for sym in selected[:slots]:
                    row = today[today['ts_code'] == sym]
                    if row.empty: continue
                    close = row.iloc[0]['close']
                    if close <= 0: continue
                    sh = int(per_stock / close / 100) * 100
                    if sh < 100: continue
                    cost = sh * close * (1 + COST_BUY)
                    if cost <= cash:
                        cash -= cost
                        positions[sym] = {
                            'entry_date': rd, 'entry_price': close, 'shares': sh,
                        }
        
        nav = cash
        for sym, p in positions.items():
            row = today[today['ts_code'] == sym]
            if not row.empty:
                nav += p['shares'] * row.iloc[0]['close']
        equity_curve.append({'date': rd, 'nav': round(nav, 2)})
    
    return calc_metrics(equity_curve, trades, capital)


def calc_metrics(equity_curve, trades, capital):
    if not equity_curve:
        return {'ret': 0, 'sharpe': 0, 'dd': 0, 'wr': 0, 'pf': 0, 'n': 0}
    fn = equity_curve[-1]['nav']
    tr = (fn - capital) / capital * 100
    navs = [e['nav'] for e in equity_curve]
    rets = [(navs[i] - navs[i-1]) / navs[i-1] for i in range(1, len(navs)) if navs[i-1] > 0]
    sharpe = 0
    if len(rets) > 1 and np.std(rets, ddof=1) > 0:
        sharpe = (np.mean(rets) * 252 / HOLD_DAYS - 0.02) / (np.std(rets, ddof=1) * np.sqrt(252 / HOLD_DAYS))
    peak = capital; mdd = 0
    for e in equity_curve:
        v = e['nav']
        if v > peak: peak = v
        mdd = max(mdd, (peak - v) / peak * 100)
    n_trades = len(trades)
    wins = sum(1 for t in trades if t['ret'] > 0)
    wr = wins / n_trades * 100 if n_trades > 0 else 0
    win_r = np.mean([t['ret'] for t in trades if t['ret'] > 0]) if wins > 0 else 0
    loss_r = abs(np.mean([t['ret'] for t in trades if t['ret'] <= 0])) if n_trades > wins > 0 else 1
    pf = win_r / loss_r if loss_r > 0 else 0
    return {'ret': round(tr, 2), 'sharpe': round(sharpe, 4), 'dd': round(mdd, 2), 'wr': round(wr, 1), 'pf': round(pf, 2), 'n': n_trades}


def main():
    print(f"LGBM因子策略模拟交易 (1.0)")
    print(f"方案: +2xLGBM Top3-10d")
    print()
    
    t0 = time.time()
    data = load_data()
    gc.collect()
    print(f"数据: {len(data):,}行, {data['trade_date'].nunique()}天")
    
    result = run_trade(data)
    print(f"\n回测结果:")
    print(f"  收益: {result['ret']:+.2f}%")
    print(f"  Sharpe: {result['sharpe']:.4f}")
    print(f"  回撤: {result['dd']:.2f}%")
    print(f"  胜率: {result['wr']:.1f}%")
    print(f"  PF: {result['pf']:.2f}")
    print(f"  交易: {result['n']}笔")
    print(f"  耗时: {time.time()-t0:.0f}s")


if __name__ == '__main__':
    main()
