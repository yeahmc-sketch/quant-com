#!/usr/bin/env python3
"""
B2 爆发力 — 模拟实盘交易
======================
策略: +2xLGBM Top2-20d (Fusion20 + 2倍LGBM预测分数)

核心参数:
  - TopN = 2 (两只分散，非单只满仓)
  - 持仓 = 20天 (超长周期)
  - LGBM权重 = 2x (双倍权重)
  - 无择时 (纯因子评分)
  - 仓位: 等权分配
  - NavDD8%净值回撤保护

回测表现 (2024-09 ~ 2026-04, +2xLGBM Top2-20d):
  - 无保护: +85.60%, DD 22.64%
  - NavDD8%: +106.72%, DD 11.55%, Sharpe 2.36  ← 定版方案

用法:
  # 正常执行 (最新交易日)
  python3 v74/backtest/b2_paper_trade.py

  # 补跑指定日期
  python3 v74/backtest/b2_paper_trade.py --force-date YYYYMMDD

  # 查看持仓状态
  python3 v74/backtest/b2_paper_trade.py --status

  # 仅发送邮件通知
  python3 v74/backtest/b2_paper_trade.py --notify

数据依赖:
  - data/db/market.db (日线、基本面、资金流向、财务指标)
  - output/v74/multi_factor/lgbm_pred_lgbm_5d.parquet (LGBM预测分数)
"""
import sys, json, os, gc, warnings
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
STATE_DIR = PROJECT_DIR / "output" / "v74" / "portfolio"
STATE_FILE = STATE_DIR / "b2_trade_state.json"
STATE_DIR.mkdir(parents=True, exist_ok=True)

# ===== B2 策略参数 =====
INIT_CASH = 50_000        # 初始资金
MAX_POS = 2               # 2只分散持仓（非Top1满仓，回测Top2-20d: +83% Sharpe 1.26）
HOLD_DAYS = 20            # 20天持仓
TOP_N = 2                 # 选股数
POOL_SIZE = 500           # 活跃池大小
COST_BUY = 0.00035        # 买入佣金+过户费 万3.5
COST_SELL = 0.00135       # 卖出佣金+印花税+过户费 万13.5
NAV_DRAWDOWN_STOP = 8.0   # 净值回撤保护：总权益从峰值回调≥8%时清仓

# LGBM预测路径
LGBM_PRED_PATH = PROJECT_DIR / "output" / "v74" / "multi_factor" / "lgbm_pred_lgbm_5d.parquet"
LGBM_MODEL_PATH = PROJECT_DIR / "output" / "v74" / "multi_factor" / "lgbm_model_v1.txt"

# 数据范围
LOOKBACK_DAYS = 250       # 因子计算回看天数
DATA_START = 20240101      # 数据起始日 (DB查询用)

# 排除前缀
EXCLUDE_PREFIXES = ['688', '30', '8', '4', '920', 'bj']

# Fusion20因子列表 (唯一列名)
FACTOR_COLS = [
    'neg_volatility_20', 'neg_ma_bias', 'close_to_high', 'rev_5',
    'neg_pe_ttm', 'neg_pb', 'neg_ps_ttm', 'neg_ln_mv',
    'netprofit_yoy', 'op_yoy', 'or_yoy', 'roe',
    'avg_turnover_20', 'no_zt_5',
    'alpha_16', 'alpha_13', 'alpha_40', 'alpha_88',
    'main_pct', 'main_pct_5d',
    'lgbm_score',
]
LGBM_WEIGHT = 2  # 2x LGBM 权重


# ============================================================
# Alpha101 轻量计算（现场算，不依赖缓存）
# ============================================================

def _alpha13(data, td_str):
    """-rank(covariance(rank(close), rank(volume), 5))"""
    data = data.sort_values(['ts_code', 'trade_date']).copy()
    g = data.groupby('ts_code')
    rank_close = g['close'].transform(lambda s: s.rank(pct=True))
    rank_vol = g['vol'].transform(lambda s: s.rank(pct=True))
    # covariance(close_rank, vol_rank, 5) per group
    pairs = pd.DataFrame({'rc': rank_close, 'rv': rank_vol}, index=data.index)
    def _cov5(g):
        return g['rc'].rolling(5, min_periods=3).cov(g['rv'])
    cov5 = pairs.groupby(data['ts_code'], group_keys=False).apply(_cov5)
    # cross-sectional rank on trade_date
    result = pd.Series(np.nan, index=data.index)
    mask = data['trade_date'] == td_str
    today_vals = cov5[mask]
    ranked = today_vals.rank(pct=True)
    result[mask] = -ranked.values
    return result

def _alpha16(data, td_str):
    """-rank(covariance(rank(high), rank(volume), 5))"""
    data = data.sort_values(['ts_code', 'trade_date']).copy()
    g = data.groupby('ts_code')
    rank_high = g['high'].transform(lambda s: s.rank(pct=True))
    rank_vol = g['vol'].transform(lambda s: s.rank(pct=True))
    pairs = pd.DataFrame({'rh': rank_high, 'rv': rank_vol}, index=data.index)
    def _cov5(g):
        return g['rh'].rolling(5, min_periods=3).cov(g['rv'])
    cov5 = pairs.groupby(data['ts_code'], group_keys=False).apply(_cov5)
    result = pd.Series(np.nan, index=data.index)
    mask = data['trade_date'] == td_str
    today_vals = cov5[mask]
    ranked = today_vals.rank(pct=True)
    result[mask] = -ranked.values
    return result

