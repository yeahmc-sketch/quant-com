#!/usr/bin/env python3
"""
MF v2.3 盘后模拟交易 — Fusion20(20因子)
=================================
每日15:20触发，基于收盘价模拟交易。

策略: DMA双确认择时 + Top500活跃池 + 20因子(Fusion20)等权评分 + Top3选股 + 10天持有

Fusion20(20因子):
  MF14(14量价财务): nv20, mb, c2h, rev_5, npe, npb, nps, nlmv, at20, nozt, npy, opy, ory, roe
  ALPHA4(4α101): a16, a13, a40, a88
  FF(2资金): main_pct, main_pct_5d

v2.3 回退: 从错误的F方案(REDUCED15+量价5)回退到已验证的Fusion20
          新增: rev_5, op_yoy, alpha_13, alpha_88, main_pct_5d
          移除: 量价5因子(Amihud/价量背离/量比/趋势一致/额比)

用法:
  # 最新交易日
  python3 v74/backtest/mf22_paper_trade.py
  
  # 补跑指定日期
  python3 v74/backtest/mf22_paper_trade.py --force-date 20260423
  
  # 查看持仓状态
  python3 v74/backtest/mf22_paper_trade.py --status
"""
import sys, json, os, subprocess, tempfile
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import sqlite3

# ===== 路径 =====
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
DB_PATH = PROJECT_DIR / "data" / "db" / "market.db"
STATE_DIR = PROJECT_DIR / "output" / "v74" / "portfolio"
STATE_FILE = STATE_DIR / "mf22_trade_state.json"
STATE_DIR.mkdir(parents=True, exist_ok=True)

# ===== 策略参数 =====
INIT_CASH = 50_000       # 初始资金
MAX_POS = 3              # 最大持仓数
HOLD_DAYS = 10           # 持仓天数
TOP_N = 3                # 选股数
POOL_SIZE = 500          # 活跃池大小
COST_BUY = 0.00035       # 买入佣金+过户费 万3.5
COST_SELL = 0.00135      # 卖出佣金+印花税+过户费 万13.5

# DMA双确认参数
DMA1_S, DMA1_L, DMA1_A = 5, 35, 5
DMA2_S, DMA2_L, DMA2_A = 10, 50, 10

# 因子列表 — Fusion20: MF14(14量价财务) + ALPHA4(4α101) + FF(2资金) = 20因子
FACTOR_COLS = [
    'nv20','mb','c2h','rev_5',
    'npe','npb','nps','nlmv','at20','nozt',
    'npy','opy','ory','roe',
    'a16','a13','a40','a88',
    'main_pct','main_pct_5d',
]
SKIP_ZSCORE = {'nozt'}

EXCLUDE_PREFIXES = ['688', '30', '8', '4', '920', 'bj']


# ============================================================
# 数据加载
# ============================================================

