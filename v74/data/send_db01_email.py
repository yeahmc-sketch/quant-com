#!/usr/bin/env python3
"""DB01 数据拉取状态报告 — 汇报每一步的完成情况"""
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
SMTP_USER = "18313835@qq.com"
SMTP_PASS = "ngrzdzjuhwfnbgbh"
TO_EMAIL = "18313835@qq.com"

LOG_PATH = "/Users/chenshi/WorkBuddy/Claw/v74/data/logs/db01_{date}.log".format(
    date=datetime.now().strftime("%Y%m%d")
)

STEP_NAMES = {
    "1":   "腾讯行情 (fast_tencent_daily)",
    "1.1": "Tushare日线 (前复权)",
    "1.2": "Tushare到位时间",
    "1.5": "指数下载",
    "1.6": "每日基本面 (PE/PB/市值)",
    "1.8": "概念板块热度",
    "2":   "板块资金流向",
    "3":   "个股资金流向",
}

def parse_log():
    """解析DB01日志，提取每步状态"""
    try:
        with open(LOG_PATH) as f:
            text = f.read()
    except:
        return None, "日志文件不存在"

    lines = text.split("\n")
    steps = []
    start_time = None

    for line in lines:
        # 开始时间
        m = re.search(r"DB01 (\S+) (\S+)", line)
        if m and not start_time:
            start_time = f"{m.group(1)} {m.group(2)}"

        # 每步完成: [15:01:10] Step 1.1 完成 (exit=0)
        m = re.search(r"\[(\S+)\] Step ([\d.]+) 完成 \(exit=(\d+)\)", line)
        if m:
            time_str, step, exit_code = m.group(1), m.group(2), int(m.group(3))
            name = STEP_NAMES.get(step, f"Step {step}")
            status = "✅" if exit_code == 0 else "❌"
            steps.append((time_str, step, name, status, exit_code))

        # 股票数信息（从各个步骤的print中提取）
        m = re.search(r"(?:完成|写入|成功|处理).*?(\d+) 行", line)
        if m:
            pass  # 下面单独提取

    return start_time, steps


def get_record_counts():
    """从日志中解析每个步骤的数据量"""
    try:
        with open(LOG_PATH) as f:
            text = f.read()
    except:
        return {}

    counts = {}

    # Step 1: "处理: 5500 行" → 腾讯行情
    tencent_count = ""
    m = re.search(r"处理:\s*(\d+)\s*行", text)
    if m:
        tencent_count = f"{m.group(1)} 只"
        counts["腾讯行情"] = tencent_count

    # Step 1.1: Tushare（数据未到位时显示腾讯数据量）
    m = re.search(r"共处理\s*(\d+)\s*行", text)
    if m:
        tushare_count = int(m.group(1))
        if tushare_count == 0 and tencent_count:
            counts["Tushare日线"] = f"待覆盖 (腾讯{tencent_count}已就绪)"
        elif tushare_count > 0:
            counts["Tushare日线"] = f"{tushare_count} 行"
        else:
            counts["Tushare日线"] = "待更新"

    # Step 1.6: "写入: X 行"
    m = re.search(r"写入:\s*(\d+)\s*行", text)
    if m: counts["基本面"] = f"{m.group(1)} 只"

    # Step 1.8: "SQLite已写入: ... (X条)"
    m = re.search(r"SQLite已写入.*?\((\d+)条\)", text)
    if m: counts["概念板块"] = f"{m.group(1)} 条"

    # Step 2: "总计写入: X 条"
    m = re.search(r"总计.*?(\d+)\s*条", text)
    if m: counts["板块资金流向"] = f"{m.group(1)} 条"

    # Step 1.5 指数
    m = re.search(r"成功.*?(\d+)\s*条", text)
    if m: counts["指数"] = f"{m.group(1)} 条"

    return counts


