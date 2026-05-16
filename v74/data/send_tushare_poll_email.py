#!/usr/bin/env python3
"""
Tushare 数据轮询到位通知邮件
在数据到位并下载完成后发送通知
"""
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
SMTP_USER = "18313835@qq.com"
SMTP_PASS = "ngrzdzjuhwfnbgbh"
TO_EMAIL = "18313835@qq.com"


def send_email(ready_time, stock_count, status="成功"):
    subject = f"【Tushare】{datetime.now().strftime('%Y-%m-%d')} 数据到位 {ready_time}"

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif; 
         max-width: 600px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
  .card {{ background: #fff; border-radius: 12px; padding: 24px; 
           box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  .header {{ text-align: center; margin-bottom: 20px; }}
  .header h1 {{ font-size: 18px; font-weight: 700; color: #1a1a1a; margin: 0 0 4px 0; }}
  .header .time {{ font-size: 13px; color: #888; }}
  .status {{ text-align: center; padding: 16px; border-radius: 8px; 
             font-size: 16px; font-weight: 600; margin-bottom: 20px; }}
  .status.ok {{ background: #e8f5e9; color: #2e7d32; border: 1px solid #a5d6a7; }}
  .metric-grid {{ display: flex; gap: 12px; margin-bottom: 16px; }}
  .metric {{ flex: 1; background: #f8f9fa; border-radius: 8px; padding: 16px; 
             text-align: center; }}
  .metric .val {{ font-size: 24px; font-weight: 700; color: #333; }}
  .metric .label {{ font-size: 12px; color: #888; margin-top: 4px; }}
  .footer {{ text-align: center; color: #bbb; font-size: 12px; 
             margin-top: 20px; padding-top: 16px; border-top: 1px solid #eee; }}
</style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h1>📡 Tushare 数据到位通知</h1>
      <div class="time">{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
    </div>
    
    <div class="status ok">✅ 数据已到位并下载完成</div>
    
    <div class="metric-grid">
      <div class="metric">
        <div class="val">{ready_time}</div>
        <div class="label">到位时间</div>
      </div>
      <div class="metric">
        <div class="val">{stock_count}</div>
        <div class="label">股票数量</div>
      </div>
    </div>
    
    <div style="background: #f8f9fa; border-radius: 8px; padding: 12px 16px; 
                font-size: 13px; color: #666;">
      📊 Tushare数据已成功下载到本地数据库，可开始策略模拟交易。
    </div>
  </div>
  
  <div class="footer">
    由 tushare_poll 自动发送 · {datetime.now().strftime('%Y-%m-%d %H:%M')}
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = TO_EMAIL
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, [TO_EMAIL], msg.as_string())
        print(f"[邮件] 发送成功 -> {TO_EMAIL}")
        return 0
    except Exception as e:
        print(f"[邮件] 发送失败: {e}")
        return 1


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: send_tushare_poll_email.py <ready_time> <stock_count>")
        sys.exit(1)

    ready_time = sys.argv[1]
    stock_count = sys.argv[2]
    status = sys.argv[3] if len(sys.argv) > 3 else "成功"

    sys.exit(send_email(ready_time, stock_count, status))