def load_today_data(trade_date_str, lookback_days=120):
    """加载当日所需数据（含lookback用于因子计算）"""
    trade_date = int(trade_date_str)
    start_date = trade_date - lookback_days * 100  # 粗略往前推
    if start_date < 20191101:
        start_date = 20191101

    conn = sqlite3.connect(DB_PATH)

    # 股票列表（按前缀和ST/*ST名称过滤）
    stocks = pd.read_sql('SELECT ts_code, name FROM stocks', conn)
    for p in EXCLUDE_PREFIXES:
        stocks = stocks[~stocks['ts_code'].str.startswith(p)]
    # 名称不含ST/*ST
    stocks = stocks[~stocks['name'].str.contains(r'\*?ST', na=False, regex=True)]
    valid_codes = set(stocks['ts_code'])

    # 日线（取lookback天）
    kline = pd.read_sql(f"""
        SELECT ts_code, trade_date, open, high, low, close, pre_close, pct_chg, vol, amount
        FROM daily_kline
        WHERE trade_date >= '{start_date}' AND trade_date <= '{trade_date}'
        ORDER BY ts_code, trade_date
    """, conn)
    kline = kline[kline['ts_code'].isin(valid_codes)]

    # 基本面
    basic = pd.read_sql(f"""
        SELECT ts_code, trade_date, pe_ttm, pb, ps_ttm, total_mv
        FROM daily_basic
        WHERE trade_date >= '{start_date}' AND trade_date <= '{trade_date}'
    """, conn)

    # 沪深300指数（DMA用）
    idx = pd.read_sql("""
        SELECT trade_date, close FROM v9_index_daily
        WHERE ts_code='000300.SH' ORDER BY trade_date
    """, conn)

    # 沪深300指数（DMA用）
    idx = pd.read_sql("""
        SELECT trade_date, close FROM v9_index_daily
        WHERE ts_code='000300.SH' ORDER BY trade_date
    """, conn)

    # 资金流向（新浪/东方财富双源，含main_pct）
    fund_flow = pd.read_sql(f"""
        SELECT ts_code, trade_date, main_pct
        FROM fund_flow
        WHERE trade_date >= '{start_date}' AND trade_date <= '{trade_date}'
    """, conn)
    fund_flow = fund_flow[fund_flow['ts_code'].isin(valid_codes)]

    # 财务指标（netprofit_yoy, op_yoy, or_yoy, roe — fina_indicator按公告日前向填充）
    fina = pd.read_sql(f"""
        SELECT ts_code, ann_date, netprofit_yoy, op_yoy, or_yoy, roe
        FROM fina_indicator
        WHERE ann_date <= '{trade_date}'
        ORDER BY ts_code, ann_date
    """, conn)
    fina = fina[fina['ts_code'].isin(valid_codes)]

    # 101因子（已缓存）
    # 注意: 101_core_v1.parquet只有截面上最活跃一批, 我们用实时计算替代
    conn.close()

    return kline, basic, idx, fund_flow, fina


# ============================================================
# Alpha101 轻量计算（现场算，不依赖缓存）
# ============================================================

def _alpha13_compute(data, td_str):
    """-rank(covariance(rank(close), rank(volume), 5))"""
    data = data.sort_values(['ts_code', 'trade_date']).copy()
    g = data.groupby('ts_code')
    rank_close = g['close'].transform(lambda s: s.rank(pct=True))
    rank_vol = g['vol'].transform(lambda s: s.rank(pct=True))
    pairs = pd.DataFrame({'rc': rank_close, 'rv': rank_vol}, index=data.index)
    def _cov5(g):
        return g['rc'].rolling(5, min_periods=3).cov(g['rv'])
    cov5 = pairs.groupby(data['ts_code'], group_keys=False).apply(_cov5)
    result = pd.Series(np.nan, index=data.index)
    mask = data['trade_date'] == td_str
    result[mask] = -cov5[mask].rank(pct=True).values
    return result

def _alpha16_compute(data, td_str):
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
    result[mask] = -cov5[mask].rank(pct=True).values
    return result

def _alpha40_compute(data, td_str):
    """-rank(stddev(high,10)) * correlation(high, volume, 10)"""
    data = data.sort_values(['ts_code', 'trade_date']).copy()
    g = data.groupby('ts_code')
    std10 = g['high'].transform(lambda s: s.rolling(10, min_periods=5).std())
    pairs = pd.DataFrame({'h': data['high'], 'v': data['vol']}, index=data.index)
    def _corr10(g):
        return g['h'].rolling(10, min_periods=5).corr(g['v'])
    corr10 = pairs.groupby(data['ts_code'], group_keys=False).apply(_corr10)
    result = pd.Series(np.nan, index=data.index)
    mask = data['trade_date'] == td_str
    result[mask] = (-(std10[mask].rank(pct=True).values * corr10[mask].values))
    return result

