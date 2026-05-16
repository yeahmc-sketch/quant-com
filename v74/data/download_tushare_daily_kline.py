#!/usr/bin/env python3
"""
Tushare Pro daily 前复权日线下载 —— 替代东方财富 fqt=1
用法：
  python3 download_tushare_daily_kline.py                 # 补全所有缺失
  python3 download_tushare_daily_kline.py 2025            # 只处理2025年
  python3 download_tushare_daily_kline.py 2025 2026       # 批量年份
  python3 download_tushare_daily_kline.py --today         # 每日增量（launchd用）

按交易日维度批量拉取，INSERT OR REPLACE 覆盖原数据（东财→Tushare）。
"""
import sys, time, json, urllib.request, sqlite3
from pathlib import Path, PurePath

PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_PATH     = PROJECT_DIR / "data" / "db" / "market.db"
TOKEN       = '2e50aa62898e603850c324723dbcf05fbb5fa671c6160d26e4593f41'
DELAY       = 0.35  # Tushare 200次/分钟 = 0.3s，余量0.35


def ts_call(api, params=None):
    for _ in range(3):
        try:
            payload = json.dumps({
                'api_name': api, 'token': TOKEN, 'params': params or {}
            }).encode()
            req = urllib.request.Request(
                'https://api.tushare.pro', data=payload,
                headers={'Content-Type': 'application/json'}
            )
            result = json.loads(urllib.request.urlopen(req, timeout=30).read().decode('utf-8'))
            if result.get('code') == 0:
                return result['data']
            time.sleep(3)
        except Exception:
            time.sleep(3)
    return None


def get_trading_dates(conn, years):
    """从 daily_kline 或 stocks 获取交易日列表"""
    if years:
        clauses = ' OR '.join(f"trade_date LIKE '{y}%'" for y in years)
        return sorted(r[0] for r in conn.execute(
            f'SELECT DISTINCT trade_date FROM daily_kline WHERE {clauses} ORDER BY trade_date'
        ).fetchall())
    else:
        return sorted(r[0] for r in conn.execute(
            'SELECT DISTINCT trade_date FROM daily_kline ORDER BY trade_date'
        ).fetchall())


def main():
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # ── 解析参数 ──────────────────────────────────────────────────────────
    args = sys.argv[1:]
    if '--today' in args:
        from datetime import date
        today = date.today().strftime('%Y%m%d')
        dates_to_download = [today]
        print(f"今日增量: {today}", flush=True)
    else:
        years = [a for a in args if a.isdigit() and len(a) == 4]
        dates_to_download = get_trading_dates(conn, years or None)
        if years:
            print(f"指定年份: {years}", flush=True)
        else:
            print(f"全部交易日: {len(dates_to_download)} 天", flush=True)
            if len(dates_to_download) > 500:
                print("  ⚠️ 数据量大，建议分年份执行", flush=True)

    db_min = conn.execute('SELECT MIN(trade_date) FROM daily_kline').fetchone()[0]
    db_max = conn.execute('SELECT MAX(trade_date) FROM daily_kline').fetchone()[0]
    print(f"DB当前: {db_min} ~ {db_max}", flush=True)

    total_all = 0
    total_dates = len(dates_to_download)
    t0 = time.time()
    req_count = 0       # 计数请求次数
    req_reset = time.time()  # 每分钟重置

    for i, td in enumerate(dates_to_download):
        # ── 限速控制：确保 ≤ 190次/分钟 ────────────────────────────────────
        req_count += 1
        elapsed_60s = time.time() - req_reset
        if elapsed_60s >= 60:
            req_count = 0
            req_reset = time.time()
        elif req_count >= 190:
            wait = 60 - elapsed_60s + 1
            print(f"  ⚠️ 接近限速上限({req_count}次/分钟)，暂停 {wait:.0f}s", flush=True)
            time.sleep(wait)
            req_count = 0
            req_reset = time.time()

        # ── 进度显示 ────────────────────────────────────────────────────────
        pct = (i + 1) / total_dates * 100

        # 进度条
        bar_len = 30
        filled = int(bar_len * (i + 1) // total_dates)
        bar = '█' * filled + '░' * (bar_len - filled)
        rate = req_count / max(elapsed_60s, 1)
        print(f"\r  [{bar}] {pct:.0f}%  {td}  {rate:.0f}次/分  ", end='', flush=True)
        data = ts_call('daily', {
            'trade_date': td,
            'adj': 'qfq',  # 前复权，匹配东财 fqt=1
        })
        if not data or not data.get('items'):
            time.sleep(DELAY)
            continue

        rows = []
        for item in data['items']:
            try:
                rows.append((
                    item[0],                       # ts_code
                    str(item[1]),                  # trade_date
                    round(float(item[2] or 0), 4), # open
                    round(float(item[3] or 0), 4), # high
                    round(float(item[4] or 0), 4), # low
                    round(float(item[5] or 0), 4), # close
                    round(float(item[6] or 0), 4), # pre_close（前复权后）
                    round(float(item[8] or 0), 4), # pct_chg
                    float(item[9] or 0),            # vol（手）
                    round(float(item[10] or 0) / 10000, 4),  # amount 万元→亿元
                    1.0,                            # adj_factor（已前复权）
                ))
            except (IndexError, ValueError):
                continue

        if rows:
            cur.executemany('''INSERT OR REPLACE INTO daily_kline
                (ts_code, trade_date, open, high, low, close,
                 pre_close, pct_chg, vol, amount, adj_factor)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''', rows)
            conn.commit()
            total_all += len(rows)

        time.sleep(DELAY)

    # 完成提示
    # 完成提示
    elapsed = time.time() - t0
    avg_rpm = total_dates / (elapsed / 60) if elapsed > 0 else 0
    print(f"\n✅ 完成! 共处理 {total_all:,} 行, {total_dates} 个交易日, 耗时 {elapsed:.0f}s (均 {avg_rpm:.0f}次/分)", flush=True)
    conn.close()

    # --today 模式：下载 0 行视为异常（Tushare 数据未就绪或网络故障）
    if '--today' in sys.argv and total_all == 0:
        print(f"⚠️ --today 模式但下载了 0 行数据，疑似 Tushare 数据未就绪或网络故障", flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
