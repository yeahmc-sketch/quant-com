#!/usr/bin/env python3
"""
B2 爆发力 — 独立选股工具 (不做模拟交易)
========================================
完整21因子: Fusion20 (14量价财务 + 4α101 + 2资金) + 2xLGBM预测分数

用法:
  # 选今天 (自动判断最新交易日)
  python3 v74/backtest/b2_stock_picker.py

  # 指定日期
  python3 v74/backtest/b2_stock_picker.py --date 20260508

  # 输出Top N (默认10)
  python3 v74/backtest/b2_stock_picker.py --top 5

数据依赖:
  - data/db/market.db (日线/基本面/资金流向/财务指标)
  - output/v74/multi_factor/lgbm_model_v1.txt (LGBM导出模型)
  - output/v74/multi_factor/lgbm_pred_lgbm_5d.parquet (LGBM预测缓存, 可选)
  - output/v74/multi_factor/101_core_v2.parquet (α101缓存, 可选)
"""

import sys, json, os, warnings, time, argparse
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import sqlite3
from lightgbm import Booster

warnings.filterwarnings('ignore')

# ===== 路径 =====
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
DB_PATH = PROJECT_DIR / "data" / "db" / "market.db"
LGBM_MODEL_PATH = PROJECT_DIR / "output" / "v74" / "multi_factor" / "lgbm_model_v1.txt"
LGBM_PRED_PATH = PROJECT_DIR / "output" / "v74" / "multi_factor" / "lgbm_pred_lgbm_5d.parquet"
ALPHA_PATH = PROJECT_DIR / "output" / "v74" / "multi_factor" / "101_core_v2.parquet"

# ===== 策略参数 =====
POOL_SIZE = 500           # 活跃池: Top500成交额
TOP_N = 10                # 默认输出Top N
LGBM_WEIGHT = 2           # LGBM权重倍数
DATA_START = 20240101      # 因子计算起始日
EXCLUDE_PREFIXES = ['688', '30', '8', '4', '920', 'bj']

# Fusion20因子 (不含LGBM)
FACTORS = [
    'neg_volatility_20', 'neg_ma_bias', 'close_to_high', 'rev_5',
    'neg_pe_ttm', 'neg_pb', 'neg_ps_ttm', 'neg_ln_mv',
    'netprofit_yoy', 'op_yoy', 'or_yoy', 'roe',
    'avg_turnover_20', 'no_zt_5',
    'alpha_16', 'alpha_13', 'alpha_40', 'alpha_88',
    'main_pct', 'main_pct_5d',
]


def get_latest_trade_date(conn):
    """从DB获取最新交易日期"""
    r = pd.read_sql('SELECT max(trade_date) as d FROM daily_kline', conn)
    return int(r['d'].iloc[0])


def load_data(trade_date_str):
    """加载并合并所有数据源"""
    trade_date = int(trade_date_str)
    conn = sqlite3.connect(str(DB_PATH))

    # 股票列表
    stocks = pd.read_sql('SELECT ts_code, name FROM stocks', conn)
    for p in EXCLUDE_PREFIXES:
        stocks = stocks[~stocks['ts_code'].str.startswith(p)]
    stocks = stocks[~stocks['name'].str.contains(r'\*?ST', na=False, regex=True)]
    valid_codes = set(stocks['ts_code'])

    # 日线
    kline = pd.read_sql(f"""
        SELECT ts_code, trade_date, open, high, low, close, pre_close, pct_chg, vol, amount
        FROM daily_kline WHERE trade_date >= {DATA_START} AND trade_date <= {trade_date}
        ORDER BY ts_code, trade_date
    """, conn)
    kline = kline[kline['ts_code'].isin(valid_codes)]
    kline['trade_date'] = kline['trade_date'].astype(str)

    # 基本面
    basic = pd.read_sql(f"""
        SELECT ts_code, trade_date, pe_ttm, pb, ps_ttm, total_mv
        FROM daily_basic WHERE trade_date >= {DATA_START} AND trade_date <= {trade_date}
    """, conn)
    basic['trade_date'] = basic['trade_date'].astype(str)

    # 资金流向
    funds = pd.read_sql(f"""
        SELECT ts_code, trade_date, main_pct FROM fund_flow
        WHERE trade_date >= {DATA_START} AND trade_date <= {trade_date}
    """, conn)
    funds = funds[funds['ts_code'].isin(valid_codes)]
    funds['trade_date'] = funds['trade_date'].astype(str)

    # 财务指标
    fina = pd.read_sql(f"""
        SELECT ts_code, ann_date, netprofit_yoy, op_yoy, or_yoy, roe
        FROM fina_indicator WHERE ann_date >= {DATA_START}
        ORDER BY ts_code, ann_date
    """, conn)
    fina = fina[fina['ts_code'].isin(valid_codes)]

    conn.close()

    # Alpha101 (从parquet加载)
    alpha = None
    if ALPHA_PATH.exists():
        alpha = pd.read_parquet(ALPHA_PATH).reset_index()
        alpha['trade_date'] = alpha['trade_date'].astype(str)

    return stocks, kline, basic, funds, fina, alpha