def _alpha88_compute(data, td_str):
    """min(rank(decay_linear((rank(open)+rank(low))-(rank(high)+rank(close)), 8)),
             ts_rank(decay_linear(correlation(ts_rank(close,8), ts_rank(adv60,21), 8), 7), 3))"""
    data = data.sort_values(['ts_code', 'trade_date']).copy()
    g = data.groupby('ts_code')
    rank_open = g['open'].transform(lambda s: s.rank(pct=True))
    rank_low = g['low'].transform(lambda s: s.rank(pct=True))
    rank_high = g['high'].transform(lambda s: s.rank(pct=True))
    rank_close = g['close'].transform(lambda s: s.rank(pct=True))
    ol_minus_hc = (rank_open + rank_low) - (rank_high + rank_close)
    w8 = np.arange(1, 9, dtype=float); w8 /= w8.sum()
    decay_olhc = ol_minus_hc.groupby(data['ts_code'], group_keys=False).apply(
        lambda s: s.rolling(8, min_periods=4).apply(lambda x: np.dot(x.values, w8[:len(x)]) if len(x) == 8 else np.nan, raw=False)
    )
    left = decay_olhc.rank(pct=True)
    tsrank_close = g['close'].transform(lambda s: s.rolling(8, min_periods=4).rank(pct=True))
    adv60 = g['vol'].transform(lambda s: s.rolling(60, min_periods=30).mean())
    tsrank_adv = adv60.groupby(data['ts_code'], group_keys=False).apply(
        lambda s: s.rolling(21, min_periods=10).rank(pct=True)
    )
    corr_pairs = pd.DataFrame({'rc': tsrank_close, 'ra': tsrank_adv}, index=data.index)
    corr_vals = corr_pairs.groupby(data['ts_code'], group_keys=False).apply(
        lambda g: g['rc'].rolling(8, min_periods=4).corr(g['ra'])
    )
    w7 = np.arange(1, 8, dtype=float); w7 /= w7.sum()
    decay_corr = corr_vals.groupby(data['ts_code'], group_keys=False).apply(
        lambda s: s.rolling(7, min_periods=4).apply(lambda x: np.dot(x.values, w7[:len(x)]) if len(x) == 7 else np.nan, raw=False)
    )
    right = decay_corr.groupby(data['ts_code'], group_keys=False).apply(
        lambda s: s.rolling(3, min_periods=2).rank(pct=True)
    )
    result = pd.Series(np.nan, index=data.index)
    mask = data['trade_date'] == td_str
    result[mask] = pd.concat([left[mask], right[mask]], axis=1).min(axis=1).values
    return result


