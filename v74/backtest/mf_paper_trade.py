#!/usr/bin/env python3
"""
多因子选股模拟交易执行器 v1.0
==============================

每日自动执行的模拟交易系统，从收盘后的选股结果驱动交易。

架构：
  - 数据流：download_snapshot_sqlite.py (16:05) → mf_daily_selector.py (16:10) → 本脚本 (16:15)
  - 状态持久化：output/v74/portfolio/trade_state.json
  - 每日输出：output/v74/portfolio/daily/YYYYMMDD.md（交易日志）
  - 周报输出：output/v74/portfolio/weekly/YYYYMMDD_weekly.md

交易规则（与回测一致）：
  - T日收盘后选出候选股（mf_layered_daily_selector.py 生成）
  - T+1 以开盘价买入（等权分配）
  - 持有 15 个交易日后以收盘价卖出（B参数定版）
  - 涨停拦截：T+1 开盘涨幅 >= 9.5% 跳过
  - ST拦截：名称含 ST 或退 的跳过
  - 手续费：买入万三+过户费万0.5；卖出万三+印花税千一+过户费万0.5

参数（B参数定版 2026-04-12）：
  - 25%池 / Top3 / 15天持仓 / 无择时
  - L1(筛选): neg_volatility_20, neg_pb, roe, neg_idio_vol, amihud, neg_cmra
  - L2(排序): rev_5, rev_10, shrink_vol, close_to_high, ma5_slope, neg_volatility_10
  - 6年全程回测：+91.5%, Sharpe 0.57, 回撤32.5%
  - 初始本金：¥50,000

用法：
  python3 v74/backtest/mf_paper_trade.py           # 执行今日交易
  python3 v74/backtest/mf_paper_trade.py --init     # 初始化模拟账户
  python3 v74/backtest/mf_paper_trade.py --status   # 查看当前状态
  python3 v74/backtest/mf_paper_trade.py --report   # 生成周报
  python3 v74/backtest/mf_paper_trade.py --force-date 20260413  # 强制指定日期（用于补跑）
"""

import sys
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import sqlite3