def _alpha40(data, td_str):
    """-rank(stddev(high,10)) * correlation(high, volume, 10)"""
    data = data.sort_values(['ts_code', 'trade_date']).copy()
    g = data.groupby('ts_code')
    std10 = g['high'].transform(lambda s: s.rolling(10, min_periods=5).std())
    pairs = pd.DataFrame({'h': data['high'], 'v': data['vol']}, index=data.index)
    def _corr10(g):
        return g['h'].rolling(10, min_periods=5).corr(g['v'])
    corr10 = pairs.groupby(data['ts_code'], group_keys=False).apply(_corr10)
    # cross-sectional: rank(std10) * corr10, then negate
    result = pd.Series(np.nan, index=data.index)
    mask = data['trade_date'] == td_str
    today_std = std10[mask].rank(pct=True).values
    today_corr = corr10[mask].values
    combined = -(today_std * today_corr)
    result[mask] = combined
    return result

def _alpha88(data, td_str):
    """min(rank(decay_linear((rank(open)+rank(low))-(rank(high)+rank(close)), 8)),
             ts_rank(decay_linear(correlation(ts_rank(close,8), ts_rank(adv60,21), 8), 7), 3))"""
    data = data.sort_values(['ts_code', 'trade_date']).copy()
    g = data.groupby('ts_code')

    # Part 1: rank(decay_linear((rank(open)+rank(low))-(rank(high)+rank(close)), 8))
    rank_open = g['open'].transform(lambda s: s.rank(pct=True))
    rank_low = g['low'].transform(lambda s: s.rank(pct=True))
    rank_high = g['high'].transform(lambda s: s.rank(pct=True))
    rank_close = g['close'].transform(lambda s: s.rank(pct=True))
    ol_minus_hc = (rank_open + rank_low) - (rank_high + rank_close)
    # decay_linear(ol_minus_hc, 8)
    w = np.arange(1, 9, dtype=float); w /= w.sum()
    decay_olhc = g['open'].transform(lambda s: np.nan)  # placeholder
    decay_vals = ol_minus_hc.groupby(data['ts_code'], group_keys=False).apply(
        lambda s: s.rolling(8, min_periods=4).apply(lambda x: np.dot(x.values, w[:len(x)]) if len(x) == 8 else np.nan, raw=False)
    )
    left = decay_vals.rank(pct=True)

    # Part 2: ts_rank(decay_linear(correlation(ts_rank(close,8), ts_rank(adv60,21), 8), 7), 3)
    tsrank_close = g['close'].transform(lambda s: s.rolling(8, min_periods=4).rank(pct=True))
    adv60 = g['vol'].transform(lambda s: s.rolling(60, min_periods=30).mean())
    tsrank_adv = g['vol'].transform(lambda s: np.nan)  # placeholder
    tsrank_adv_vals = adv60.groupby(data['ts_code'], group_keys=False).apply(
        lambda s: s.rolling(21, min_periods=10).rank(pct=True)
    )
    corr_pairs = pd.DataFrame({'rc': tsrank_close, 'ra': tsrank_adv_vals}, index=data.index)
    corr_vals = corr_pairs.groupby(data['ts_code'], group_keys=False).apply(
        lambda g: g['rc'].rolling(8, min_periods=4).corr(g['ra'])
    )
    decay_corr = corr_vals.groupby(data['ts_code'], group_keys=False).apply(
        lambda s: s.rolling(7, min_periods=4).apply(lambda x: np.dot(x.values, w[:len(x)]) if len(x) == 7 else np.nan, raw=False)
    )
    right = decay_corr.groupby(data['ts_code'], group_keys=False).apply(
        lambda s: s.rolling(3, min_periods=2).rank(pct=True)
    )

    result = pd.Series(np.nan, index=data.index)
    mask = data['trade_date'] == td_str
    result[mask] = pd.concat([left[mask], right[mask]], axis=1).min(axis=1).values
    return result


# ============================================================
# 数据加载
# ============================================================

def load_today_data(trade_date_str):
    """加载当日截面数据（含因子计算所需的lookback期）"""
    trade_date = int(trade_date_str)
    start_date = DATA_START

    conn = sqlite3.connect(str(DB_PATH))

    # 1. 股票列表
    stocks = pd.read_sql('SELECT ts_code, name FROM stocks', conn)
    for p in EXCLUDE_PREFIXES:
        stocks = stocks[~stocks['ts_code'].str.startswith(p)]
    stocks = stocks[~stocks['name'].str.contains(r'\*?ST', na=False, regex=True)]
    valid_codes = set(stocks['ts_code'])

    # 2. 日线
    kline = pd.read_sql(f"""
        SELECT ts_code, trade_date, open, high, low, close, pre_close, pct_chg, vol, amount
        FROM daily_kline
        WHERE trade_date >= {start_date} AND trade_date <= {trade_date}
        ORDER BY ts_code, trade_date
    """, conn)
    kline = kline[kline['ts_code'].isin(valid_codes)]
    kline['trade_date'] = kline['trade_date'].astype(str)

    # 3. 基本面
    basic = pd.read_sql(f"""
        SELECT ts_code, trade_date, pe_ttm, pb, ps_ttm, total_mv
        FROM daily_basic
        WHERE trade_date >= {start_date} AND trade_date <= {trade_date}
    """, conn)
    basic['trade_date'] = basic['trade_date'].astype(str)

    # 4. 资金流向
    funds = pd.read_sql(f"""
        SELECT ts_code, trade_date, main_pct
        FROM fund_flow
        WHERE trade_date >= {start_date} AND trade_date <= {trade_date}
    """, conn)
    funds = funds[funds['ts_code'].isin(valid_codes)]
    funds['trade_date'] = funds['trade_date'].astype(str)

    # 5. 财务指标 (前向填充)
    fina = pd.read_sql(f"""
        SELECT ts_code, ann_date, netprofit_yoy, op_yoy, or_yoy, roe
        FROM fina_indicator
        WHERE ann_date >= {start_date}
        ORDER BY ts_code, ann_date
    """, conn)
    fina = fina[fina['ts_code'].isin(valid_codes)]

    conn.close()

    return kline, basic, funds, fina