def compute_factors(kline, basic, funds, fina, trade_date_str):
    """计算当日截面因子 (Fusion20, 不含LGBM)"""
    td_str = trade_date_str

    # 财务前向填充
    fina_sorted = fina.dropna(subset=['ann_date']).sort_values(['ts_code', 'ann_date'])
    fina_sorted = fina_sorted.drop_duplicates(subset=['ts_code'], keep='last')
    fina_map = fina_sorted.set_index('ts_code')[['netprofit_yoy', 'op_yoy', 'or_yoy', 'roe']].to_dict('index')

    # 合并
    data = kline.merge(basic, on=['ts_code', 'trade_date'], how='left')
    data = data.sort_values(['ts_code', 'trade_date'])
    funds['main_pct'] = pd.to_numeric(funds['main_pct'], errors='coerce').fillna(0)
    data = data.merge(funds, on=['ts_code', 'trade_date'], how='left')

    today = data[data['trade_date'] == td_str].copy().reset_index(drop=True)
    if today.empty:
        return None

    g = data.groupby('ts_code')

    # 量价因子
    nv20 = -g['pct_chg'].transform(lambda s: s.rolling(20, min_periods=10).std())
    ma20 = g['close'].transform(lambda s: s.rolling(20, min_periods=10).mean())
    mb = -(data['close'] - ma20) / (ma20 + 1e-8)
    h20 = g['high'].transform(lambda s: s.rolling(20).max())
    c2h = data['close'] / h20
    rev_5 = g['pct_chg'].transform(lambda s: s.rolling(5, min_periods=3).mean())
    is_limit = (data['pct_chg'] >= 9.5) & (data['pre_close'] > 0)
    nozt = 1 - 2 * is_limit.groupby(data['ts_code']).transform(lambda s: s.rolling(5).max().fillna(0))
    at20 = -g['vol'].transform(lambda s: s.rolling(20).mean())
    main_pct_5d = g['main_pct'].transform(lambda s: s.rolling(5, min_periods=3).mean())

    mask_td = data['trade_date'] == td_str
    today['neg_volatility_20'] = nv20.values[mask_td]
    today['neg_ma_bias'] = (-mb.values[mask_td])
    today['close_to_high'] = c2h.values[mask_td]
    today['rev_5'] = (-rev_5.values[mask_td])
    today['no_zt_5'] = nozt.values[mask_td]
    today['avg_turnover_20'] = at20.values[mask_td]
    today['neg_pe_ttm'] = -today['pe_ttm'].clip(upper=200).fillna(0)
    today['neg_pb'] = -today['pb'].clip(upper=50).fillna(0)
    today['neg_ps_ttm'] = -today['ps_ttm'].clip(upper=50).fillna(0)
    today['neg_ln_mv'] = -np.log(today['total_mv'].clip(lower=1).fillna(1e9))
    today['main_pct'] = today['main_pct'].fillna(0)
    today['main_pct_5d'] = main_pct_5d.values[mask_td]

    # 财务因子
    for f in ['netprofit_yoy', 'op_yoy', 'or_yoy', 'roe']:
        today[f] = 0.0
    for idx, row in today.iterrows():
        code = row['ts_code']
        if code in fina_map:
            for f in ['netprofit_yoy', 'op_yoy', 'or_yoy', 'roe']:
                today.at[idx, f] = fina_map[code].get(f, 0) / 100

    return today


