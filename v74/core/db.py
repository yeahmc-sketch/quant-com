#!/usr/bin/env python3
"""
DB01 SQLite 数据库核心模块
===========================
提供统一的数据库连接、查询、数据写入接口。
"""

import sqlite3
import threading
from pathlib import Path
from contextlib import contextmanager
from typing import Optional, List, Tuple, Any

# ===== 路径配置 =====
DB_DIR = Path("/Users/chenshi/WorkBuddy/Claw/data/db")
DB_DIR.mkdir(parents=True, exist_ok=True)

MARKET_DB = DB_DIR / "market.db"
CANDIDATE_DB = DB_DIR / "candidates.db"


# ===== 主库连接（线程安全） =====
_local = threading.local()


def get_market_conn() -> sqlite3.Connection:
    """获取市场数据库连接（线程单例）"""
    if not hasattr(_local, "market"):
        _local.market = sqlite3.connect(
            str(MARKET_DB),
            check_same_thread=False,
            timeout=60.0,
        )
        _local.market.execute("PRAGMA journal_mode=WAL")
        _local.market.execute("PRAGMA synchronous=NORMAL")
        _local.market.execute("PRAGMA cache_size=-64000")  # 64MB
        _local.market.row_factory = sqlite3.Row
    return _local.market


def get_candidate_conn() -> sqlite3.Connection:
    """获取候选股数据库连接（线程单例）"""
    if not hasattr(_local, "candidate"):
        _local.candidate = sqlite3.connect(
            str(CANDIDATE_DB),
            check_same_thread=False,
            timeout=60.0,
        )
        _local.candidate.execute("PRAGMA journal_mode=WAL")
        _local.candidate.execute("PRAGMA synchronous=NORMAL")
        _local.candidate.row_factory = sqlite3.Row
    return _local.candidate


@contextmanager
def market_cursor():
    """市场库上下文管理器"""
    conn = get_market_conn()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


@contextmanager
def candidate_cursor():
    """候选股库上下文管理器"""
    conn = get_candidate_conn()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


# ===== 初始化表结构 =====
MARKET_SCHEMA = """
-- 股票基础信息表（去重，只存最新记录）
CREATE TABLE IF NOT EXISTS stocks (
    ts_code    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    area       TEXT DEFAULT '',
    industry   TEXT DEFAULT '',
    market     TEXT DEFAULT '',
    exchange   TEXT DEFAULT '',
    list_date  TEXT DEFAULT '',
    asset_type TEXT DEFAULT 'stock'
);

-- ═══════════════════════════════════════════════════════════════════════
-- 日线行情表（核心数据）
-- ⚠️ 数据状态: 前复权（东方财富API fqt=1）
--    所有策略（V15/V15SS/V15SS-N/Fusion20）都直接读此表 close 字段。
--    不要对这个表做不复权转换！否则历史与当前价格会不一致。
--    如需覆写历史数据，必须用 --force 参数（见 download_snapshot_sqlite.py）。
-- 数据来源: 东方财富K线API（push2his.eastmoney.com，fqt=1）
-- 每次写入: DELETE 当天旧数据 + INSERT 新数据，不影响其他日期
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS daily_kline (
    ts_code    TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open       REAL DEFAULT 0,
    high       REAL DEFAULT 0,
    low        REAL DEFAULT 0,
    close      REAL DEFAULT 0,
    pre_close  REAL DEFAULT 0,
    pct_chg    REAL DEFAULT 0,
    vol        REAL DEFAULT 0,
    amount     REAL DEFAULT 0,
    adj_factor REAL DEFAULT 1.0,
    PRIMARY KEY (ts_code, trade_date)
);

-- 日线表索引（加速查询）
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_kline(trade_date);
CREATE INDEX IF NOT EXISTS idx_daily_code ON daily_kline(ts_code);
CREATE INDEX IF NOT EXISTS idx_daily_pct  ON daily_kline(pct_chg);

-- 概念板块热度表
CREATE TABLE IF NOT EXISTS concept_heat (
    trade_date TEXT NOT NULL,
    concept_code TEXT NOT NULL,
    concept_name TEXT NOT NULL,
    heat_score  REAL DEFAULT 0,
    pct_chg     REAL DEFAULT 0,
    PRIMARY KEY (trade_date, concept_code)
);

-- 大盘指数日线
CREATE TABLE IF NOT EXISTS index_daily (
    ts_code    TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open       REAL DEFAULT 0,
    high       REAL DEFAULT 0,
    low        REAL DEFAULT 0,
    close      REAL DEFAULT 0,
    pct_chg    REAL DEFAULT 0,
    PRIMARY KEY (ts_code, trade_date)
);
"""