def compute_factors(kline, basic, funds, fina, trade_date_str):
    """计算当日截面因子 (Fusion20 全部现场计算) + Alpha101 现场计算"""
    trade_date_int = int(trade_date_str)
    td_str = trade_date_str

    # === 前向填充财务指标 ===
    fina_sorted = fina.dropna(subset=['ann_date']).sort_values(['ts_code', 'ann_date'])
    fina_sorted = fina_sorted.drop_duplicates(subset=['ts_code'], keep='last')
    fina_map = fina_sorted.set_index('ts_code')[['netprofit_yoy', 'op_yoy', 'or_yoy', 'roe']].to_dict('index')

    # === 合并K线+基本面 ===
    data = kline.merge(basic, on=['ts_code', 'trade_date'], how='left')
    data = data.sort_values(['ts_code', 'trade_date'])

    # === 合并资金流向 ===
    funds['main_pct'] = pd.to_numeric(funds['main_pct'], errors='coerce').fillna(0)
    data = data.merge(funds, on=['ts_code', 'trade_date'], how='left')

    # === 取当日截面 ===
    today = data[data['trade_date'] == td_str].copy()
    if today.empty:
        return None

    # === 计算因子 ===
    g = data.groupby('ts_code')

    # MF因子
    nv20 = -g['pct_chg'].transform(lambda s: s.rolling(20, min_periods=10).std())
    ma20 = g['close'].transform(lambda s: s.rolling(20, min_periods=10).mean())
    mb = -(data['close'] - ma20) / (ma20 + 1e-8)
    h20 = g['high'].transform(lambda s: s.rolling(20).max())
    c2h = data['close'] / h20
    rev_5 = g['pct_chg'].transform(lambda s: s.rolling(5, min_periods=3).mean())
    is_limit = (data['pct_chg'] >= 9.5) & (data['pre_close'] > 0)
    nozt = 1 - 2 * is_limit.groupby(data['ts_code']).transform(lambda s: s.rolling(5).max().fillna(0))
    at20 = -g['vol'].transform(lambda s: s.rolling(20).mean())
    # main_pct_5d (5日资金趋势)
    main_pct_5d = g['main_pct'].transform(lambda s: s.rolling(5, min_periods=3).mean())

    today = today.reset_index(drop=True)
    today['neg_volatility_20'] = nv20.values[data['trade_date'] == td_str]
    today['neg_ma_bias'] = (-mb.values[data['trade_date'] == td_str])
    today['close_to_high'] = c2h.values[data['trade_date'] == td_str]
    today['rev_5'] = (-rev_5.values[data['trade_date'] == td_str])
    today['no_zt_5'] = nozt.values[data['trade_date'] == td_str]
    today['avg_turnover_20'] = at20.values[data['trade_date'] == td_str]

    today['neg_pe_ttm'] = -today['pe_ttm'].clip(upper=200).fillna(0)
    today['neg_pb'] = -today['pb'].clip(upper=50).fillna(0)
    today['neg_ps_ttm'] = -today['ps_ttm'].clip(upper=50).fillna(0)
    today['neg_ln_mv'] = -np.log(today['total_mv'].clip(lower=1).fillna(1e9))
    today['main_pct'] = today['main_pct'].fillna(0)
    # 5日资金趋势
    today['main_pct_5d'] = main_pct_5d.values[data['trade_date'] == td_str]

    # === 财务因子 ===
    today['netprofit_yoy'] = 0.0
    today['op_yoy'] = 0.0
    today['or_yoy'] = 0.0
    today['roe'] = 0.0
    for idx, row in today.iterrows():
        code = row['ts_code']
        if code in fina_map:
            today.at[idx, 'netprofit_yoy'] = fina_map[code].get('netprofit_yoy', 0) / 100
            today.at[idx, 'op_yoy'] = fina_map[code].get('op_yoy', 0) / 100
            today.at[idx, 'or_yoy'] = fina_map[code].get('or_yoy', 0) / 100
            today.at[idx, 'roe'] = fina_map[code].get('roe', 0) / 100

    # === α101因子 (现场计算，不依赖缓存) ===
    print("  计算Alpha101因子 (现场)...", flush=True)
    mask_td = (data['trade_date'] == td_str).values
    a16 = pd.Series(_alpha16(data, td_str).values[mask_td]).fillna(0)
    a13 = pd.Series(_alpha13(data, td_str).values[mask_td]).fillna(0)
    a40 = pd.Series(_alpha40(data, td_str).values[mask_td]).fillna(0)
    a88 = pd.Series(_alpha88(data, td_str).values[mask_td]).fillna(0)
    today['alpha_16'] = a16.values
    today['alpha_13'] = a13.values
    today['alpha_40'] = a40.values
    today['alpha_88'] = a88.values
    nza = {c: (today[c] != 0).sum() for c in ['alpha_16', 'alpha_13', 'alpha_40', 'alpha_88']}
    print(f"    alpha非零: a16={nza['alpha_16']} a13={nza['alpha_13']} a40={nza['alpha_40']} a88={nza['alpha_88']}")

    # === LGBM预测分数 (现场推理，不依赖缓存) ===
    today['lgbm_score'] = 0.0
    if LGBM_MODEL_PATH.exists():
        print("  LGBM推理 (现场)...", flush=True)
        model = Booster(model_file=str(LGBM_MODEL_PATH))
        feat_cols = [c for c in FACTOR_COLS if c != 'lgbm_score']
        X = today[feat_cols].fillna(0.0).values.astype('float32')
        scores = model.predict(X)
        today['lgbm_score'] = scores
        print(f"    LGBM范围: [{scores.min():.4f}, {scores.max():.4f}]")

        # 更新缓存
        try:
            if LGBM_PRED_PATH.exists():
                old = pd.read_parquet(LGBM_PRED_PATH)
                old['trade_date'] = old['trade_date'].astype(str)
                new_pred = today[['ts_code', 'trade_date', 'lgbm_score']].rename(columns={'lgbm_score': 'lgbm_5d'}).copy()
                new_pred['trade_date'] = td_str
                combined = pd.concat([old[old['trade_date'] < td_str], new_pred], ignore_index=True)
                combined.to_parquet(LGBM_PRED_PATH, index=False)
                print(f"    LGBM缓存已更新")
        except Exception as e:
            print(f"    LGBM缓存更新失败: {e}")
    else:
        print(f"  ⚠️ LGBM模型不存在: {LGBM_MODEL_PATH}")

    return today