def apply_alpha101(today, alpha, trade_date_str):
    """合并Alpha101因子 (alpha_16/13/40/88)"""
    td_str = trade_date_str
    for f in ['alpha_16', 'alpha_13', 'alpha_40', 'alpha_88']:
        today[f] = 0.0

    if alpha is None:
        print('  [WARN] 101_core_v2.parquet 不存在, alpha因子=0')
        return

    at = alpha[alpha['trade_date'] == td_str]
    if at.empty:
        print(f'  [WARN] alpha101无 {td_str} 数据')
        return

    alpha_dict = {}
    for _, ar in at.iterrows():
        alpha_dict[ar['ts_code']] = ar

    for idx, row in today.iterrows():
        if row['ts_code'] in alpha_dict:
            ar = alpha_dict[row['ts_code']]
            for f in ['alpha_16', 'alpha_13', 'alpha_40', 'alpha_88']:
                if f in ar.index and pd.notna(ar[f]):
                    today.at[idx, f] = ar[f]


def run_lgbm_predict(today, trade_date_str):
    """LGBM推理 (优先缓存, 无缓存则实时推理)"""
    td_str = trade_date_str

    # 尝试从缓存加载
    if LGBM_PRED_PATH.exists():
        pred = pd.read_parquet(LGBM_PRED_PATH)
        pred['trade_date'] = pred['trade_date'].astype(str)
        pt = pred[pred['trade_date'] == td_str]
        if not pt.empty:
            score_col = 'lgbm_5d' if 'lgbm_5d' in pt.columns else 'lgbm_score'
            if len(pt) == len(today):
                today['lgbm_score'] = pt.set_index('ts_code').reindex(today['ts_code'])[score_col].fillna(0).values
                print(f'  LGBM: 缓存加载 ({len(today)} 只)')
                return

    # 实时推理
    if not LGBM_MODEL_PATH.exists():
        print('  [WARN] LGBM模型不存在, lgbm_score=0')
        today['lgbm_score'] = 0.0
        return

    print(f'  LGBM: 实时推理 ({len(today)} 只)...')
    model = Booster(model_file=str(LGBM_MODEL_PATH))
    X = today[FACTORS].fillna(0.0).values.astype('float32')
    scores = model.predict(X)
    today['lgbm_score'] = scores
    print(f'  LGBM: [{scores.min():.4f}, {scores.max():.4f}]')

    # 更新缓存
    if LGBM_PRED_PATH.exists():
        old = pd.read_parquet(LGBM_PRED_PATH)
        old['trade_date'] = old['trade_date'].astype(str)
        new_pred = today[['ts_code', 'trade_date', 'lgbm_score']].rename(columns={'lgbm_score': 'lgbm_5d'})
        new_pred['trade_date'] = new_pred['trade_date'].astype(str)
        combined = pd.concat([old[old['trade_date'] < td_str], new_pred], ignore_index=True)
        combined.to_parquet(LGBM_PRED_PATH, index=False)
        print(f'  LGBM: 缓存已更新 ({combined.trade_date.min()}~{combined.trade_date.max()})')


def b2_select(today, stocks_df, top_n=TOP_N):
    """B2选股: 活跃池Top500 + 21因子Z-score"""
    # 活跃池
    today = today.dropna(subset=['amount'])
    today['amount_f'] = today['amount'].astype(float)
    pool = today.nlargest(POOL_SIZE, 'amount_f')

    # Z-score合成
    sc = pd.DataFrame(index=pool.index)
    for fc in FACTORS + ['lgbm_score']:
        w = LGBM_WEIGHT if fc == 'lgbm_score' else 1
        s = pool[fc]
        z = (s - s.mean()) / (s.std() + 1e-8)
        sc[fc] = z * w

    pool['total_score'] = sc.mean(axis=1)
    pool['rank_val'] = pool['total_score'].rank(ascending=False)

    name_map = stocks_df.set_index('ts_code')['name'].to_dict()
    pool['name'] = pool['ts_code'].map(name_map)

    return pool.nsmallest(top_n, 'rank_val')