def compute_factors(kline, basic, trade_date, fund_flow=None, fina=None):
    """计算当日截面因子值 — F方案: REDUCED15(15因子) + 量价(5因子) = 20因子"""
    td_str = str(trade_date)

    # 财务指标前向填充（取最新公告值）
    if fina is not None and not fina.empty:
        fina_sorted = fina.dropna(subset=['ann_date']).sort_values('ann_date')
        fina_sorted = fina_sorted.drop_duplicates(subset=['ts_code'], keep='last')
        fina_map = fina_sorted.set_index('ts_code')[['netprofit_yoy', 'op_yoy', 'or_yoy', 'roe']].to_dict('index')

    # 合并基本面
    kline = kline.merge(basic, on=['ts_code', 'trade_date'], how='left')
    kline = kline.sort_values(['ts_code', 'trade_date'])

    today = kline[kline['trade_date'] == td_str].copy()

    if today.empty:
        return None

    g = kline.groupby('ts_code')

    # MF因子（Fusion20技术因子）
    nv20 = -g['pct_chg'].transform(lambda s: s.rolling(20, min_periods=10).std())
    ma20 = g['close'].transform(lambda s: s.rolling(20, min_periods=10).mean())
    mb = -(kline['close'] - ma20) / (ma20 + 1e-8)
    h20 = g['high'].transform(lambda s: s.rolling(20).max())
    c2h = kline['close'] / h20
    rev_5 = g['pct_chg'].transform(lambda s: s.rolling(5, min_periods=3).mean())
    is_limit = (kline['pct_chg'] >= 9.5) & (kline['pre_close'] > 0)
    nozt = 1 - 2 * is_limit.groupby(kline['ts_code']).transform(lambda s: s.rolling(5).max().fillna(0))
    at20 = -g['vol'].transform(lambda s: s.rolling(20).mean())

    today = today.reset_index(drop=True)
    today['nv20'] = nv20.values[kline['trade_date'] == td_str]
    today['mb'] = mb.values[kline['trade_date'] == td_str]
    today['c2h'] = c2h.values[kline['trade_date'] == td_str]
    today['rev_5'] = rev_5.values[kline['trade_date'] == td_str]
    today['nozt'] = nozt.values[kline['trade_date'] == td_str]
    today['at20'] = at20.values[kline['trade_date'] == td_str]
    today['npe'] = -today['pe_ttm'].clip(upper=200).fillna(0)
    today['npb'] = -today['pb'].clip(upper=50).fillna(0)
    today['nps'] = -today['ps_ttm'].clip(upper=50).fillna(0)
    today['nlmv'] = -np.log(today['total_mv'].clip(lower=1).fillna(1e9))

    # 财务因子（前向填充）
    today['npy'] = 0.0
    today['opy'] = 0.0
    today['ory'] = 0.0
    today['roe'] = 0.0
    if fina is not None and not fina.empty:
        for idx, row in today.iterrows():
            code = row['ts_code']
            if code in fina_map:
                today.at[idx, 'npy'] = fina_map[code].get('netprofit_yoy', 0) / 100
                today.at[idx, 'opy'] = fina_map[code].get('op_yoy', 0) / 100
                today.at[idx, 'ory'] = fina_map[code].get('or_yoy', 0) / 100
                today.at[idx, 'roe'] = fina_map[code].get('roe', 0) / 100
            else:
                today.at[idx, 'npy'] = 0
                today.at[idx, 'opy'] = 0
                today.at[idx, 'ory'] = 0
                today.at[idx, 'roe'] = 0

    # 资金流向因子（当日主力净占比 + 5日均值）
    today['main_pct'] = 0.0
    today['main_pct_5d'] = 0.0
    if fund_flow is not None and not fund_flow.empty:
        ff = fund_flow.copy()
        ff['main_pct'] = pd.to_numeric(ff['main_pct'], errors='coerce').fillna(0)
        # 每日主力净占比
        ff_today = ff[ff['trade_date'] == td_str][['ts_code', 'main_pct']]
        today = today.merge(ff_today, on='ts_code', how='left', suffixes=('', '_ff'))
        if 'main_pct_ff' in today.columns:
            missing_pct = (today['main_pct_ff'].isna()).mean() * 100
            if missing_pct > 10:
                print(f"  ⚠️ 资金数据缺失 {missing_pct:.0f}% 股票（main_pct=0），请检查 fund_flow 增量下载")
            today['main_pct'] = today['main_pct_ff'].fillna(0)
            today.drop(columns=['main_pct_ff'], inplace=True)
        # 5日均值
        ff['trade_date_int'] = ff['trade_date'].astype(int)
        today_ff = ff[ff['trade_date'] == td_str][['ts_code']].copy()
        today_ff['trade_date_int'] = int(td_str)
        # 取近5个交易日的main_pct均值
        past5 = ff.sort_values('trade_date_int').groupby('ts_code')['main_pct'].rolling(
            window=5, min_periods=3).mean().reset_index(0, drop=True)
        ff['main_pct_5d'] = past5
        ff_5d = ff[ff['trade_date'] == td_str][['ts_code', 'main_pct_5d']]
        today = today.merge(ff_5d, on='ts_code', how='left', suffixes=('', '_5d'))
        if 'main_pct_5d_5d' in today.columns:
            today['main_pct_5d'] = today['main_pct_5d_5d'].fillna(0)
            today.drop(columns=['main_pct_5d_5d'], inplace=True)

    # Alpha101因子 (现场计算，不依赖缓存)
    today['a16'] = 0; today['a13'] = 0; today['a40'] = 0; today['a88'] = 0
    try:
        print("  计算Alpha101因子 (现场)...", flush=True)
        mask_td = (kline['trade_date'] == td_str).values
        a16 = pd.Series(_alpha16_compute(kline, td_str).values[mask_td]).fillna(0)
        a13 = pd.Series(_alpha13_compute(kline, td_str).values[mask_td]).fillna(0)
        a40 = pd.Series(_alpha40_compute(kline, td_str).values[mask_td]).fillna(0)
        a88 = pd.Series(_alpha88_compute(kline, td_str).values[mask_td]).fillna(0)
        today['a16'] = a16.values
        today['a13'] = a13.values
        today['a40'] = a40.values
        today['a88'] = a88.values
        nza = {c: (today[c] != 0).sum() for c in ['a16', 'a13', 'a40', 'a88']}
        print(f"    alpha非零: {nza}")
    except Exception as e:
        print(f"  Alpha101计算失败: {e}，使用默认值0")

    return today


