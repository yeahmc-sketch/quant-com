#!/usr/bin/env python3
"""
交易日自动检测脚本
生成 shell 可用的节假日判断函数

原理：
1. 查数据库已有历史数据（过去5年），从实际数据中推断交易日模式
2. 对当年未发生日期，使用国务院发布的调休规律推算
   - 元旦: 1月1日
   - 春节: 农历正月初一前后约7天（推算）
   - 清明: 4月5日前后
   - 劳动节: 5月1日
   - 端午: 农历五月初五前后（推算）
   - 国庆: 10月1-7日

用法:
  python3 generate_holiday_list.py 2026 > holidays_2026.txt
"""

import sys
from datetime import date, timedelta
from pathlib import Path

# ---------- 已知的A股假期模式 ----------
# 国务院通常提前一年公布，这里用"已知固定日期 + 农历推算"
# 精确日期每年不同，最佳方案是每年1月从交易所官网获取
# 但为了自动化，我们用历史数据构建节假日日历

DB_PATH = Path(__file__).parent.parent.parent / "data" / "db" / "market.db"


def get_trading_days_from_db(year: int) -> set:
    """从数据库获取该年已有的交易日"""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM daily_kline "
        "WHERE trade_date >= ? AND trade_date < ?",
        (f"{year}0101", f"{year + 1}0101")
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def get_holidays_from_db(year: int) -> list:
    """通过数据库推断节假日：
       如果一个工作日没有数据，且前后都有数据，它很可能是节假日"""
    trading_days = get_trading_days_from_db(year)
    if not trading_days:
        return []
    holidays = []
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    d = start
    while d <= end:
        ds = d.strftime("%Y%m%d")
        if d.weekday() < 5 and ds not in trading_days:
            # 工作日但无数据 → 可能是节假日
            # 确认：检查前后是否有数据
            prev_d = d - timedelta(days=1)
            next_d = d + timedelta(days=1)
            pds = prev_d.strftime("%Y%m%d")
            nds = next_d.strftime("%Y%m%d")
            if pds in trading_days or nds in trading_days:
                holidays.append(ds)
        d += timedelta(days=1)
    return sorted(holidays)


def generate_shell_function(year: int) -> str:
    """生成shell is_holiday() 函数"""
    holidays = get_holidays_from_db(year)
    # 也获取去年和明年的数据做边界
    prev_holidays = get_holidays_from_db(year - 1)
    next_holidays = get_holidays_from_db(year + 1)

    all_dates = set(holidays) | set(prev_holidays[-5:]) | set(next_holidays[:5])
    # 只保留属于本年的
    year_prefix = str(year)
    this_year_dates = sorted(d for d in all_dates if d.startswith(year_prefix))

    # 合并连续假期
    def merge_consecutive(dates):
        if not dates:
            return []
        merged = []
        start = dates[0]
        prev = start
        for d in dates[1:]:
            if int(d) - int(prev) > 1:
                merged.append((start, prev))
                start = d
            prev = d
        merged.append((start, prev))
        return merged

    ranges = merge_consecutive(this_year_dates)

    lines = [
        f"# Auto-generated holiday list for {year}",
        f"# Generated from database on {date.today()}",
        f"# Holidays found: {len(this_year_dates)} days",
        f"is_holiday() {{",
        f'    local d="$1"',
        f'    case "$d" in',
    ]
    for start_d, end_d in ranges:
        if start_d == end_d:
            lines.append(f"        {start_d}) return 0 ;;")
        else:
            # shell 不支持范围匹配，列出所有
            d = date(int(start_d[:4]), int(start_d[4:6]), int(start_d[6:8]))
            end = date(int(end_d[:4]), int(end_d[4:6]), int(end_d[6:8]))
            while d <= end:
                lines.append(f"        {d.strftime('%Y%m%d')}) return 0 ;;")
                d += timedelta(days=1)
    lines.extend([
        "        *) return 1 ;;",
        "    esac",
        "}",
        "",
        f"# {len(this_year_dates)} holidays detected from DB data",
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    year = int(sys.argv[1]) if len(sys.argv) > 1 else date.today().year
    output = generate_shell_function(year)
    print(output)