def print_results(top, trade_date_str):
    """格式化输出"""
    print()
    print('=' * 78)
    print(f'B2 爆发力选股 Top{len(top)} ({trade_date_str}) — 完整21因子 (Fusion20 + 2xLGBM)')
    print('=' * 78)
    fmt = '{:>4} {:>10} {:>8} {:>8} {:>8} {:>8} {:>8} {:>8} {:>10}'
    print(fmt.format('#', '代码', '名称', '评分', 'LGBM', 'a16', 'a13', 'a40', '成交亿'))
    print('-' * 78)
    for _, r in top.iterrows():
        pct = float(r.get('pct_chg', 0) or 0)
        amt = float(r.get('amount_f', 0) or 0) / 1e8
        lg = float(r.get('lgbm_score', 0) or 0)
        a16 = float(r.get('alpha_16', 0) or 0)
        a13 = float(r.get('alpha_13', 0) or 0)
        a40 = float(r.get('alpha_40', 0) or 0)
        print(fmt.format(int(r['rank_val']), r.ts_code, r.name, round(r.total_score, 3),
                         round(lg, 4), round(a16, 3), round(a13, 3), round(a40, 3), round(amt, 2)))

    print()
    print('>>> 推荐关注 (Top2):')
    for i, (_, r) in enumerate(top.head(2).iterrows(), 1):
        pct = float(r.get('pct_chg', 0) or 0)
        amt = float(r.get('amount_f', 0) or 0) / 1e8
        lg = float(r.get('lgbm_score', 0) or 0)
        a16v = float(r.get('alpha_16', 0) or 0)
        a13v = float(r.get('alpha_13', 0) or 0)
        a40v = float(r.get('alpha_40', 0) or 0)
        a88v = float(r.get('alpha_88', 0) or 0)
        print(f'  ★{i} {r.ts_code} {r.name}')
        print(f'     收盘 {float(r.close):.2f} | 今日 {pct:+.2f}% | 成交 {amt:.1f}亿')
        print(f'     LGBM={lg:.4f} | a16={a16v:.3f} a13={a13v:.3f} a40={a40v:.3f} a88={a88v:.3f}')


def main():
    parser = argparse.ArgumentParser(description='B2爆发力选股')
    parser.add_argument('--date', type=str, help='交易日 (YYYYMMDD), 默认最新')
    parser.add_argument('--top', type=int, default=TOP_N, help=f'输出Top N (默认{TOP_N})')
    args = parser.parse_args()

    t0 = time.time()

    # 确定日期
    if args.date:
        trade_date = args.date
    else:
        conn = sqlite3.connect(str(DB_PATH))
        trade_date = str(get_latest_trade_date(conn))
        conn.close()
        print(f'自动检测最新交易日: {trade_date}')

    print(f'\n交易日: {trade_date}')
    print(f'活跃池: Top{POOL_SIZE}成交额')
    print(f'选股数: Top{args.top}')

    # 加载数据
    print('加载数据...', flush=True)
    stocks, kline, basic, funds, fina, alpha = load_data(trade_date)
    print(f'  截面: {len(kline[kline["trade_date"]==trade_date])} 只')

    # 计算因子
    print('计算因子...', flush=True)
    today = compute_factors(kline, basic, funds, fina, trade_date)
    if today is None:
        print(f'❌ {trade_date} 无截面数据')
        sys.exit(1)

    # Alpha101
    print('Alpha101...', flush=True)
    apply_alpha101(today, alpha, trade_date)
    nza = [(today[c] != 0).sum() for c in ['alpha_16', 'alpha_13', 'alpha_40', 'alpha_88']]
    print(f'  alpha匹配: a16={nza[0]} a13={nza[1]} a40={nza[2]} a88={nza[3]}')

    # LGBM
    print('LGBM...', flush=True)
    run_lgbm_predict(today, trade_date)

    # 选股
    top = b2_select(today, stocks, top_n=args.top)
    print_results(top, trade_date)

    print(f'\n耗时: {time.time() - t0:.1f}s')


if __name__ == '__main__':
    main()