def get_dma_signal(idx, trade_date_str):
    """获取DMA双确认信号"""
    idx = idx.sort_values('trade_date')
    idx['close'] = idx['close'].astype(float)
    dma1 = idx['close'].rolling(DMA1_S).mean() - idx['close'].rolling(DMA1_L).mean()
    ama1 = dma1.rolling(DMA1_A).mean()
    dma2 = idx['close'].rolling(DMA2_S).mean() - idx['close'].rolling(DMA2_L).mean()
    ama2 = dma2.rolling(DMA2_A).mean()
    signal = (dma1 > ama1) & (dma2 > ama2)

    # 取今天的信号
    idx['signal'] = signal
    row = idx[idx['trade_date'] == trade_date_str]
    if row.empty:
        # 用最新信号
        return bool(signal.iloc[-1])
    return bool(row['signal'].iloc[0])


def load_state():
    """加载当前持仓状态"""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        'strategy': 'MF v2.3',
        'cash': INIT_CASH,
        'init_cash': INIT_CASH,
        'positions': {},
        'trades': [],
        'equity_curve': [],
        'last_date': None,
    }


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def run_daily(trade_date_str, force=False):
    """执行单日模拟交易"""
    state = load_state()

    # 如果已经处理过今天且不是force，跳过
    if state.get('last_date') == trade_date_str and not force:
        print(f"  {trade_date_str} 已处理，跳过")
        return

    # 检查有没有事做：无到期+无空位，跳过数据加载（force时不跳过）
    positions = state.get('positions', {})
    cash = state['cash']
    has_expiring = False
    if positions:
        td = datetime.strptime(trade_date_str, '%Y%m%d')
        for sym, p in positions.items():
            ed = datetime.strptime(p['entry_date'], '%Y%m%d')
            if (td - ed).days >= HOLD_DAYS:
                has_expiring = True
                break
    has_slots = len(positions) < MAX_POS
    if not force and not has_expiring and not has_slots:
        # 记录净值（收盘价估算）
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            nav = cash
            missing_data = False
            for sym, p in positions.items():
                cur.execute("SELECT close FROM daily_kline WHERE ts_code=? AND trade_date=?",
                           (sym, trade_date_str))
                row = cur.fetchone()
                if row and row[0]:
                    nav += p['shares'] * float(row[0])
                else:
                    missing_data = True
            conn.close()
            if missing_data:
                # 无今日数据（非交易日或数据未到位），跳过不写入
                print(f"  ⏭️ 无{trade_date_str}行情数据，跳过")
                return
            state['equity_curve'].append({
                'date': trade_date_str, 'nav': round(nav, 2),
                'cash': round(cash, 2), 'pos_count': len(positions),
            })
            state['last_date'] = trade_date_str
            save_state(state)
            print(f"  ⏭️ 持仓满/未到期，跳过计算 (¥{nav:,.0f})")
        except:
            print(f"  ⏭️ 持仓满/未到期，跳过")
        return

    # 交易日验证（由调用者处理）

    print(f"\n===== MF v2.3 {trade_date_str} =====")
    print(f"现金: ¥{cash:,.2f}")

    # 加载数据
    kline, basic, idx, fund_flow, fina = load_today_data(trade_date_str)
    today = compute_factors(kline, basic, trade_date_str, fund_flow, fina)
    if today is None:
        print(f"  ⚠️ {trade_date_str} 无数据，跳过")
        return

    # DMA择时信号
    dma_ok = get_dma_signal(idx, trade_date_str)
    print(f"  DMA信号: {'✅ 多头' if dma_ok else '❌ 空头'}")

    # 处理到期持仓（卖出）
    positions = state.get('positions', {})
    cash = state['cash']
    sold_today = set()  # 记录今日卖出，避免当天买回

    if positions:
        for sym in list(positions.keys()):
            p = positions[sym]
            entry_date = p['entry_date']
            # 计算持仓天数
            ed = datetime.strptime(entry_date, '%Y%m%d')
            td = datetime.strptime(trade_date_str, '%Y%m%d')
            hold_days = (td - ed).days

            if hold_days >= HOLD_DAYS:
                row = today[today['ts_code'] == sym]
                if not row.empty:
                    close = row.iloc[0]['close']
                    shares = p['shares']
                    entry_price = p['entry_price']
                    ret = (close - entry_price) / entry_price * 100
                    proceeds = shares * close * (1 - COST_SELL)
                    cash += proceeds
                    state['trades'].append({
                        'code': sym,
                        'entry_date': entry_date,
                        'exit_date': trade_date_str,
                        'entry_price': entry_price,
                        'exit_price': close,
                        'shares': shares,
                        'return_pct': round(ret, 2),
                    })
                    print(f"  卖出 {sym}: {ret:+.2f}% | 持仓{hold_days}天")
                    sold_today.add(sym)
                    del positions[sym]

    # DMA多头 → 买入
    if dma_ok:
        # 构建Top500活跃池
        pool = today.nlargest(POOL_SIZE, 'amount') if 'amount' in today.columns else today

        # 因子评分
        valid_f = [c for c in FACTOR_COLS if c in pool.columns]
        scores = pool[valid_f].fillna(0).copy()
        for c in valid_f:
            if c in SKIP_ZSCORE:
                continue
            std = scores[c].std()
            if std > 1e-8:
                scores[c] = (scores[c] - scores[c].mean()) / std
            else:
                scores[c] = 0
        pool['score'] = scores.mean(axis=1)

        # 选Top3（排除已持仓 + 今日已卖出）
        exclude = set(positions.keys()) | sold_today
        candidates = pool[~pool['ts_code'].isin(exclude)]
        selected = candidates.nlargest(TOP_N, 'score')

        # 买入
        slots = MAX_POS - len(positions)
        if slots > 0:
            per_stock = cash / slots
            for _, row in selected.head(slots).iterrows():
                sym = row['ts_code']
                close = row['close']
                if close <= 0:
                    continue
                shares = int(per_stock / close / 100) * 100
                if shares < 100:
                    continue
                cost = shares * close * (1 + COST_BUY)
                if cost <= cash:
                    cash -= cost
                    # 获取股票名称
                    stock_name = ''
                    try:
                        c2 = sqlite3.connect(DB_PATH)
                        cur = c2.cursor()
                        cur.execute("SELECT name FROM stocks WHERE ts_code=?", (sym,))
                        r = cur.fetchone()
                        if r: stock_name = r[0]
                        c2.close()
                    except:
                        pass
                    positions[sym] = {
                        'entry_date': trade_date_str,
                        'entry_price': close,
                        'shares': shares,
                        'name': stock_name,
                    }
                    print(f"  买入 {sym}({stock_name}): ¥{close:.2f} × {shares}股 = ¥{cost:,.0f}")

    # 记录净值
    nav = cash
    for sym, p in positions.items():
        row = today[today['ts_code'] == sym]
        if not row.empty:
            nav += p['shares'] * row.iloc[0]['close']

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
    print(f"  净值: ¥{nav:,.0f} | 持仓: {len(positions)}只 | 现金: ¥{cash:,.0f}")
    print(f"  总收益: {(nav - INIT_CASH) / INIT_CASH * 100:+.1f}%")