# ============================================================
# 状态管理
# ============================================================

def load_state():
    """加载当前持仓状态"""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        'strategy': 'B2 爆发力',
        'cash': INIT_CASH,
        'init_cash': INIT_CASH,
        'positions': {},
        'trades': [],
        'equity_curve': [],
        'last_date': None,
        'peak_nav': INIT_CASH,           # 历史最高净值
        'drawdown_protection': False,     # 是否处于回撤保护期
    }


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_stock_name(ts_code):
    """从DB获取股票名称"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute("SELECT name FROM stocks WHERE ts_code=?", (ts_code,))
        r = cur.fetchone()
        conn.close()
        return r[0] if r else ''
    except:
        return ''


# ============================================================
# 交易逻辑
# ============================================================

def run_daily(trade_date_str, force=False):
    """执行单日模拟交易"""
    state = load_state()

    # 去重检查
    if state.get('last_date') == trade_date_str and not force:
        print(f"  {trade_date_str} 已处理，跳过")
        return

    # 检查有没有事做：轻量级净值查询 + 判断是否到期/回撤风险
    positions = state.get('positions', {})
    cash = state['cash']
    peak_nav = state.get('peak_nav', INIT_CASH)
    protection = state.get('drawdown_protection', False)

    current_nav = cash
    has_expiring = False
    has_navdd_risk = False
    td = datetime.strptime(trade_date_str, '%Y%m%d')

    if positions:
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            for sym, p in positions.items():
                cur.execute("SELECT close FROM daily_kline WHERE ts_code=? AND trade_date=?",
                           (sym, trade_date_str))
                row = cur.fetchone()
                cl = float(row[0]) if row and row[0] else p.get('entry_price', 0)
                current_nav += p['shares'] * cl
                ed = datetime.strptime(p['entry_date'], '%Y%m%d')
                if (td - ed).days >= HOLD_DAYS:
                    has_expiring = True
            conn.close()
        except:
            current_nav = 0

    if current_nav > peak_nav:
        peak_nav = current_nav
        state['peak_nav'] = peak_nav
    dd_from_peak = (peak_nav - current_nav) / peak_nav * 100 if peak_nav > 0 else 0
    if dd_from_peak >= NAV_DRAWDOWN_STOP and positions:
        has_navdd_risk = True

    has_slots = len(positions) < MAX_POS
    if not force and not has_expiring and not has_navdd_risk and not has_slots and not protection:
        state['equity_curve'].append({
            'date': trade_date_str, 'nav': round(current_nav, 2),
            'cash': round(cash, 2), 'pos_count': len(positions),
        })
        state['last_date'] = trade_date_str
        save_state(state)
        print(f"  \u23ed\ufe0f 持仓满/未到期/无回撤风险，跳过计算 (\u00a5{current_nav:,.0f})")
        return

    print(f"\n===== B2 爆发力 {trade_date_str} =====")
    print(f"现金: ¥{cash:,.2f}")

    # 加载数据
    kline, basic, funds, fina = load_today_data(trade_date_str)
    today = compute_factors(kline, basic, funds, fina, trade_date_str)
    if today is None:
        print(f"  ⚠️ {trade_date_str} 无数据，跳过")
        return

    print(f"  截面股票数: {len(today)}")

    # --- 判断是否调仓日 ---
    is_rebalance = True
    if positions:
        first_entry = min(datetime.strptime(p['entry_date'], '%Y%m%d') for p in positions.values())
        try:
            conn2 = sqlite3.connect(str(DB_PATH))
            td_count = pd.read_sql(f"""
                SELECT COUNT(DISTINCT trade_date) FROM daily_kline
                WHERE trade_date >= '{first_entry.strftime('%Y%m%d')}'
                  AND trade_date <= '{trade_date_str}'
            """, conn2)
            conn2.close()
            days_passed = int(td_count.iloc[0, 0])
            is_rebalance = days_passed > 0 and days_passed % HOLD_DAYS == 0
        except:
            is_rebalance = True

    # 1) NavDD8%保护：净值回撤≥8%时全部清仓
    sold_today = set()
    if dd_from_peak >= NAV_DRAWDOWN_STOP and positions:
        print(f"  ⚠️ 净值回撤 {dd_from_peak:.1f}% ≥ {NAV_DRAWDOWN_STOP}%，触发保护清仓")
        for sym in list(positions.keys()):
            p = positions[sym]
            row = today[today['ts_code'] == sym]
            if not row.empty:
                close = float(row.iloc[0]['close'])
                ret = (close - p['entry_price']) / p['entry_price'] * 100
                proceeds = p['shares'] * close * (1 - COST_SELL)
                cash += proceeds
                state['trades'].append({
                    'code': sym, 'name': p.get('name', ''),
                    'entry_date': p['entry_date'], 'exit_date': trade_date_str,
                    'entry_price': p['entry_price'], 'exit_price': close,
                    'shares': p['shares'], 'return_pct': round(ret, 2),
                    'sell_reason': '净值回撤保护',
                })
                print(f"  🔒 保护清仓 {sym}({p.get('name','')}): {ret:+.2f}%")
            sold_today.add(sym)
            del positions[sym]
        protection = True
        state['drawdown_protection'] = True

    # 2) 到期卖出（只在调仓日）
    if is_rebalance and positions:
        for sym in list(positions.keys()):
            p = positions[sym]
            entry_date = p['entry_date']
            ed = datetime.strptime(entry_date, '%Y%m%d')
            td = datetime.strptime(trade_date_str, '%Y%m%d')
            hold_days = (td - ed).days

            if hold_days >= HOLD_DAYS:
                row = today[today['ts_code'] == sym]
                if not row.empty:
                    close = float(row.iloc[0]['close'])
                    entry_price = p['entry_price']
                    shares = p['shares']
                    ret = (close - entry_price) / entry_price * 100
                    proceeds = shares * close * (1 - COST_SELL)
                    cash += proceeds
                    state['trades'].append({
                        'code': sym,
                        'name': p.get('name', ''),
                        'entry_date': entry_date,
                        'exit_date': trade_date_str,
                        'entry_price': entry_price,
                        'exit_price': close,
                        'shares': shares,
                        'return_pct': round(ret, 2),
                    })
                    print(f"  ✅ 卖出 {sym}({p.get('name','')}): {ret:+.2f}% | 持仓{hold_days}天 | ¥{proceeds:,.0f}")
                    sold_today.add(sym)
                    del positions[sym]

    # 调仓日解除回撤保护
    if is_rebalance and protection:
        protection = False
        state['drawdown_protection'] = False
        print(f"  调仓日，解除净值回撤保护")

    # --- 买入（在保护期外 + 有空位）---
    pool = today.nlargest(POOL_SIZE, 'amount') if 'amount' in today.columns else today

    # 评分因子: Fusion20 + 2x LGBM
    # (注意: lgbm_score只出现一次在列中，但LGBM_WEIGHT=2实现双倍权重)
    scoring_cols = [c for c in FACTOR_COLS if c in pool.columns]
    valid_scoring = []
    for c in scoring_cols:
        if pool[c].notna().sum() > 10:
            valid_scoring.append(c)

    scores = pool[valid_scoring].fillna(0).copy()

    # Z-score归一化（处理Series/DataFrame歧义）
    for c in valid_scoring:
        vals = scores[c]
        mean_val = vals.mean()
        std_val = vals.std()
        if isinstance(std_val, (np.ndarray, pd.Series)):
            std_val = float(std_val.max())
        if pd.isna(std_val) or std_val <= 1e-8:
            scores[c] = 0.0
        else:
            scores[c] = (vals - mean_val) / std_val

    # 均值得分（LGBM双倍权重 = 在平均时重复计算）
    if 'lgbm_score' in valid_scoring and LGBM_WEIGHT > 1:
        lgbm_col = scores['lgbm_score'].values
        total_sum = scores[valid_scoring].sum(axis=1).values + lgbm_col * (LGBM_WEIGHT - 1)
        total_count = len(valid_scoring) + (LGBM_WEIGHT - 1)
        pool['score'] = total_sum / total_count
    else:
        pool['score'] = scores.mean(axis=1)

    # 选Top2 (排除已持仓 + 今日已卖出)
    exclude = set(positions.keys()) | sold_today
    candidates = pool[~pool['ts_code'].isin(exclude)]
    selected = candidates.nlargest(TOP_N, 'score')

    # 买入（只在非保护期 + 有空位）
    slots = MAX_POS - len(positions)
    if not protection and slots > 0 and not selected.empty:
        per_stock = cash / slots
        for _, row in selected.head(slots).iterrows():
            sym = row['ts_code']
            close = float(row['close'])
            if close <= 0:
                continue
            shares = int(per_stock / close / 100) * 100
            if shares < 100:
                continue
            cost = shares * close * (1 + COST_BUY)
            if cost <= cash:
                cash -= cost
                stock_name = get_stock_name(sym)
                positions[sym] = {
                    'entry_date': trade_date_str,
                    'entry_price': close,
                    'shares': shares,
                    'name': stock_name,
                }
                print(f"  🔴 买入 {sym}({stock_name}): ¥{close:.2f} × {shares}股 = ¥{cost:,.0f}  | 评分: {row['score']:.4f}")

    # --- 记录净值 ---
    nav = cash
    for sym, p in positions.items():
        row = today[today['ts_code'] == sym]
        if not row.empty:
            nav += p['shares'] * float(row.iloc[0]['close'])

    if nav > peak_nav:
        peak_nav = nav
        state['peak_nav'] = peak_nav

    state['cash'] = cash
    state['positions'] = positions
    state['last_date'] = trade_date_str
    state['equity_curve'].append({
        'date': trade_date_str,
        'nav': round(nav, 2),
        'cash': round(cash, 2),
        'pos_count': len(positions),
    })

    save_state(state)
    total_ret = (nav - INIT_CASH) / INIT_CASH * 100
    dd_now = (peak_nav - nav) / peak_nav * 100 if peak_nav > 0 else 0
    prot_flag = ' 🔒保护中' if protection else ''
    print(f"  净值: ¥{nav:,.0f} | 持仓: {len(positions)}只 | 现金: ¥{cash:,.0f}")
    print(f"  总收益: {total_ret:+.1f}% | 峰值回撤: {dd_now:.1f}%{prot_flag}")


# ============================================================
# 状态查看
# ============================================================

def show_status():
    """显示当前持仓和收益"""
    state = load_state()
    eq = state.get('equity_curve', [])
    positions = state.get('positions', {})
    cash = state.get('cash', INIT_CASH)
    init_cash = state.get('init_cash', INIT_CASH)

    print(f"\n{'='*50}")
    print(f"  B2 爆发力 模拟交易状态")
    print(f"{'='*50}")
    print(f"  策略: {state.get('strategy', 'B2 爆发力')}")
    print(f"  参数: Top{TOP_N} · {HOLD_DAYS}天持仓 · +{LGBM_WEIGHT}xLGBM · 无择时")
    print(f"  初始资金: ¥{init_cash:,}")
    print(f"  当前现金: ¥{cash:,.0f}")
    print(f"  持仓: {len(positions)}只")
    print(f"  总交易: {len(state.get('trades', []))}笔")
    print(f"  最后更新: {state.get('last_date', '无')}")

    if eq:
        latest = eq[-1]
        nav = latest['nav']
        ret = (nav - init_cash) / init_cash * 100
        print(f"  最新净值: ¥{nav:,.0f} ({ret:+.1f}%)")

        # 持仓明细
        today_s = state.get('last_date', '')
        if positions and today_s:
            print(f"\n  --- 当前持仓 ---")
            for sym, p in sorted(positions.items()):
                ed = datetime.strptime(p['entry_date'], '%Y%m%d')
                td = datetime.strptime(today_s, '%Y%m%d')
                hd = (td - ed).days
                ep = p['entry_price']
                name = p.get('name', sym)
                remaining = max(0, HOLD_DAYS - hd)
                print(f"  {sym}({name}): ¥{ep:.2f} × {p['shares']}股 | 持仓{hd}天 (到期还需{remaining}天)")

    print(f"{'='*50}")


# ============================================================
# 补跑
# ============================================================

def backfill(start_date, end_date):
    """补跑一段历史区间"""
    conn = sqlite3.connect(str(DB_PATH))
    trading_days = pd.read_sql(f"""
        SELECT DISTINCT trade_date FROM daily_kline
        WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        ORDER BY trade_date
    """, conn)
    conn.close()

    dates = trading_days['trade_date'].tolist()
    print(f"B2 补跑 {len(dates)} 个交易日: {dates[0]} ~ {dates[-1]}")

    for d in dates:
        run_daily(d, force=True)


# ============================================================
# 邮件通知
# ============================================================

def send_email_notification():
    """发送HTML格式的持仓报告邮件"""
    state = load_state()
    eq = state.get('equity_curve', [])
    positions = state.get('positions', {})
    trades = state.get('trades', [])
    cash = state.get('cash', INIT_CASH)
    init_cash = state.get('init_cash', INIT_CASH)
    last_date = state.get('last_date', '')
    nav = eq[-1]['nav'] if eq else cash
    total_ret = (nav - init_cash) / init_cash * 100
    win_trades = [t for t in trades if t.get('return_pct', 0) > 0]
    win_rate = len(win_trades) / len(trades) * 100 if trades else 0
    avg_ret = sum(t.get('return_pct', 0) for t in trades) / len(trades) if trades else 0
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    subj_str = datetime.now().strftime('%m-%d')

    # 大盘数据
    hs300_10d_ret = 'N/A'
    hs300_close_val = 'N/A'
    try:
        conn = sqlite3.connect(str(DB_PATH))
        idx = pd.read_sql("""
            SELECT trade_date, close FROM v9_index_daily
            WHERE ts_code='000300.SH' ORDER BY trade_date DESC LIMIT 12
        """, conn)
        conn.close()
        if len(idx) >= 2:
            idx = idx.sort_values('trade_date').reset_index(drop=True)
            idx_10d = idx.tail(10)
            if len(idx_10d) >= 2:
                hs300_last = float(idx_10d.iloc[-1]['close'])
                hs300_first = float(idx_10d.iloc[0]['close'])
                hs300_10d_ret = f'{(hs300_last - hs300_first) / hs300_first * 100:+.2f}%'
                hs300_close_val = f'{hs300_last:,.2f}'
    except:
        pass

    # 大盘涨跌幅颜色
    hs300_cls = ''
    if isinstance(hs300_10d_ret, str) and hs300_10d_ret.startswith('+'):
        hs300_cls = 'stat-up'
    elif isinstance(hs300_10d_ret, str) and hs300_10d_ret.startswith('-'):
        hs300_cls = 'stat-down'

    # 持仓列表
    pos_rows = ''
    for sym, p in sorted(positions.items()):
        ed = datetime.strptime(p['entry_date'], '%Y%m%d')
        td = datetime.strptime(last_date, '%Y%m%d') if last_date else datetime.now()
        hd = (td - ed).days
        ep = p['entry_price']
        name = p.get('name', sym)
        pnl = 0
        cls = 'stat-up'
        pnl_str = '—'
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            cur.execute("SELECT close FROM daily_kline WHERE ts_code=? AND trade_date=?",
                       (sym, last_date))
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                cp = float(row[0])
                pnl = (cp - ep) / ep * 100
                pnl_str = f'{pnl:+.2f}%'
                cls = 'stat-up' if pnl >= 0 else 'stat-down'
        except:
            pass
        pos_rows += f'''
                <tr>
                    <td><b>{name}</b><br><small>{sym}</small></td>
                    <td>{hd}/{HOLD_DAYS}天</td>
                    <td>¥{ep:.2f}</td>
                    <td class="{cls}">{pnl_str}</td>
                    <td>{p["shares"]}</td>
                </tr>'''

    # 今日交易
    today_trades = [t for t in trades if t.get('exit_date') == last_date]
    trade_rows = ''
    for t in today_trades:
        cls = 'stat-up' if t['return_pct'] >= 0 else 'stat-down'
        trade_rows += f'''
        <div class="trade-item">
            <b>{t["code"]}({t.get("name","")})</b> 卖出 <span class="{cls}">{t["return_pct"]:+.2f}%</span>
            （持有至{t["exit_date"]}）
        </div>'''

    html = f'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
.card {{ background: white; border-radius: 12px; padding: 20px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
.card-title {{ font-size: 16px; font-weight: 600; color: #333; margin-bottom: 12px; border-left: 4px solid #e65100; padding-left: 10px; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
.stat-box {{ background: #f8f9fa; border-radius: 8px; padding: 12px; text-align: center; }}
.stat-val {{ font-size: 20px; font-weight: 700; color: #333; }}
.stat-label {{ font-size: 12px; color: #666; margin-top: 4px; }}
.stat-up {{ color: #e53935; }}
.stat-down {{ color: #43a047; }}
.data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.data-table th {{ background: #f8f9fa; padding: 8px; text-align: left; border-bottom: 2px solid #eee; }}
.data-table td {{ padding: 8px; border-bottom: 1px solid #f0f0f0; }}
.trade-item {{ background: #f8f9fa; border-radius: 8px; padding: 10px; margin-bottom: 8px; font-size: 13px; }}
.param {{ font-size: 12px; color: #666; line-height: 1.8; }}
.b2-badge {{ display: inline-block; background: #e65100; color: white; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
</style>
</head>
<body>
<div class="card">
<div class="card-title">🌏 今日大盘</div>
<div class="grid">
<div class="stat-box"><div class="stat-val {hs300_cls}">{hs300_10d_ret}</div><div class="stat-label">沪深300近10日</div></div>
<div class="stat-box"><div class="stat-val">{hs300_close_val}</div><div class="stat-label">收盘点位</div></div>
</div>
</div>

<div class="card">
<div class="card-title"><span class="b2-badge">B2 爆发力</span> 📊 账户</div>
<div class="grid">
<div class="stat-box"><div class="stat-val {"stat-up" if total_ret>=0 else "stat-down"}">{total_ret:+.2f}%</div><div class="stat-label">累计收益</div></div>
<div class="stat-box"><div class="stat-val">¥{nav:,.0f}</div><div class="stat-label">总权益</div></div>
<div class="stat-box"><div class="stat-val">{win_rate:.0f}%</div><div class="stat-label">胜率</div></div>
<div class="stat-box"><div class="stat-val">{avg_ret:+.2f}%</div><div class="stat-label">笔均收益</div></div>
</div>
<div class="param">策略: +2xLGBM Top1-20d | 初始: ¥{init_cash:,} | 现金: ¥{cash:,.0f} | 交易: {len(trades)}笔</div>
</div>

<div class="card">
<div class="card-title">💼 当前持仓</div>
<table class="data-table">
<thead><tr><th>代码</th><th>持仓</th><th>买入价</th><th>浮盈</th><th>股数</th></tr></thead>
<tbody>{pos_rows if pos_rows else '<tr><td colspan="5" style="text-align:center;color:#999;">空仓</td></tr>'}</tbody>
</table>
</div>
{"<div class='card'><div class='card-title'>📋 今日交易</div>" + trade_rows + "</div>" if trade_rows else ""}

<div class="card" style="text-align:center;color:#999;font-size:11px;">
B2 爆发力 · +2xLGBM Top1-20d · 无择时 · 满仓单只<br>
{now_str}
</div>
</body>
</html>'''

    # 发送邮件
    sent = False
    try:
        email_txt = f'''From: "B2 爆发力" <18313835@qq.com>
To: 18313835@qq.com
Subject: B2 爆发力 持仓报告 ({subj_str})
MIME-Version: 1.0
Content-Type: text/html; charset="utf-8"

{html}'''
        import tempfile, subprocess
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        tmp.write(email_txt)
        tmp.close()
        ret = subprocess.run([
            'curl', '--url', 'smtps://smtp.qq.com:465',
            '--ssl-reqd', '--mail-from', '18313835@qq.com',
            '--mail-rcpt', '18313835@qq.com',
            '--user', '18313835@qq.com:ngrzdzjuhwfnbgbh',
            '--login-options', 'AUTH=LOGIN',
            '--upload-file', tmp.name, '--silent',
        ], capture_output=True, timeout=20)
        os.unlink(tmp.name)
        sent = ret.returncode == 0
    except Exception as e:
        print(f"  邮件发送异常: {e}")

    if sent:
        print(f'  📧 邮件已发送 ({total_ret:+.2f}%)')
    else:
        print(f'  📧 邮件发送失败')


# ============================================================
# Main
# ============================================================

if __name__ == '__main__':
    if '--status' in sys.argv:
        show_status()
    elif '--notify' in sys.argv:
        send_email_notification()
    elif '--pick' in sys.argv:
        idx = sys.argv.index('--pick')
        date_str = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else datetime.now().strftime('%Y%m%d')
        kline, basic, funds, fina = load_today_data(date_str)
        today = compute_factors(kline, basic, funds, fina, date_str)
        if today is None:
            print(f"  {date_str} 无数据"); sys.exit(1)
        pool = today.nlargest(POOL_SIZE, 'amount') if 'amount' in today.columns else today
        scoring_cols = [c for c in FACTOR_COLS if c in pool.columns and pool[c].notna().sum() > 10]
        scores = pool[scoring_cols].fillna(0).copy()
        for c in scoring_cols:
            vals = scores[c]; m = vals.mean(); s = float(vals.std())
            scores[c] = 0.0 if (pd.isna(s) or s <= 1e-8) else (vals - m) / s
        if 'lgbm_score' in scoring_cols and LGBM_WEIGHT > 1:
            lc = scores['lgbm_score'].values
            pool['score'] = (scores[scoring_cols].sum(axis=1).values + lc * (LGBM_WEIGHT - 1)) / (len(scoring_cols) + LGBM_WEIGHT - 1)
        else:
            pool['score'] = scores.mean(axis=1)
        state = load_state()
        exclude = set(state.get('positions', {}).keys())
        top10 = pool[~pool['ts_code'].isin(exclude)].nlargest(10, 'score')
        name_map = {}
        try:
            conn = sqlite3.connect(str(DB_PATH))
            ns = pd.read_sql('SELECT ts_code, name FROM stocks', conn); conn.close()
            name_map = dict(zip(ns['ts_code'], ns['name']))
        except: pass
        print(f"\nB2 Top10 ({date_str}, 排除持仓{exclude}):")
        print(f"{'#':>2} {'代码':<12} {'名称':<8} {'Score':>7} {'价格':>7} {'涨跌':>7} {'20日位置':>8} {'LGBM':>7}")
        print('-' * 90)
        for i, (_, r) in enumerate(top10.iterrows(), 1):
            n = name_map.get(r['ts_code'], '??')
            # 20日位置
            try:
                conn2 = sqlite3.connect(str(DB_PATH))
                hk = pd.read_sql(f"SELECT high, low FROM daily_kline WHERE ts_code='{r['ts_code']}' ORDER BY trade_date DESC LIMIT 20", conn2)
                conn2.close()
                hi, lo = hk['high'].max(), hk['low'].min()
                pos20 = (r['close'] - lo) / (hi - lo) * 100 if hi > lo else 50
            except:
                pos20 = 0
            print(f"{i:>2} {r['ts_code']:<12} {n:<8} {r['score']:>+7.4f} {r['close']:>7.2f} {r['pct_chg']:>+6.2f}% {pos20:>6.0f}% {r.get('lgbm_score',0):>+7.4f}")
        t1 = top10.iloc[0]
        c1, n1 = t1['ts_code'], name_map.get(t1['ts_code'], '??')
        try:
            conn = sqlite3.connect(str(DB_PATH))
            h = pd.read_sql(f"SELECT trade_date,open,high,low,close,vol,pct_chg FROM daily_kline WHERE ts_code='{c1}' ORDER BY trade_date DESC LIMIT 20", conn)
            conn.close(); h = h.sort_values('trade_date')
            print(f"\nTop1: {c1} {n1} (score={t1['score']:+.4f})")
            ret5 = (h['close'].iloc[-1]/h['close'].iloc[-6]-1)*100 if len(h)>=6 else 0
            ret20 = (h['close'].iloc[-1]/h['close'].iloc[0]-1)*100
            hi, lo = h['high'].max(), h['low'].min()
            pos = (t1['close']-lo)/(hi-lo)*100 if hi>lo else 50
            print(f"  5日{ret5:+.2f}% 20日{ret20:+.2f}% | 20日位置{pos:.0f}% ({lo:.2f}~{hi:.2f})")
            print(f"  近20日K线:")
            for _, r in h.iterrows():
                print(f"    {r['trade_date']} O={r['open']:.2f} H={r['high']:.2f} L={r['low']:.2f} C={r['close']:.2f} {r['pct_chg']:+.2f}%")
        except: pass
    elif '--force-date' in sys.argv:
        idx = sys.argv.index('--force-date')
        date_str = sys.argv[idx + 1]
        run_daily(date_str, force=True)
    elif '--backfill' in sys.argv:
        idx = sys.argv.index('--backfill')
        start = sys.argv[idx + 1]
        end = sys.argv[idx + 2] if len(sys.argv) > idx + 2 else datetime.now().strftime('%Y%m%d')
        backfill(start, end)
    else:
        today_str = datetime.now().strftime('%Y%m%d')
        run_daily(today_str)