# ===== 路径 =====
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
DB_PATH = PROJECT_DIR / "data" / "db" / "market.db"
SELECT_DIR = PROJECT_DIR / "output" / "v74" / "multi_factor"
PORTFOLIO_DIR = PROJECT_DIR / "output" / "v74" / "portfolio"
DAILY_DIR = PORTFOLIO_DIR / "daily"
WEEKLY_DIR = PORTFOLIO_DIR / "weekly"
for d in [PORTFOLIO_DIR, DAILY_DIR, WEEKLY_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ===== 固定参数（B参数定版 2026-04-12）=====
INITIAL_CAPITAL = 50000
FEE_BUY = 0.00035
FEE_SELL = 0.00135
LIMIT_UP_PCT = 9.5

# ===== 自适应参数（从 mf_regime_config 读取，不再硬编码）=====
# 默认兜底：B·趋势牛市定版参数
_DEFAULT_HOLD_DAYS = 15
_DEFAULT_TOP_N     = 3

def get_active_params() -> dict:
    """
    获取当前生效的策略参数（从自适应状态文件读取）

    优先级：
      1. output/v74/portfolio/adaptive_state.json（自适应调度器写入）
      2. 硬编码默认值（C·高波震荡参数，兜底）

    Returns
    -------
    dict  包含 hold_days, top_n, timing_mode, timing_param,
               strategy_name, regime_id, regime_label_cn
    """
    adaptive_state_file = PORTFOLIO_DIR / "adaptive_state.json"
    if adaptive_state_file.exists():
        try:
            import sys as _sys
            import importlib
            # 确保能找到 mf_regime_config
            _script_dir = str(Path(__file__).parent)
            if _script_dir not in _sys.path:
                _sys.path.insert(0, _script_dir)
            from mf_regime_config import get_config_by_id
            from mf_market_detector import MarketRegime

            with open(adaptive_state_file, encoding='utf-8') as f:
                adp = json.load(f)
            regime_id = adp.get('current_regime_id', 'B_TRENDING_BULL')
            cfg = get_config_by_id(regime_id)
            return {
                'hold_days':     cfg.hold_days,
                'top_n':         cfg.top_n,
                'timing_mode':   cfg.timing_mode,
                'timing_param':  cfg.timing_param,
                'position_ratio': cfg.position_ratio,
                'strategy_name': cfg.strategy_name,
                'regime_id':     cfg.regime.regime_id,
                'regime_label_cn': cfg.regime.label_cn,
                'validated':     cfg.validated,
            }
        except Exception as e:
            print(f"  ⚠️  自适应参数读取失败（{e}），使用默认参数")

    # 兜底默认：B·趋势牛市
    return {
        'hold_days':      _DEFAULT_HOLD_DAYS,
        'top_n':          _DEFAULT_TOP_N,
        'timing_mode':    'none',
        'timing_param':   0.0,
        'position_ratio': 1.0,
        'strategy_name':  'B_TRENDING_BULL_LAYERED_v1',
        'regime_id':      'B_TRENDING_BULL',
        'regime_label_cn': 'B·趋势牛市',
        'validated':      True,
    }

# ===== 状态文件 =====
STATE_FILE = PORTFOLIO_DIR / "trade_state.json"


def get_db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA cache_size=-64000")
    return conn


def load_state():
    """加载交易状态"""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return None


def save_state(state):
    """保存交易状态"""
    state['last_updated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, ensure_ascii=False, default=str)


def init_state(start_date=None):
    """初始化模拟账户"""
    if start_date is None:
        conn = get_db_conn()
        latest = pd.read_sql("SELECT MAX(trade_date) as d FROM daily_kline", conn)
        conn.close()
        start_date = str(latest['d'].iloc[0])

    state = {
        'version': '1.0',
        'initial_capital': INITIAL_CAPITAL,
        'capital': INITIAL_CAPITAL,
        'start_date': start_date,
        'positions': [],
        'closed_trades': [],
        'daily_equity': [],
        'trade_log': [],
        'last_trade_date': None,
        'last_select_date': None,
        'pending_buy': None,  # 待买入订单 {select_date, buy_date, ...}
        'stats': {
            'total_trades': 0,
            'win_trades': 0,
            'total_pnl': 0,
            'total_fee': 0,
        },
    }
    save_state(state)
    print(f"  ✅ 模拟账户已初始化")
    print(f"  本金: ¥{INITIAL_CAPITAL:,}")
    print(f"  起始日: {start_date}")
    print(f"  状态文件: {STATE_FILE}")
    return state


def get_price(conn, ts_code, trade_date, price_type='open'):
    """获取指定日期的价格"""
    df = pd.read_sql(f'''
        SELECT {price_type} as price FROM daily_kline
        WHERE ts_code = '{ts_code}' AND trade_date = '{trade_date}'
    ''', conn)
    if not df.empty:
        return float(df['price'].iloc[0])
    return None


def get_pre_close(conn, ts_code, trade_date):
    """获取前一交易日收盘价"""
    df = pd.read_sql(f'''
        SELECT pre_close FROM daily_kline
        WHERE ts_code = '{ts_code}' AND trade_date = '{trade_date}'
    ''', conn)
    if not df.empty:
        return float(df['pre_close'].iloc[0])
    return None


def load_stock_names(conn):
    """加载股票名称映射"""
    df = pd.read_sql('SELECT ts_code, name FROM stocks', conn)
    return dict(zip(df['ts_code'], df['name']))


def get_next_trade_date(conn, current_date):
    """获取下一个交易日"""
    df = pd.read_sql(f'''
        SELECT DISTINCT trade_date FROM daily_kline
        WHERE trade_date > '{current_date}'
        ORDER BY trade_date LIMIT 1
    ''', conn)
    if not df.empty:
        return str(df['trade_date'].iloc[0])
    return None


def get_prev_trade_date(conn, current_date):
    """获取上一个交易日"""
    df = pd.read_sql(f'''
        SELECT DISTINCT trade_date FROM daily_kline
        WHERE trade_date < '{current_date}'
        ORDER BY trade_date DESC LIMIT 1
    ''', conn)
    if not df.empty:
        return str(df['trade_date'].iloc[0])
    return None


def calc_buy_fee(amount):
    """计算买入费用"""
    commission = max(amount * 0.0003, 5.0)
    transfer_fee = amount * 0.00005
    return commission + transfer_fee


def calc_sell_fee(amount):
    """计算卖出费用"""
    commission = max(amount * 0.0003, 5.0)
    stamp_tax = amount * 0.001
    transfer_fee = amount * 0.00005
    return commission + stamp_tax + transfer_fee


def load_selector_result(trade_date):
    """加载指定日期的选股结果"""
    json_files = list(SELECT_DIR.glob(f'mf_select_{trade_date}*.json'))
    if not json_files:
        return None
    # 优先选不带时间戳的
    simple_files = [f for f in json_files if f.stem == f'mf_select_{trade_date}']
    target = simple_files[0] if simple_files else max(json_files, key=lambda f: f.stat().st_mtime)
    try:
        with open(target) as f:
            return json.load(f)
    except:
        return None


def execute_sell(state, conn, trade_date, hold_days=None):
    """执行到期卖出

    Parameters
    ----------
    hold_days : int or None
        持仓天数阈值。None 时从自适应参数自动获取。
    """
    if hold_days is None:
        hold_days = get_active_params()['hold_days']

    positions = state['positions']
    if not positions:
        return []

    sold = []
    remaining = []

    for pos in positions:
        # 优先使用仓位自身记录的 hold_days 阈值（买入时已确定）
        pos_hold_threshold = pos.get('hold_days_target', hold_days)
        if pos['hold_days'] >= pos_hold_threshold:
            sell_price = get_price(conn, pos['ts_code'], trade_date, 'close')
            if sell_price is None:
                # 停牌，顺延
                pos['hold_days'] += 0  # 不增加，保持等下一个卖出机会
                remaining.append(pos)
                continue

            sell_amount = pos['shares'] * sell_price
            sell_fee = calc_sell_fee(sell_amount)
            net_proceeds = sell_amount - sell_fee
            gross_ret = (sell_price / pos['buy_price'] - 1) * 100
            net_ret = (net_proceeds / pos['cost'] - 1) * 100

            state['capital'] += net_proceeds

            trade_record = {
                'ts_code': pos['ts_code'],
                'name': pos['name'],
                'buy_date': pos['buy_date'],
                'buy_price': pos['buy_price'],
                'sell_date': trade_date,
                'sell_price': sell_price,
                'shares': pos['shares'],
                'gross_ret_pct': round(gross_ret, 2),
                'net_ret_pct': round(net_ret, 2),
                'buy_fee': round(pos['buy_fee'], 2),
                'sell_fee': round(sell_fee, 2),
                'net_proceeds': round(net_proceeds, 2),
                'hold_days_actual': pos['hold_days'],
                'select_date': pos['select_date'],
                'select_rank': pos.get('select_rank', 0),
            }

            state['closed_trades'].append(trade_record)
            sold.append(trade_record)

            # 更新统计
            state['stats']['total_trades'] += 1
            state['stats']['total_pnl'] += net_ret
            state['stats']['total_fee'] += pos['buy_fee'] + sell_fee
            if net_ret > 0:
                state['stats']['win_trades'] += 1
        else:
            pos['hold_days'] += 1
            remaining.append(pos)

    state['positions'] = remaining
    return sold


def execute_buy(state, conn, select_date, buy_date, top_n=None, hold_days_target=None):
    """执行买入

    Parameters
    ----------
    top_n : int or None
        买入股数。None 时从自适应参数自动获取。
    hold_days_target : int or None
        本次买入持仓目标天数（记录到 position，卖出时用）。
        None 时从自适应参数自动获取。
    """
    params = get_active_params()
    if top_n is None:
        top_n = params['top_n']
    if hold_days_target is None:
        hold_days_target = params['hold_days']

    # B·趋势牛市：top_n=0 代表暂停买入
    if top_n == 0:
        return None, f"⚠️ 当前市场类型({params['regime_label_cn']})无对应策略，已暂停买入"

    sel_data = load_selector_result(select_date)
    if sel_data is None:
        return None, "无选股结果"

    name_map = load_stock_names(conn)
    stocks = sel_data.get('stocks', [])[:top_n]

    if not stocks:
        return None, "选股结果为空"

    bought = []
    skipped = []
    capital = state['capital']

    for stock in stocks:
        code = stock['ts_code']
        name = stock.get('name', name_map.get(code, code))

        # ST拦截
        if 'ST' in str(name) or '退' in str(name):
            skipped.append(f"{name}(ST)")
            continue

        buy_price = get_price(conn, code, buy_date, 'open')
        pre_close = get_pre_close(conn, code, buy_date)
        if buy_price is None or pre_close is None:
            skipped.append(f"{name}(无价格)")
            continue

        # 涨停拦截
        if pre_close > 0 and (buy_price / pre_close - 1) * 100 >= LIMIT_UP_PCT:
            skipped.append(f"{name}(涨停)")
            continue

        # 计算可买股数（整手）
        per_stock = capital / top_n
        shares = int(per_stock / (buy_price * 100)) * 100
        if shares <= 0:
            skipped.append(f"{name}(资金不足,需¥{buy_price*100:.0f}/手)")
            continue

        actual_cost = shares * buy_price
        actual_fee = calc_buy_fee(actual_cost)
        total_cost = actual_cost + actual_fee

        if total_cost > capital:
            skipped.append(f"{name}(超出余额)")
            continue

        capital -= total_cost

        position = {
            'ts_code': code,
            'name': name,
            'buy_date': buy_date,
            'buy_price': buy_price,
            'shares': shares,
            'cost': total_cost,
            'buy_fee': actual_fee,
            'hold_days': 0,
            'hold_days_target': hold_days_target,   # ← 新增：记录本次持仓目标天数
            'select_date': select_date,
            'select_rank': stock.get('rank', 0),
            'select_score': stock.get('score', 0),
            'regime_id': params['regime_id'],        # ← 新增：记录买入时的市场类型
            'strategy_name': params['strategy_name'], # ← 新增：记录买入时用的策略名
        }

        state['positions'].append(position)
        state['capital'] = capital
        bought.append(position)

    result = {
        'stocks': bought,
        'skipped': skipped,
        'total_invested': sum(p['cost'] for p in bought),
        'n_bought': len(bought),
        'n_skipped': len(skipped),
        'top_n': top_n,
        'hold_days_target': hold_days_target,
        'regime_id': params['regime_id'],
        'strategy_name': params['strategy_name'],
    }
    return result, None


def update_daily_equity(state, conn, trade_date):
    """更新每日净值"""
    position_value = 0
    for pos in state['positions']:
        price = get_price(conn, pos['ts_code'], trade_date, 'close')
        if price is not None:
            position_value += price * pos['shares']

    total_equity = state['capital'] + position_value
    equity_pct = round(total_equity / state['initial_capital'] * 100, 2)

    entry = {
        'trade_date': trade_date,
        'capital': round(state['capital'], 2),
        'position_value': round(position_value, 2),
        'total_equity': round(total_equity, 2),
        'n_positions': len(state['positions']),
        'equity_pct': equity_pct,
    }
    state['daily_equity'].append(entry)
    return entry


def generate_daily_report(state, trade_date, sold, buy_result, equity):
    """生成每日交易日志"""
    lines = [
        f"# 模拟交易日志 {trade_date}",
        "",
        f"> 交易日: {trade_date} ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})",
        "",
        "## 账户概览",
        "",
        f"| 指标 | 值 |",
        f"| --- | --- |",
        f"| 总资产 | ¥{equity['total_equity']:,.2f} |",
        f"| 可用资金 | ¥{equity['capital']:,.2f} |",
        f"| 持仓市值 | ¥{equity['position_value']:,.2f} |",
        f"| 净值 | {equity['equity_pct']/100:.4f} |",
        f"| 累计收益 | {equity['equity_pct']-100:+.2f}% |",
        f"| 当前持仓 | {len(state['positions'])} 只 |",
        f"| 累计交易 | {state['stats']['total_trades']} 笔 |",
        f"| 累计胜率 | {state['stats']['win_trades']/max(state['stats']['total_trades'],1)*100:.1f}% |",
        "",
    ]

    # 卖出记录
    if sold:
        lines.append("## 今日卖出")
        lines.append("")
        lines.append(f"| 代码 | 名称 | 买入日 | 买入价 | 卖出价 | 持仓天数 | 净收益% |")
        lines.append(f"| --- | --- | --- | --- | --- | --- | --- |")
        for t in sold:
            wl = "🔴" if t['net_ret_pct'] > 0 else "🟢"
            lines.append(
                f"| {t['ts_code']} | {t['name']} | {t['buy_date']} | "
                f"¥{t['buy_price']:.2f} | ¥{t['sell_price']:.2f} | "
                f"{t['hold_days_actual']}天 | {t['net_ret_pct']:+.2f}% {wl} |"
            )
        lines.append("")

    # 买入记录
    if buy_result and buy_result.get('stocks'):
        lines.append("## 今日买入")
        lines.append("")
        lines.append(f"| 代码 | 名称 | 买入价 | 股数 | 成本 | 排名 |")
        lines.append(f"| --- | --- | --- | --- | --- | --- |")
        for p in buy_result['stocks']:
            lines.append(
                f"| {p['ts_code']} | {p['name']} | ¥{p['buy_price']:.2f} | "
                f"{p['shares']} | ¥{p['cost']:,.2f} | #{p.get('select_rank', '-')} |"
            )
        if buy_result.get('skipped'):
            lines.append(f"\n> 跳过: {', '.join(buy_result['skipped'])}")
        lines.append("")

    # 当前持仓
    if state['positions']:
        lines.append("## 当前持仓")
        lines.append("")
        lines.append(f"| 代码 | 名称 | 买入日 | 买入价 | 现价 | 持仓天数 | 浮盈% |")
        lines.append(f"| --- | --- | --- | --- | --- | --- | --- |")
        conn = get_db_conn()
        for pos in state['positions']:
            price = get_price(conn, pos['ts_code'], trade_date, 'close')
            if price is None:
                price = pos['buy_price']
            pnl = (price / pos['buy_price'] - 1) * 100
            wl = "🔴" if pnl > 0 else "🟢"
            lines.append(
                f"| {pos['ts_code']} | {pos['name']} | {pos['buy_date']} | "
                f"¥{pos['buy_price']:.2f} | ¥{price:.2f} | {pos['hold_days']}天 | "
                f"{pnl:+.2f}% {wl} |"
            )
        conn.close()
        lines.append("")

    lines.extend([
        "---",
        f"*模拟交易，仅供研究参考。本金 ¥{state['initial_capital']:,} | B参数定版 25%池/Top3/15天*",
    ])

    report_path = DAILY_DIR / f"{trade_date}.md"
    report_path.write_text("\n".join(lines), encoding='utf-8')
    return report_path


def run_daily(force_date=None):
    """执行每日交易流程

    时间线（每日 16:20 自动化任务执行）：
    ┌─────────────────────────────────────────────────────┐
    │  T日 16:20（收盘后运行）                             │
    │  1. download_snapshot_sqlite.py → 写入 T 日行情      │
    │  2. mf_daily_selector.py → 生成 T 日选股 JSON        │
    │  3. mf_paper_trade.py:                               │
    │     a. 卖出到期持仓（T日收盘价）                      │
    │     b. 检查 T 日选股结果 → 记录为 pending_buy        │
    │     c. 如果有上一轮的 pending_buy（T-1日选股，今天买入）│
    │        → 用 T日开盘价执行买入                         │
    └─────────────────────────────────────────────────────┘
    """
    state = load_state()
    if state is None:
        print("  ❌ 状态文件不存在，请先 --init")
        return None

    conn = get_db_conn()
    name_map = load_stock_names(conn)

    # 确定今日日期
    if force_date:
        trade_date = force_date
    else:
        latest = pd.read_sql("SELECT MAX(trade_date) as d FROM daily_kline", conn)
        trade_date = str(latest['d'].iloc[0])

    # 检查是否已执行过
    if state['last_trade_date'] == trade_date:
        print(f"  ⏭️ {trade_date} 已执行过，跳过")
        return state

    print(f"  📅 交易日: {trade_date}")
    print(f"  💰 当前资金: ¥{state['capital']:,.2f}")
    print(f"  📦 持仓: {len(state['positions'])} 只")

    # ===== 读取当前自适应参数 =====
    params = get_active_params()
    print(f"  🎯 策略: [{params['regime_id']}] {params['strategy_name']}")
    print(f"      pool=N/A  top={params['top_n']}  hold={params['hold_days']}d  "
          f"timing={params['timing_mode']}")

    # ===== Step 1: 卖出到期持仓（T日收盘价） =====
    sold = execute_sell(state, conn, trade_date, hold_days=params['hold_days'])
    if sold:
        total_sold = sum(s['net_proceeds'] for s in sold)
        win_count = sum(1 for s in sold if s['net_ret_pct'] > 0)
        print(f"  📤 卖出 {len(sold)} 只 (盈利 {win_count}/{len(sold)}), 回收 ¥{total_sold:,.2f}")

    # ===== Step 2: 执行待买入订单（上一轮选股，今天 T+1 买入） =====
    buy_result = None
    pending = state.get('pending_buy')
    if pending and not state['positions']:
        sel_date = pending['select_date']
        # 验证买入日期 = 今天
        expected_buy_date = pending.get('buy_date')
        if expected_buy_date == trade_date:
            print(f"  📥 执行待买入: 选股日={sel_date}, 买入日={trade_date}")
            buy_result, err = execute_buy(
                state, conn, sel_date, trade_date,
                top_n=params['top_n'],
                hold_days_target=params['hold_days'],
            )
            if buy_result and buy_result.get('stocks'):
                print(f"  ✅ 买入 {buy_result['n_bought']} 只 "
                      f"[{buy_result['regime_id']}], "
                      f"投资 ¥{buy_result['total_invested']:,.2f}")
                if buy_result.get('skipped'):
                    print(f"  ⏭️ 跳过: {', '.join(buy_result['skipped'])}")
                state['pending_buy'] = None  # 清除待买入
            else:
                print(f"  ❌ 买入失败: {err}")
        elif expected_buy_date and expected_buy_date < trade_date:
            # 买入日已过（补跑场景），仍然执行
            print(f"  ⚠️ 补跑买入: 选股日={sel_date}, 应买入日={expected_buy_date}, 实际执行日={trade_date}")
            buy_result, err = execute_buy(
                state, conn, sel_date, trade_date,
                top_n=params['top_n'],
                hold_days_target=params['hold_days'],
            )
            if buy_result and buy_result.get('stocks'):
                print(f"  ✅ 补跑买入 {buy_result['n_bought']} 只")
                if buy_result.get('skipped'):
                    print(f"  ⏭️ 跳过: {', '.join(buy_result['skipped'])}")
                state['pending_buy'] = None  # 清除待买入
            else:
                print(f"  ❌ 买入失败: {err}")
        else:
            print(f"  ⏳ 待买入日={expected_buy_date}, 今日={trade_date}, 等待")

    # ===== Step 3: 更新净值（卖出+买入后） =====
    equity = update_daily_equity(state, conn, trade_date)

    # ===== Step 4: 如果空仓，查找今日选股结果，设为待买入 =====
    if not state['positions'] and not state.get('pending_buy'):
        sel_date = trade_date
        sel_data = load_selector_result(sel_date)
        if sel_data is None:
            # 尝试前一交易日（兼容补跑场景）
            prev_date = get_prev_trade_date(conn, trade_date)
            if prev_date:
                sel_data = load_selector_result(prev_date)
                sel_date = prev_date

        if sel_data is not None:
            buy_date = get_next_trade_date(conn, sel_date)
            if buy_date:
                state['pending_buy'] = {
                    'select_date': sel_date,
                    'buy_date': buy_date,
                    'select_count': len(sel_data.get('stocks', [])),
                    'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                }
                state['last_select_date'] = sel_date
                print(f"  📋 记录待买入: 选股日={sel_date} → 买入日={buy_date} (Top{sel_data.get('top_n', '?')})")
            else:
                print(f"  ⚠️ 选股日={sel_date} 后无交易日，跳过")
        else:
            print(f"  ℹ️ {trade_date} 无选股结果，空仓等待")

    # ===== Step 5: 记录最后交易日 + 保存状态 =====
    state['last_trade_date'] = trade_date
    save_state(state)

    # ===== Step 6: 生成每日报告 =====
    report_path = generate_daily_report(state, trade_date, sold, buy_result, equity)
    print(f"  📝 日志: {report_path}")

    conn.close()

    # 汇总
    print(f"\n  {'='*50}")
    nv = equity['equity_pct'] / 100
    print(f"  净值: {nv:.4f} | 累计: {nv-1:+.2f}%")
    print(f"  资金: ¥{equity['capital']:,.2f} | 持仓: {len(state['positions'])} 只")
    if state.get('pending_buy'):
        pb = state['pending_buy']
        print(f"  待买入: 选股日={pb['select_date']} → 买入日={pb['buy_date']}")

    return state


def show_status():
    """查看当前状态"""
    state = load_state()
    if state is None:
        print("  ❌ 模拟账户未初始化，请先 --init")
        return

    conn = get_db_conn()
    name_map = load_stock_names(conn)

    print(f"\n  {'='*50}")
    print(f"  多因子选股模拟交易")
    print(f"  {'='*50}")
    print(f"  本金:     ¥{state['initial_capital']:,}")
    print(f"  起始日:   {state['start_date']}")
    print(f"  最后交易: {state['last_trade_date']}")
    print(f"  可用资金: ¥{state['capital']:,.2f}")

    stats = state['stats']
    n_trades = stats['total_trades']
    win_rate = stats['win_trades'] / max(n_trades, 1) * 100
    print(f"  累计交易: {n_trades} 笔")
    print(f"  累计胜率: {win_rate:.1f}%")

    # 待买入
    pending = state.get('pending_buy')
    if pending:
        print(f"\n  📋 待买入: 选股日={pending['select_date']} → 买入日={pending['buy_date']}")

    # 当前持仓
    positions = state['positions']
    print(f"\n  📦 当前持仓 ({len(positions)} 只)")
    if positions:
        # 获取最新价格
        latest = pd.read_sql("SELECT MAX(trade_date) as d FROM daily_kline", conn)
        latest_date = str(latest['d'].iloc[0])

        total_pos_value = 0
        for pos in positions:
            price = get_price(conn, pos['ts_code'], latest_date, 'close')
            if price is None:
                price = pos['buy_price']
            pnl = (price / pos['buy_price'] - 1) * 100
            mv = price * pos['shares']
            total_pos_value += mv
            wl = "🔴" if pnl > 0 else "🟢"
            hold_target = pos.get('hold_days_target', get_active_params()['hold_days'])
            print(f"    {pos['name']}({pos['ts_code']}) | "
                  f"¥{pos['buy_price']:.2f}→¥{price:.2f} | "
                  f"{pnl:+.2f}% {wl} | {pos['hold_days']}/{hold_target}天")

        total_equity = state['capital'] + total_pos_value
        total_ret = (total_equity / state['initial_capital'] - 1) * 100
        print(f"\n  持仓市值: ¥{total_pos_value:,.2f}")
        print(f"  总资产:   ¥{total_equity:,.2f}")
        print(f"  累计收益: {total_ret:+.2f}%")
    else:
        print(f"    （空仓）")

    conn.close()


def generate_weekly_report(state):
    """生成周报"""
    daily = state.get('daily_equity', [])
    if len(daily) < 2:
        print("  ⚠️ 数据不足，无法生成周报")
        return

    df = pd.DataFrame(daily)
    df['trade_date'] = pd.to_datetime(df['trade_date'], format='%Y%m%d')

    # 最近7个交易日的表现
    recent = df.tail(7)
    week_ret = (recent['equity_pct'].iloc[-1] - recent['equity_pct'].iloc[0])

    # 净值曲线
    equities = df['equity_pct'].values / 100
    peak = np.maximum.accumulate(equities)
    drawdown = (equities - peak) / peak
    max_dd = drawdown.min() * 100

    total_ret = (df['equity_pct'].iloc[-1] - 100)

    lines = [
        f"# 模拟交易周报",
        "",
        f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"> 区间: {df['trade_date'].iloc[0].strftime('%Y-%m-%d')} ~ {df['trade_date'].iloc[-1].strftime('%Y-%m-%d')}",
        "",
        "## 绩效概览",
        "",
        f"| 指标 | 值 |",
        f"| --- | --- |",
        f"| 总资产 | ¥{df['total_equity'].iloc[-1]:,.2f} |",
        f"| 累计收益 | {total_ret:+.2f}% |",
        f"| 本周收益 | {week_ret:+.2f}% |",
        f"| 最大回撤 | {max_dd:.2f}% |",
        f"| 当前净值 | {equities[-1]:.4f} |",
        f"| 累计交易 | {state['stats']['total_trades']} 笔 |",
        f"| 胜率 | {state['stats']['win_trades']/max(state['stats']['total_trades'],1)*100:.1f}% |",
        f"| 累计手续费 | ¥{state['stats']['total_fee']:,.2f} |",
        "",
    ]

    # 最近5笔交易
    trades = state.get('closed_trades', [])
    if trades:
        lines.append("## 最近交易")
        lines.append("")
        lines.append(f"| 卖出日 | 代码 | 名称 | 净收益% | 持仓天数 |")
        lines.append(f"| --- | --- | --- | --- | --- |")
        for t in trades[-5:]:
            wl = "🔴" if t['net_ret_pct'] > 0 else "🟢"
            lines.append(
                f"| {t['sell_date']} | {t['ts_code']} | {t['name']} | "
                f"{t['net_ret_pct']:+.2f}% {wl} | {t.get('hold_days_actual', '-')}天 |"
            )
        lines.append("")

    lines.extend([
        "---",
        f"*模拟交易，仅供研究参考。*",
    ])

    report_date = df['trade_date'].iloc[-1].strftime('%Y%m%d')
    report_path = WEEKLY_DIR / f"{report_date}_weekly.md"
    report_path.write_text("\n".join(lines), encoding='utf-8')
    print(f"  📊 周报: {report_path}")
    return report_path


def main():
    args = sys.argv[1:]

    if '--init' in args:
        # 初始化
        start_date = None
        i = args.index('--init') + 1
        if i < len(args) and not args[i].startswith('-'):
            start_date = args[i].replace('-', '')[:8]
        init_state(start_date)
        return

    if '--status' in args:
        show_status()
        return

    if '--report' in args:
        state = load_state()
        if state:
            generate_weekly_report(state)
        else:
            print("  ❌ 未初始化")
        return

    # 默认：执行每日交易
    force_date = None
    if '--force-date' in args:
        i = args.index('--force-date') + 1
        if i < len(args):
            force_date = args[i].replace('-', '')[:8]

    run_daily(force_date)


if __name__ == '__main__':
    main()