def build_html(start_time, steps, counts, exit_code):
    """生成干净的HTML状态报告"""

    rows = ""
    for time_str, step, name, status, ec in steps:
        rows += f"""<tr>
    <td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px">{time_str}</td>
    <td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px">Step {step}</td>
    <td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px">{name}</td>
    <td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px;text-align:center">{status}</td>
    <td style="padding:6px 8px;border-bottom:1px solid #eee;font-size:12px">{counts.get(name.split('(')[0].strip(), '')}</td>
</tr>"""

    if not rows:
        rows = '<tr><td colspan="5" style="padding:12px;text-align:center;color:#999;font-size:12px">无日志记录</td></tr>'

    flow_info = ""
    if "板块资金流向" in counts:
        flow_info = f'<div style="font-size:12px;color:#666;margin-top:4px">其中概念{counts.get("板块资金流向","")}</div>'

    status_badge = "✅ 全部成功" if exit_code == 0 else f"⚠️ 有警告 (exit={exit_code})"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, 'PingFang SC', sans-serif; max-width: 600px; margin:20px auto; padding:0 16px; background:#f5f5f5; }}
  .card {{ background:#fff; border-radius:10px; padding:20px; margin-bottom:16px; box-shadow:0 1px 4px rgba(0,0,0,0.08); }}
  .title {{ font-size:16px; font-weight:700; color:#1a1a1a; margin:0 0 4px 0; }}
  .time {{ font-size:12px; color:#888; margin-bottom:16px; }}
  table {{ width:100%; border-collapse:collapse; }}
  th {{ background:#f8f9fa; padding:6px 8px; text-align:left; font-size:11px; color:#666; font-weight:600; border-bottom:2px solid #eee; }}
  td {{ font-size:12px; }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:4px; font-size:12px; font-weight:600; }}
  .badge.ok {{ background:#e8f5e9; color:#2e7d32; }}
  .badge.warn {{ background:#fff3e0; color:#e65100; }}
  .summary {{ display:flex; gap:12px; margin-top:16px; flex-wrap:wrap; }}
  .summary-item {{ background:#f8f9fa; border-radius:6px; padding:10px 14px; flex:1; min-width:120px; text-align:center; }}
  .summary-val {{ font-size:18px; font-weight:700; color:#333; }}
  .summary-label {{ font-size:11px; color:#888; margin-top:2px; }}
</style>
</head><body>

<div class="card">
  <div class="title">📡 DB01 数据拉取报告</div>
  <div class="time">{start_time or "时间未知"} | <span class="badge {'ok' if exit_code==0 else 'warn'}">{status_badge}</span></div>

  <table>
    <thead><tr>
      <th>时间</th><th>步骤</th><th>数据</th><th style="text-align:center">状态</th><th>数量</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>

<div class="card">
  <div class="title" style="font-size:14px;margin-bottom:12px">📊 数据汇总</div>
  <div class="summary">
"""

    for label, val in counts.items():
        html += f"""
    <div class="summary-item">
      <div class="summary-val">{val}</div>
      <div class="summary-label">{label}</div>
    </div>"""

    html += f"""
  </div>
</div>

<div style="text-align:center;font-size:11px;color:#bbb;margin-top:16px">
  DB01 Pipeline · {datetime.now().strftime('%Y-%m-%d %H:%M')}
</div>

</body></html>"""
    return html


def send_email(subject, html_body):
    msg = MIMEMultipart()
    msg['From'] = SMTP_USER
    msg['To'] = TO_EMAIL
    msg['Subject'] = subject
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, TO_EMAIL, msg.as_string())
        print(f"[邮件] 发送成功 -> {TO_EMAIL}")
    except Exception as e:
        print(f"[邮件] 发送失败: {e}")


if __name__ == "__main__":
    import sys
    exit_code = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    start_time, steps = parse_log()
    counts = get_record_counts()

    status = "✅ 成功" if exit_code == 0 else f"⚠️ 有警告(exit={exit_code})"
    subject = f"【DB01】{datetime.now().strftime('%Y-%m-%d')} 数据拉取 {status}"

    html = build_html(start_time, steps, counts, exit_code)
    send_email(subject, html)
