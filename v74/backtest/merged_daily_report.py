#!/usr/bin/env python3
"""
5策略赛马 — 合并每日报告
========================
读取5个策略的状态文件，生成一封完整的HTML邮件。
每个策略独立区块，含收益/胜率/回撤/持仓明细。

由 launchd 在 18:00 触发（所有策略跑完后）。
"""
import sys, json, os, tempfile, subprocess, sqlite3, base64
from pathlib import Path
from datetime import datetime
import pandas as pd

CLAW = Path('/Users/chenshi/WorkBuddy/Claw')
V10 = Path('/Users/chenshi/WorkBuddy/20260412151307/v10')
DB_PATH = CLAW / 'data' / 'db' / 'market.db'
INIT_CASH = 50000

# 股票中文名缓存
_NAME_CACHE = {}
def _load_name_cache():
    if _NAME_CACHE: return
    try:
        conn = sqlite3.connect(str(DB_PATH))
        for r in conn.execute('SELECT ts_code, name FROM stocks').fetchall():
            _NAME_CACHE[r[0]] = r[1]
        conn.close()
    except:
        pass

def _get_name(sym):
    _load_name_cache()
    return _NAME_CACHE.get(sym, '')

def _last_trade_date():
    """获取数据库中最新交易日"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        r = conn.execute('SELECT MAX(trade_date) FROM daily_kline').fetchone()
        conn.close()
        return str(r[0]) if r and r[0] else None
    except:
        return None

STRATEGIES = [
    {'id':'b2','name':'B2 爆发力','badge':'B2','color':'#e65100',
     'params':'+2xLGBM Top2-10d · 无择时',
     'sf':CLAW/'output/v74/portfolio/b2_trade_state.json','nk':'nav','tp':True},
    {'id':'mf','name':'MF v2.3','badge':'MF','color':'#7f77dd',
     'params':'Fusion20 Top3-10d · DMA择时',
     'sf':CLAW/'output/v74/portfolio/mf22_trade_state.json','nk':'nav','tp':True},
    # V15系列已暂停（2026-05-14），保留配置便于恢复
    # {'id':'v15','name':'V15','badge':'V15','color':'#1976d2',
    #  'params':'板轮动+EMA · 4只30天 · LGBM过滤',
    #  'sf':V10/'v15_paper_portfolio.json','nk':'total_value','tp':False},
    # {'id':'v15ss','name':'V15SS','badge':'SS','color':'#00897b',
    #  'params':'超短N字波 · 98%保护 · MAX_POS=6',
    #  'sf':V10/'v15ss_paper_portfolio.json','nk':'total_value','tp':False},
    # {'id':'v15ssn','name':'V15SS N','badge':'N','color':'#546e7a',
    #  'params':'超短N字波 · MAX_POS=6',
    #  'sf':V10/'v15ss_n_paper_portfolio.json','nk':'total_value','tp':False},
]


def load_one(cfg):
    try:
        with open(cfg['sf']) as f:
            s = json.load(f)
    except:
        return None

    pos = s.get('positions', {})
    cash = s.get('cash', 0)
    last = s.get('last_date', s.get('last_update', ''))
    init = s.get('init_cash', INIT_CASH)

    # NAV
    nav = s.get(cfg['nk'], 0)
    if nav == 0:
        eq = s.get('equity_curve', [])
        if eq:
            nav = eq[-1].get('nav', eq[-1].get('total_value', 0))
    ret = (nav / init - 1) * 100 if init > 0 else 0

    # 胜率
    trades = s.get('trades', [])
    trs = []
    for t in trades:
        r = t.get('return_pct', t.get('realized_pnl_pct', 0))
        if isinstance(r, (int, float)) and r != 0:
            trs.append(r)
    n_closed = len(trs)
    wr = sum(1 for r in trs if r > 0) / n_closed * 100 if n_closed > 0 else 0

    # 最大回撤
    eq = s.get('equity_curve', [])
    mdd = 0
    if eq:
        peak = init
        for e in eq:
            v = e.get('nav', e.get('total_value', 0))
            if v > peak:
                peak = v
            dd = (peak - v) / peak * 100 if peak > 0 else 0
            if dd > mdd:
                mdd = dd

    # 现价（用数据库最新交易日，不是状态的last_date，避免非交易日/数据出错）
    ref_date = _last_trade_date()
    if ref_date:
        try:
            conn = sqlite3.connect(str(DB_PATH))
            for sym, p in pos.items():
                cur = conn.cursor()
                cur.execute("SELECT close FROM daily_kline WHERE ts_code=? AND trade_date=?",
                           (sym, ref_date))
                row = cur.fetchone()
                p['_cp'] = float(row[0]) if row and row[0] else p.get('entry_price', 0)
            conn.close()
        except:
            for sym, p in pos.items():
                p['_cp'] = p.get('entry_price', 0)
    else:
        for sym, p in pos.items():
            p['_cp'] = p.get('entry_price', 0)

    # B2策略：持仓天数用交易日（与调仓逻辑一致，HOLD_DAYS=10个交易日）
    for sym, p in pos.items():
        ep = p.get('entry_price', 0)
        p['_pnl'] = (p['_cp'] - ep) / ep * 100 if ep > 0 else 0

    if cfg['id'] == 'b2' and pos:
        try:
            entry_dates = [str(p.get('entry_date', '')) for p in pos.values() if p.get('entry_date')]
            if entry_dates:
                earliest = min(entry_dates)
                conn = sqlite3.connect(str(DB_PATH))
                td_count = pd.read_sql(f"""
                    SELECT COUNT(DISTINCT trade_date) FROM daily_kline
                    WHERE trade_date >= '{earliest}'
                      AND trade_date <= '{ref_date or last}'
                """, conn)
                conn.close()
                b2_td = int(td_count.iloc[0, 0])
                for p in pos.values():
                    p['_hd'] = b2_td
            else:
                for p in pos.values():
                    p['_hd'] = 0
        except:
            for p in pos.values():
                p['_hd'] = 0
    else:
        for sym, p in pos.items():
            ep = p.get('entry_price', 0)
            ed = p.get('entry_date', last)
            try:
                d1 = datetime.strptime(str(ed), '%Y%m%d')
                d2 = datetime.strptime(str(last), '%Y%m%d') if last else datetime.now()
                p['_hd'] = (d2 - d1).days
            except:
                p['_hd'] = 0

    return {
        'name': cfg['name'], 'badge': cfg['badge'], 'color': cfg['color'],
        'params': cfg['params'], 'init': init, 'cash': cash,
        'nav': nav, 'ret': ret, 'wr': wr, 'mdd': mdd,
        'last': last, 'pos': pos, 'pc': len(pos),
        'trades': trades, 'n_closed': n_closed,
    }


def get_hs300():
    try:
        conn = sqlite3.connect(str(DB_PATH))
        idx = pd.read_sql("""
            SELECT trade_date, close FROM v9_index_daily
            WHERE ts_code='000300.SH' ORDER BY trade_date DESC LIMIT 12
        """, conn)
        conn.close()
        if len(idx) >= 2:
            idx = idx.sort_values('trade_date').reset_index(drop=True)
            d10 = idx.tail(10)
            if len(d10) >= 2:
                last_c = float(d10.iloc[-1]['close'])
                first_c = float(d10.iloc[0]['close'])
                r10 = (last_c - first_c) / first_c * 100
                return f'{r10:+.2f}%', f'{last_c:,.2f}', r10 >= 0
    except:
        pass
    return 'N/A', 'N/A', True


def build_strategy_block(s, ref_date):
    rc = 'c-up' if s['ret'] >= 0 else 'c-down'
    wr_s = f'{s["wr"]:.0f}%' if s['n_closed'] > 0 else 'N/A'
    mdd_s = f'{s["mdd"]:.1f}%'

    pos_rows = ''
    for sym, p in sorted(s['pos'].items()):
        pnl_c = 'c-up' if p['_pnl'] >= 0 else 'c-down'
        tags = ''
        if 'vol_ratio' in p:
            tags += f'<span class="pt">{p["vol_ratio"]:.1f}x</span>'
        if 'rsi' in p:
            tags += f'<span class="pt">RSI{p["rsi"]:.0f}</span>'
        pos_rows += (
            f'<tr><td><b>{p.get("name", _get_name(sym) or sym)}</b><br><span class="pc">{sym}</span></td>'
            f'<td>&yen;{p["entry_price"]:.2f}</td>'
            f'<td>&yen;{p["_cp"]:.2f}</td>'
            f'<td class="{pnl_c}">{p["_pnl"]:+.2f}%</td>'
            f'<td>{p["_hd"]}d</td><td>{p["shares"]}</td><td>{tags}</td></tr>'
        )
    if not pos_rows:
        pos_rows = '<tr><td colspan="7" class="empty">空仓</td></tr>'

    # 今日卖出
    today_sells = [t for t in s['trades']
                   if t.get('exit_date') == ref_date or t.get('sell_date') == ref_date]
    # 今日买入（从当前持仓中找 entry_date=今天的）
    today_buys = [p for sym, p in s['pos'].items()
                  if p.get('entry_date') == ref_date]

    trade_items = []
    for t in today_sells:
        tr = t.get('return_pct', t.get('realized_pnl_pct', 0))
        tc = 'c-up' if tr >= 0 else 'c-down'
        sym = t.get('code', t.get('ts_code',''))
        name = t.get('name', '') or _get_name(sym)
        display = f'{name}({sym})' if name and sym else (name or sym)
        trade_items.append(f'<b>{name}</b> <span class="pc">{sym}</span> 卖出 <span class="{tc}">{tr:+.2f}%</span>')
    for p in today_buys:
        sym = p.get('ts_code', p.get('code', ''))
        name = p.get('name', '') or _get_name(sym)
        name_d = name or sym
        price = p.get('entry_price', 0)
        shares = p.get('shares', 0)
        trade_items.append(f'<b>{name_d}</b> <span class="pc">{sym}</span> 买入 ¥{price:.2f} × {shares}股')

    trade_sec = ''
    if trade_items:
        trade_html = ''.join(f'<div class="ti">{item}</div>' for item in trade_items)
        trade_sec = f'<div class="sc"><div class="st">📋 今日交易</div>{trade_html}</div>'

    return f'''<div class="card scard" style="border-top:4px solid {s["color"]}">
<div class="ch"><span class="bdg" style="background:{s["color"]}">{s["badge"]}</span>
<span class="sn">{s["name"]}</span><span class="sp">{s["params"]}</span></div>
<div class="sg">
<div class="sb"><div class="sv {rc}">{s["ret"]:+.2f}%</div><div class="sl">累计收益</div></div>
<div class="sb"><div class="sv">&yen;{s["nav"]:,.0f}</div><div class="sl">总权益</div></div>
<div class="sb"><div class="sv">&yen;{s["cash"]:,.0f}</div><div class="sl">可用现金</div></div>
<div class="sb"><div class="sv">{s["pc"]}只</div><div class="sl">持仓</div></div>
<div class="sb"><div class="sv">{wr_s}</div><div class="sl">胜率</div></div>
<div class="sb"><div class="sv">{mdd_s}</div><div class="sl">最大回撤</div></div>
</div>
<table class="ptb"><thead><tr><th>股票</th><th>买入价</th><th>现价</th><th>浮盈</th><th>天数</th><th>股数</th><th>信号</th></tr></thead>
<tbody>{pos_rows}</tbody></table>{trade_sec}</div>'''


def build_html():
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    subj_str = datetime.now().strftime('%m-%d')

    hs_ret, hs_cls_str, hs_pos = get_hs300()
    hs_c = 'c-up' if hs_pos else 'c-down'

    ref_date = _last_trade_date()
    today_str = datetime.now().strftime('%Y%m%d')

    strategies = [load_one(c) for c in STRATEGIES if load_one(c)]
    ranked = sorted(strategies, key=lambda x: x['ret'], reverse=True)

    # 异常检测：如果DB最新交易日是今天，但所有策略的last_date都不是今天 → 全部跳过
    all_skipped = False
    skip_alert_html = ''
    if ref_date == today_str and strategies:
        non_today = [s for s in strategies if s['last'] != today_str]
        if len(non_today) == len(strategies):
            all_skipped = True
            skip_alert_html = '''<div class="card" style="border-left:4px solid #e53935;background:#fff5f5">
<div style="font-size:14px;font-weight:700;color:#e53935;margin-bottom:4px">⚠️ 异常告警：全部策略跳过今日执行</div>
<div style="font-size:12px;color:#666;line-height:1.6">
DB最新交易日为 {today_str}，但 {n} 个策略均未更新（可能 DB01 数据下载失败或策略脚本异常）。<br>
请检查：<b>DB01 日志</b> → 策略日志 → 确认数据完整性后手动补跑。
</div></div>'''.format(today_str=today_str, n=len(strategies))

    blocks = skip_alert_html + ''.join(build_strategy_block(s, ref_date) for s in strategies)

    medals = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣']
    rank_rows = ''
    for i, s in enumerate(ranked):
        rc = 'c-up' if s['ret'] >= 0 else 'c-down'
        wr_r = f'{s["wr"]:.0f}%' if s['n_closed'] > 0 else '-'
        rank_rows += (
            f'<tr><td>{medals[i] if i < 5 else ""}</td>'
            f'<td><span class="bs" style="background:{s["color"]}">{s["badge"]}</span> {s["name"]}</td>'
            f'<td class="{rc}">{s["ret"]:+.2f}%</td>'
            f'<td>&yen;{s["nav"]:,.0f}</td>'
            f'<td>{s["pc"]}只</td><td>{wr_r}</td><td>{s["mdd"]:.1f}%</td></tr>')

    html = f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
body{{font:-apple-system,sans-serif;max-width:680px;margin:0 auto;padding:16px;background:#f0f2f5;color:#333;font-size:14px}}
.hdr{{text-align:center;padding:16px 0}}
.hdr h1{{font-size:19px;margin:0 0 4px;color:#222}}
.hdr .dt{{color:#888;font-size:12px}}
.card{{background:white;border-radius:12px;padding:16px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,0.06)}}
.scard{{padding-bottom:12px}}
.ch{{display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap}}
.bdg{{display:inline-block;color:white;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.5px}}
.bs{{display:inline-block;color:white;padding:1px 5px;border-radius:3px;font-size:9px;font-weight:700}}
.sn{{font-size:14px;font-weight:600}}
.sp{{font-size:10px;color:#999;margin-left:auto}}
.sg{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;margin-bottom:10px}}
.sb{{background:#f8f9fa;border-radius:8px;padding:8px;text-align:center}}
.sv{{font-size:16px;font-weight:700}}
.sl{{font-size:9px;color:#888;margin-top:1px}}
.c-up{{color:#e53935}}.c-down{{color:#43a047}}
.ptb{{width:100%;border-collapse:collapse;font-size:11px}}
.ptb th{{background:#f8f9fa;padding:6px 4px;text-align:left;border-bottom:2px solid #eee;font-size:9px;color:#666;font-weight:500}}
.ptb td{{padding:5px 4px;border-bottom:1px solid #f0f0f0;vertical-align:middle}}
.ptb .empty{{text-align:center;color:#999;padding:12px}}
.pc{{font-size:9px;color:#999}}
.pt{{display:inline-block;background:#f0f0f0;color:#666;padding:1px 4px;border-radius:2px;font-size:8px;margin-right:2px}}
.sc{{background:#f8f9fa;border-radius:8px;padding:8px 10px;margin-top:8px}}
.st{{font-size:11px;font-weight:600;margin-bottom:6px}}
.ti{{font-size:11px;padding:4px 0;border-bottom:1px solid #eee}}
.ti:last-child{{border-bottom:none}}
.rt{{width:100%;border-collapse:collapse;font-size:12px}}
.rt th{{padding:8px 6px;text-align:left;border-bottom:2px solid #ddd;font-size:10px;color:#666;font-weight:500}}
.rt td{{padding:6px;border-bottom:1px solid #f0f0f0}}
.hsg{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.hb{{text-align:center;padding:8px}}
.hv{{font-size:22px;font-weight:700}}
.hl{{font-size:11px;color:#888;margin-top:2px}}
.ft{{text-align:center;color:#999;font-size:11px;padding:12px 0;line-height:1.6}}
</style></head><body>
<div class="hdr"><h1>📊 策略赛马 · 每日报告</h1><div class="dt">{now_str}</div></div>
<div class="card"><div class="hsg">
<div class="hb"><div class="hv {hs_c}">{hs_ret}</div><div class="hl">沪深300近10日</div></div>
<div class="hb"><div class="hv">{hs_cls_str}</div><div class="hl">收盘点位</div></div>
</div></div>
{blocks}
<div class="card" style="padding:12px 16px">
<div class="ch" style="margin-bottom:6px"><span style="font-size:14px;font-weight:600">🏆 赛马排名</span></div>
<table class="rt"><thead><tr><th></th><th>策略</th><th>收益</th><th>权益</th><th>持仓</th><th>胜率</th><th>回撤</th></tr></thead>
<tbody>{rank_rows}</tbody></table></div>
<div class="ft">数据维护 · launchd 18:00 自动发送<br>初始本金统一 &yen;50,000 · 自2026-04-22起</div>
</body></html>'''

    return html, subj_str


def send_email(html, subj_str):
    # RFC 2047 编码Subject（支持中文和Emoji）
    raw_subj = f'\U0001f4ca \u7b56\u7565\u8d5b\u9a6c\u62a5\u544a ({subj_str})'
    encoded_subj = '=?UTF-8?B?' + base64.b64encode(raw_subj.encode('utf-8')).decode() + '?='

    email_txt = (
        f'From: "\u6570\u636e\u7ef4\u62a4" <18313835@qq.com>\n'
        f'To: 18313835@qq.com\n'
        f'Subject: {encoded_subj}\n'
        f'MIME-Version: 1.0\n'
        f'Content-Type: text/html; charset="utf-8"\n\n'
        f'{html}'
    )
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
    return ret.returncode == 0


if __name__ == '__main__':
    html, subj = build_html()
    ok = send_email(html, subj)
    print(f'{"✅" if ok else "❌"} 合并报告{"已" if ok else "未"}发送 ({subj})')