def show_status():
    """显示当前持仓和收益"""
    state = load_state()
    eq = state.get('equity_curve', [])
    positions = state.get('positions', {})
    cash = state.get('cash', INIT_CASH)
    init_cash = state.get('init_cash', INIT_CASH)

    print(f"\n{'='*50}")
    print(f"  MF v2.3 模拟交易状态")
    print(f"{'='*50}")
    print(f"  策略: {state.get('strategy', 'MF v2.3')}")
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

        # 按持仓天数排序
        today_s = state.get('last_date', '')
        if positions and today_s:
            print(f"\n  --- 当前持仓 ---")
            for sym, p in sorted(positions.items()):
                ed = datetime.strptime(p['entry_date'], '%Y%m%d')
                td = datetime.strptime(today_s, '%Y%m%d')
                hd = (td - ed).days
                ep = p['entry_price']
                print(f"  {sym}: 买入¥{ep:.2f} × {p['shares']}股 | 持仓{hd}天 "
                      f"(到期还需{HOLD_DAYS - hd}天)")

    print(f"{'='*50}")


def backfill(start_date, end_date):
    """补跑一段历史区间"""
    from datetime import timedelta

    # 获取交易日历
    conn = sqlite3.connect(DB_PATH)
    trading_days = pd.read_sql(f"""
        SELECT DISTINCT trade_date FROM daily_kline
        WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        ORDER BY trade_date
    """, conn)
    conn.close()

    dates = trading_days['trade_date'].tolist()
    print(f"补跑 {len(dates)} 个交易日: {dates[0]} ~ {dates[-1]}")

    for d in dates:
        run_daily(d, force=True)