CANDIDATE_SCHEMA = """
-- 每日预筛选候选股池
CREATE TABLE IF NOT EXISTS pre_pool (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_date  TEXT NOT NULL,
    ts_code    TEXT NOT NULL,
    name       TEXT NOT NULL,
    pre_close  REAL DEFAULT 0,
    ma20_amount REAL DEFAULT 0,
    vol_ratio  REAL DEFAULT 0,
    amount_yi  REAL DEFAULT 0,
    zt_count   INTEGER DEFAULT 0,
    market_cap REAL DEFAULT 0,
    turnover   REAL DEFAULT 0,
    score      REAL DEFAULT 0,
    reason     TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(pool_date, ts_code)
);

CREATE INDEX IF NOT EXISTS idx_prepool_date ON pre_pool(pool_date);
CREATE INDEX IF NOT EXISTS idx_prepool_score ON pre_pool(pool_date, score DESC);

-- DB01 买入信号记录
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date   TEXT NOT NULL,
    ts_code       TEXT NOT NULL,
    name          TEXT NOT NULL,
    buy_price     REAL NOT NULL,
    limit_up      REAL NOT NULL,
    stop_loss     REAL NOT NULL,
    pct_chg       REAL DEFAULT 0,
    pre_close     REAL DEFAULT 0,
    amount_yi     REAL DEFAULT 0,
    trigger_time  TEXT DEFAULT '',
    trigger_date  TEXT NOT NULL,
    status        TEXT DEFAULT 'pending',
    created_at    TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_signals_date  ON signals(signal_date);
CREATE INDEX IF NOT EXISTS idx_signals_code  ON signals(ts_code);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);

-- 持仓记录
CREATE TABLE IF NOT EXISTS holdings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_code     TEXT NOT NULL,
    name        TEXT NOT NULL,
    buy_date    TEXT NOT NULL,
    buy_price   REAL NOT NULL,
    quantity    REAL DEFAULT 0,
    cost        REAL DEFAULT 0,
    status      TEXT DEFAULT 'holding',
    close_date  TEXT DEFAULT '',
    sell_price  REAL DEFAULT 0,
    profit_pct  REAL DEFAULT 0,
    notes       TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_holdings_status ON holdings(status);
CREATE INDEX IF NOT EXISTS idx_holdings_code   ON holdings(ts_code);

-- 回测结果存档
CREATE TABLE IF NOT EXISTS backtest_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy    TEXT NOT NULL,
    period      TEXT NOT NULL,
    trades      INTEGER DEFAULT 0,
    win_rate    REAL DEFAULT 0,
    total_ret   REAL DEFAULT 0,
    max_dd      REAL DEFAULT 0,
    profit_factor REAL DEFAULT 0,
    Sharpe      REAL DEFAULT 0,
    params      TEXT DEFAULT '{}',
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 数据库元数据表（数据来源、复权状态、版本等信息）
CREATE TABLE IF NOT EXISTS db_metadata (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def init_market_db():
    """初始化市场数据库"""
    with market_cursor() as cur:
        cur.executescript(MARKET_SCHEMA)
    print(f"✅ 市场库初始化完成: {MARKET_DB}")


def init_candidate_db():
    """初始化候选股数据库"""
    with candidate_cursor() as cur:
        cur.executescript(CANDIDATE_SCHEMA)
    print(f"✅ 候选库初始化完成: {CANDIDATE_DB}")


def get_latest_date() -> Optional[str]:
    """获取数据库中最新交易日期"""
    with market_cursor() as cur:
        cur.execute("SELECT MAX(trade_date) FROM daily_kline")
        row = cur.fetchone()
        return row[0] if row else None


def get_stock_list() -> List[Tuple[str, str]]:
    """获取所有股票代码列表"""
    with market_cursor() as cur:
        cur.execute("SELECT ts_code, name FROM stocks ORDER BY ts_code")
        return cur.fetchall()


# ===== 快捷查询函数 =====

def query_daily_kline(
    ts_code: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[sqlite3.Row]:
    """
    查询单只股票的日线数据
    """
    sql = "SELECT * FROM daily_kline WHERE ts_code = ?"
    params: List[Any] = [ts_code]

    if start_date:
        sql += " AND trade_date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND trade_date <= ?"
        params.append(end_date)

    sql += " ORDER BY trade_date ASC"
    if limit:
        sql += f" LIMIT {limit}"

    with market_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_market_snapshot(date: str) -> List[sqlite3.Row]:
    """
    查询某一天全市场快照
    """
    with market_cursor() as cur:
        cur.execute(
            "SELECT * FROM daily_kline WHERE trade_date = ? ORDER BY ts_code",
            (date,)
        )
        return cur.fetchall()


def batch_insert_daily(rows: List[Tuple], batch_size: int = 5000):
    """
    批量写入日线数据（用于迁移或日常下载）
    rows: [(ts_code, trade_date, open, high, low, close, ...), ...]
    """
    sql = """
    INSERT OR REPLACE INTO daily_kline
        (ts_code, trade_date, open, high, low, close, pre_close,
         pct_chg, vol, amount, adj_factor)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with market_cursor() as cur:
        for i in range(0, len(rows), batch_size):
            cur.executemany(sql, rows[i:i+batch_size])
        print(f"  写入 {len(rows)} 条日线数据")


if __name__ == "__main__":
    print("初始化数据库...")
    init_market_db()
    init_candidate_db()
    print(f"最新日期: {get_latest_date()}")
    print(f"股票数: {len(get_stock_list())}")