# ============================================================
# 邮件通知
# ============================================================

def send_email_notification():
    """发送HTML格式的持仓报告邮件（对标V15/V15SS风格）"""
    state = load_state()
    eq = state.get('equity_curve', [])
    positions = state.get('positions', {})
    trades = state.get('trades', [])
    cash = state.get('cash', INIT_CASH)
    init_cash = state.get('init_cash', INIT_CASH)
    nav = eq[-1]['nav'] if eq else cash
    total_ret = (nav - init_cash) / init_cash * 100
    win_trades = [t for t in trades if t.get('return_pct', 0) > 0]
    win_rate = len(win_trades) / len(trades) * 100 if trades else 0
    avg_ret = sum(t.get('return_pct', 0) for t in trades) / len(trades) if trades else 0
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    subj_str = datetime.now().strftime('%m-%d')
    last_date = state.get('last_date', '')

    # 股票名称映射
    name_map = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT ts_code, name FROM stocks")
        for row in cur.fetchall():
            name_map[row[0]] = row[1]
        conn.close()
    except:
        pass

    # 大盘数据（10日累计涨跌）
    hs300_10d_ret = 'N/A'
    hs300_close = 'N/A'
    try:
        conn = sqlite3.connect(DB_PATH)
        if last_date:
            idx = pd.read_sql("""
                SELECT trade_date, close FROM v9_index_daily
                WHERE ts_code='000300.SH' AND trade_date <= ?
                ORDER BY trade_date DESC LIMIT 12
            """, conn, params=(last_date,))
        else:
            idx = pd.read_sql("""
                SELECT trade_date, close FROM v9_index_daily
                WHERE ts_code='000300.SH' ORDER BY trade_date DESC LIMIT 12
            """, conn)
        conn.close()
        if len(idx) >= 2:
            idx = idx.sort_values('trade_date').reset_index(drop=True)
            newest = float(idx.iloc[-1]['close'])
            oldest = float(idx.iloc[0]['close'])
            hs300_10d_ret = f'{(newest - oldest) / oldest * 100:+.2f}%'
            hs300_close = f'{newest:,.2f}'
            # 最多取10个交易日（约2周）
            idx_10d = idx.tail(10)
            if len(idx_10d) >= 2:
                hs300_10d_ret = f'{(float(idx_10d.iloc[-1]["close"]) - float(idx_10d.iloc[0]["close"])) / float(idx_10d.iloc[0]["close"]) * 100:+.2f}%'
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
    last_date = state.get('last_date', '')
    for sym, p in sorted(positions.items()):
        ed = datetime.strptime(p['entry_date'], '%Y%m%d')
        td = datetime.strptime(last_date, '%Y%m%d') if last_date else datetime.now()
        hd = (td - ed).days
        ep = p['entry_price']
        name = p.get('name', sym)
        pnl = 0
        cls = 'stat-up'
        pnl_str = '—'
        # 尝试获取现价
        try:
            conn = sqlite3.connect(DB_PATH)
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
            <b>{t["code"]}</b> 卖出 <span class="{cls}">{t["return_pct"]:+.2f}%</span>
            （持有至{t["exit_date"]}）
        </div>'''

    html = f'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
.card {{ background: white; border-radius: 12px; padding: 20px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
.card-title {{ font-size: 16px; font-weight: 600; color: #333; margin-bottom: 12px; border-left: 4px solid #7f77dd; padding-left: 10px; }}
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
</style>
</head>
<body>
<div class="card">
<div class="card-title">🌏 今日大盘</div>
<div class="grid">
<div class="stat-box"><div class="stat-val {hs300_cls}">{hs300_10d_ret}</div><div class="stat-label">沪深300近10日</div></div>
<div class="stat-box"><div class="stat-val">{hs300_close}</div><div class="stat-label">收盘点位</div></div>
</div>
</div>

<div class="card">
<div class="card-title">📊 MF v2.3 账户</div>
<div class="grid">
<div class="stat-box"><div class="stat-val {"stat-up" if total_ret>=0 else "stat-down"}">{total_ret:+.2f}%</div><div class="stat-label">累计收益</div></div>
<div class="stat-box"><div class="stat-val">¥{nav:,.0f}</div><div class="stat-label">总权益</div></div>
<div class="stat-box"><div class="stat-val">{win_rate:.0f}%</div><div class="stat-label">胜率</div></div>
<div class="stat-box"><div class="stat-val">{avg_ret:+.2f}%</div><div class="stat-label">笔均收益</div></div>
</div>
<div class="param">初始: ¥{init_cash:,} | 现金: ¥{cash:,.0f} | 持仓: {len(positions)}只 | 交易: {len(trades)}笔</div>
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
DMA双确认择时 · Top500活跃池 · 20因子(含资金) · 10天持仓 · Top3<br>
{now_str}
</div>
</body>
</html>'''

    # 纯文本版本
    pos_lines = []
    for sym, p in sorted(positions.items()):
        pos_lines.append(f'  {sym}: {p["entry_price"]:.2f}x{p["shares"]}股')
    pos_str = '\n'.join(pos_lines) if pos_lines else '  空仓'
    plain = f'''MF v2.3 模拟交易 - {now_str}

沪深300近10日: {hs300_10d_ret} ({hs300_close})

账户: {total_ret:+.2f}% | ¥{nav:,.0f} | 胜率{win_rate:.0f}% | {len(trades)}笔

持仓:
{pos_str}
'''

    # 发送（用curl，兼容开发环境SSL限制和生产环境）
    sent = False
    try:
        import subprocess, tempfile
        email_txt = f'''From: "MF v2.3" <18313835@qq.com>
To: 18313835@qq.com
Subject: MF v2.3 持仓报告 ({subj_str})
MIME-Version: 1.0
Content-Type: text/html; charset="utf-8"

{html}'''
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
        Path(tmp.name).unlink(missing_ok=True)
        sent = ret.returncode == 0
    except Exception as e:
        print(f"  邮件发送异常: {e}")
    
    if sent:
        print(f'邮件已发送 ({total_ret:+.2f}%)')
    else:
        print(f'邮件发送失败')


# ============================================================
# Main
# ============================================================

if __name__ == '__main__':
    if '--status' in sys.argv:
        show_status()
    elif '--notify' in sys.argv:
        send_email_notification()
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
