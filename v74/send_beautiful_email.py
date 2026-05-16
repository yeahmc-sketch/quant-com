#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开盘检查美化版邮件发送脚本
调用 morning_check.py 获取实时数据，生成美化HTML邮件
"""
import smtplib
import subprocess
import sys
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date

# ── ANSI清理 ────────────────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)

SCRIPT = "/Users/chenshi/WorkBuddy/Claw/v74/morning_check.py"

def get_password():
    import os
    pw = os.environ.get("QQ_EMAIL_PASSWORD", "")
    if not pw:
        cfg = "/Users/chenshi/.workbuddy/email_password.txt"
        try:
            with open(cfg) as f:
                return f.read().strip()
        except:
            pass
    return pw

def run_check():
    """运行 morning_check.py 获取实时数据"""
    result = subprocess.run(
        ["python3", SCRIPT],
        capture_output=True,
        text=True,
        timeout=180,
        cwd="/Users/chenshi/WorkBuddy/Claw/v74"
    )
    return strip_ansi(result.stdout + result.stderr), result.returncode

def parse_output(raw: str) -> dict:
    """从 morning_check.py 输出中提取关键数据"""
    lines = raw.split("\n")
    data = {
        "leaders": [],
        "hot_sectors": [],
        "industry_sectors": [],
        "index_data": []
    }
    current_section = None

    for line in lines:
        line = line.strip()
        
        # 综合评分
        m = re.search(r"综合评分[:：]\s*(?:1m)?(\d+)分", line)
        if m:
            data["score"] = m.group(1)
            continue

        # 涨跌家数
        m = re.search(r"涨跌家数[:：].*?(\d+)\s+vs\s+(\d+)", line)
        if m:
            data["up"] = int(m.group(1))
            data["down"] = int(m.group(2))
            continue
        if "偏暖" in line or "偏多" in line:
            data["breadth"] = "偏暖"
        elif "偏弱" in line:
            data["breadth"] = "偏弱"
        elif "分化" in line:
            data["breadth"] = "分化"
            # 提取判断结果
            if "→" in line:
                data["breadth_text"] = line.split("→")[-1].strip()

        # 昨日涨停溢价
        m = re.search(r"均幅\s+([+-]?\d+\.\d+)%", line)
        if m:
            data["premium"] = float(m.group(1))

        # 涨跌停数量
        m = re.search(r"涨停数量[:：]\s*(\d+)家", line)
        if m:
            data["zt"] = int(m.group(1))
        m = re.search(r"跌停数量[:：]\s*(\d+)家", line)
        if m:
            data["dt"] = int(m.group(1))
            data["risk"] = line.split("→")[-1].strip() if "→" in line else line
        # 从加分/减分项中提取涨停数量
        if "涨停" in line and "充足" in line or "极多" in line or "偏少" in line:
            m = re.search(r"涨停(?:极多|充足|偏少)?\((\d+)家\)", line)
            if m and "zt" not in data:
                data["zt"] = int(m.group(1))
        # 从加分/减分项中提取跌停数量
        if "跌停过多" in line or "跌停" in line:
            m = re.search(r"跌停过多[⁇?]?\((\d+)家\)", line)
            if m:
                data["dt"] = int(m.group(1))

        # 最高连板
        m = re.search(r"市场最高连板[:：].*?(\d+)板", line)
        if m:
            data["max_lb"] = int(m.group(1))
            # 提取情绪判断
            if "→" in line:
                data["max_lb_text"] = line.split("→")[-1].strip()

        # 梯队完整性
        if "梯队完整" in line:
            data["ladder"] = "完整"
            if "→" in line:
                data["ladder_text"] = line.split("→")[-1].strip()
        elif "梯队断层" in line:
            data["ladder"] = "断层"
            if "→" in line:
                data["ladder_text"] = line.split("→")[-1].strip()

        # 梯队分布 - 格式：首板→1进2：7只   2进3：1只   3进4：0只   4进5+：0只
        m = re.search(r"1进2[：:]?\s*(\d+)只.*?2进3[：:]?\s*(\d+)只.*?3进4[：:]?\s*(\d+)只.*?4进5[+]?\s*[：:]?\s*(\d+)只", line)
        if m:
            data["ladder_dist"] = f"1进2：{m.group(1)}只 | 2进3：{m.group(2)}只 | 3进4：{m.group(3)}只 | 4进5+：{m.group(4)}只"

        # 解析机器可读格式连板龙头：LB:000001|平安银行|3板|+10.0%
        if "【连板龙头】" in line or "代码" in line and "名称" in line:
            current_section = "leaders"
            continue
        if current_section == "leaders":
            m_lb = re.match(r"^LB:(\d{6})\|(.+?)\|(\d+)板\|([+-]?\d+\.?\d*)%$", line)
            if m_lb:
                data["leaders"].append({
                    "code": m_lb.group(1),
                    "name": m_lb.group(2),
                    "lb": m_lb.group(3),
                    "pct": m_lb.group(4)
                })
            elif re.match(r"^\d{6}", line):  # 普通格式：603318 水发燃气 5板 +10.0%
                parts = line.split()
                if len(parts) >= 4:
                    data["leaders"].append({
                        "code": parts[0],
                        "name": parts[1],
                        "lb": parts[2],
                        "pct": parts[3].replace("%", "").replace("+", "")
                    })

        # 解析指数量比
        m = re.search(r"(上证指数|深证成指|创业板指|沪深300|上证50)[：:]\s*([+-]?\d+\.?\d*)%.*?量比\s*([\d.]+).*?→(.*)", line)
        if m:
            data["index_data"].append({
                "name": m.group(1),
                "pct": m.group(2),
                "vol": m.group(3),
                "judge": m.group(4).strip()
            })

        # 竞价综合判断
        if "竞价综合判断" in line:
            vol_match = re.search(r"平均?量比\s*([\d.]+)", line)
            if vol_match:
                data["avg_vol"] = vol_match.group(1)
            if "→" in line:
                vol_text = line.split("→")[-1].strip()
                vol_text = re.sub(r"\(量比[\d.]+\)", "", vol_text).strip()
                data["vol_judge"] = vol_text.lstrip("：:：").strip()
            elif "量比" in line:
                vol_text = re.sub(r"\(量比[\d.]+\)", "", line).split("综合判断")[-1].strip()
                data["vol_judge"] = vol_text.lstrip("：:：").strip()

        # 概念板块TOP - 不要strip，保留前导空格
        if "概念板块TOP" in line:
            current_section = "concept"
            continue
        if current_section == "concept":
            if line.startswith("---"):
                continue
            if line.startswith("主线") or "风格判定" in line or line.startswith("行业板块"):
                current_section = None
                if line.startswith("行业板块"):
                    current_section = "industry"
                continue
            m = re.match(r"^(.+?)\s{2,}([+-]?\s*\d+\.?\d*)%", line)
            if not m:
                m = re.match(r"^(.+?)\s+([+-]?\s*\d+\.?\d*)%\s*$", line)
            if m and len(data["hot_sectors"]) < 3:
                name = m.group(1).strip()
                pct = m.group(2).replace(" ", "").replace("+", "")
                data["hot_sectors"].append({"name": name, "pct": pct})

        # 行业板块TOP - 不要strip，保留前导空格
        if "行业板块TOP" in line:
            current_section = "industry"
            continue
        if current_section == "industry":
            if line.startswith("---"):
                continue
            if "风格判定" in line or line.startswith("主线") or not line:
                current_section = None
                continue
            m = re.match(r"^(.+?)\s{2,}([+-]?\s*\d+\.?\d*)%", line)
            if not m:
                m = re.match(r"^(.+?)\s+([+-]?\s*\d+\.?\d*)%\s*$", line)
            if m and len(data["industry_sectors"]) < 3:
                name = m.group(1).strip()
                pct = m.group(2).replace(" ", "").replace("+", "")
                data["industry_sectors"].append({"name": name, "pct": pct})

        # 主线判断
        m = re.search(r"主线判断[:：]\s*(.+)", line)
        if m:
            data["theme"] = m.group(1).strip()

        # 风格判定
        m = re.search(r"风格判定[:：]\s*(.+)", line)
        if m:
            data["style"] = m.group(1).strip()

        # 加分/减分
        m = re.search(r"加分/减分项[:：]\s*(.+)", line)
        if m:
            data["reasons"] = m.group(1).strip()

        # 今日主线
        m = re.search(r"今日主线[:：]\s*(\[91m)?(.*?)(?:\[0m|$)", line)
        if m:
            data["main"] = m.group(2).strip()

        # 辅线/轮动
        if "辅线" in line:
            data["sub"] = line.split("辅线：")[-1].split("|")[0].strip()
        if "轮动" in line:
            data["rotation"] = line.split("轮动：")[-1].strip()

        # 涨停集中
        if "涨停集中" in line:
            data["zt_focus"] = line.split("涨停集中：")[-1].strip()

        # 风险提示（保留）
        if "风险提示" in line and "风险提示：" in line:
            if "risk2" not in data:
                data["risk2"] = line.split("风险提示：")[-1].strip()
            else:
                data["risk3"] = line.split("风险提示：")[-1].strip()

        # 三档结论
        if "正常出手" in line:
            data["action"] = "normal"
            data["action_text"] = "正常出手"
            data["action_sub"] = "仓位六成以上"
        elif "谨慎出手" in line:
            data["action"] = "caution"
            data["action_text"] = "谨慎出手"
            data["action_sub"] = "仓位三成，低吸为主"
        elif "空仓" in line:
            data["action"] = "empty"
            data["action_text"] = "直接空仓"
            data["action_sub"] = "严禁接力"

        # 操作条件
        if "条件：" in line:
            data["condition"] = line.split("条件：")[-1].strip()

        # 推荐
        if "推荐：" in line:
            data["recommend"] = line.split("推荐：")[-1].strip()

    # 处理涨跌家数显示
    if "up" in data and "down" in data:
        data["breadth_str"] = f"🔴{data['up']} vs 🟢{data['down']}"
    else:
        data["breadth_str"] = data.get("breadth", "-")

    return data

def build_html(data: dict, today: str, timestamp: str) -> str:
    """构建美化HTML邮件"""

    # 颜色配置
    colors = {
        "normal": ("#2e7d32", "#e8f5e9"),
        "caution": ("#f57c00", "#fff8e1"),
        "empty": ("#c62828", "#ffebee")
    }
    action = data.get("action", "caution")
    action_color, action_bg = colors.get(action, colors["caution"])

    # 基础指标
    zt = data.get("zt", "-")
    dt = data.get("dt", "-")
    max_lb = data.get("max_lb", "-")
    score = data.get("score", "-")
    premium = data.get("premium", 0)
    prem_str = f"{'+' if premium >= 0 else ''}{premium:.2f}%"

    # 昨日涨停
    up_count = data.get("up", 0)
    down_count = data.get("down", 0)
    breadth_text = data.get("breadth_text", "")

    # 量比数据
    index_html = ""
    for idx in data.get("index_data", []):
        pct = idx["pct"]
        is_up = not pct.startswith("-")
        pct_class = "green" if is_up else "red"
        index_html += f'''
        <div class="info-row">
          <span class="info-label">{idx["name"]}</span>
          <span class="info-value {pct_class}">{pct}%</span>
          <span class="vol-indicator">{idx["vol"]}</span>
          <span class="info-value" style="width:auto;margin-left:8px;">{idx["judge"]}</span>
        </div>'''

    # 连板龙头卡片
    leaders_html = ""
    for i, leader in enumerate(data.get("leaders", [])[:6]):
        is_high = leader.get("lb") == "5" or (isinstance(leader.get("lb"), int) and leader.get("lb", 0) >= 4)
        high_class = "high" if is_high else ""
        leaders_html += f'''
        <div class="leader-card {high_class}">
          <div class="leader-code">{leader.get("code", "")}</div>
          <div class="leader-name">{leader.get("name", "")}</div>
          <div class="leader-lb">{leader.get("lb", "")}板</div>
          <div class="leader-pct red">{leader.get("pct", "")}%</div>
        </div>'''

    # 概念板块标签
    hot_tags_html = ""
    for i, s in enumerate(data.get("hot_sectors", [])[:3]):
        tag_class = "main" if i == 0 else "up"
        prefix = "🚀" if i == 0 else ""
        hot_tags_html += f'<span class="sector-tag {tag_class}">{prefix} {s["name"]} {s["pct"]}%</span>'

    # 行业板块标签
    ind_tags_html = ""
    for i, s in enumerate(data.get("industry_sectors", [])[:3]):
        tag_class = "main" if i == 0 else "up"
        prefix = "🚀" if i == 0 else ""
        ind_tags_html += f'<span class="sector-tag {tag_class}">{prefix} {s["name"]} {s["pct"]}%</span>'

    # 风险提示
    risk1 = data.get("risk", "")
    risk2 = data.get("risk2", "")
    risk_html = ""
    if risk1 and "跌停" in risk1:
        risk_html += f'<div class="risk-box red">⚠️ {risk1}</div>'
    if risk2:
        risk_html += f'<div class="risk-box red" style="margin-top:8px;">⚠️ {risk2}</div>'

    # 推荐操作
    recommend = data.get("recommend", "")
    condition = data.get("condition", "")
    recommend_html = ""
    if recommend:
        recommend_html = f'<div class="recommend-box" style="margin-top:8px;"><strong>操作建议：</strong><br>{recommend}</div>'

    # 主线
    main = data.get("main", "-")
    sub = data.get("sub", "-")
    rotation = data.get("rotation", "-")
    if "今日主线" not in str(main) and main != "-":
        main_html = f'''<div class="recommend-box">
          <strong>今日主线：</strong>{main}<br>
          <strong>辅线：</strong>{sub} | <strong>轮动：</strong>{rotation}
        </div>'''
    else:
        main_html = ""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', sans-serif; max-width: 680px; margin: 0 auto; padding: 20px; font-size: 15px; line-height: 1.6; color: #333; background: #f8f9fa; }}
  .card {{ background: #fff; border-radius: 12px; padding: 24px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  .header {{ text-align: center; padding-bottom: 16px; border-bottom: 1px solid #eee; margin-bottom: 20px; }}
  .header h1 {{ font-size: 20px; font-weight: 700; color: #1a1a1a; margin: 0 0 4px 0; }}
  .header .date {{ font-size: 13px; color: #888; }}
  
  .action-banner {{ 
    text-align: center; 
    padding: 16px; 
    border-radius: 8px; 
    font-size: 18px; 
    font-weight: 700; 
    margin-bottom: 20px;
    background: {action_bg}; 
    color: {action_color}; 
    border: 1px solid {action_color};
  }}
  .action-banner .sub {{ font-size: 13px; font-weight: 400; margin-top: 6px; opacity: 0.8; }}
  
  .metrics-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }}
  .metric-box {{ background: #f8f9fa; border-radius: 8px; padding: 14px 12px; text-align: center; }}
  .metric-box .val {{ font-size: 24px; font-weight: 700; }}
  .metric-box .val.red {{ color: #d4380d; }}
  .metric-box .val.green {{ color: #52c41a; }}
  .metric-box .val.orange {{ color: #fa8c16; }}
  .metric-box .label {{ font-size: 12px; color: #888; margin-top: 4px; }}
  
  .section-title {{ font-size: 14px; font-weight: 700; color: #1a1a1a; margin: 0 0 12px 0; padding-bottom: 8px; border-bottom: 2px solid #d4380d; display: inline-block; }}
  
  .info-table {{ width: 100%; }}
  .info-row {{ display: flex; padding: 8px 0; border-bottom: 1px solid #f0f0f0; align-items: center; }}
  .info-row:last-child {{ border-bottom: none; }}
  .info-label {{ color: #666; width: 90px; flex-shrink: 0; font-size: 14px; }}
  .info-value {{ color: #1a1a1a; font-weight: 600; font-size: 14px; flex: 1; }}
  .info-value.red {{ color: #d4380d; }}
  .info-value.green {{ color: #52c41a; }}
  .info-value.orange {{ color: #fa8c16; }}
  
  .leaders-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }}
  .leader-card {{ background: #fff8f8; border-radius: 8px; padding: 12px; text-align: center; border: 1px solid #ffebee; }}
  .leader-card.high {{ background: #fff3e0; border-color: #ffe0b2; }}
  .leader-name {{ font-size: 14px; font-weight: 700; color: #333; margin-bottom: 4px; }}
  .leader-lb {{ font-size: 16px; font-weight: 700; color: #d4380d; }}
  .leader-pct {{ font-size: 12px; color: #888; }}
  .leader-code {{ font-size: 11px; color: #aaa; }}
  
  .sector-tags {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }}
  .sector-tag {{ display: inline-block; padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: 600; }}
  .sector-tag.up {{ background: #fff2e8; color: #d4380d; }}
  .sector-tag.main {{ background: #fff2e8; color: #d4380d; border: 2px solid #ff7a45; }}
  
  .vol-indicator {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; background: #fff2e8; color: #d4380d; }}
  
  .risk-box {{ background: #fff3cd; border-left: 4px solid #ffc107; padding: 12px 16px; border-radius: 4px; color: #7a4a00; font-size: 14px; }}
  .risk-box.red {{ background: #ffebee; border-left-color: #ef5350; color: #c62828; }}
  
  .recommend-box {{ background: #f0f5ff; border-left: 4px solid #1890ff; padding: 12px 16px; border-radius: 4px; color: #1a1a1a; font-size: 14px; }}
  
  .footer {{ text-align: center; color: #bbb; font-size: 12px; margin-top: 20px; padding-top: 16px; border-top: 1px solid #eee; }}
  
  @media screen and (max-width: 480px) {{
    body {{ padding: 12px; font-size: 14px; }}
    .metrics-grid {{ grid-template-columns: repeat(2, 1fr); gap: 8px; }}
    .metric-box {{ padding: 10px 8px; }}
    .metric-box .val {{ font-size: 20px; }}
    .leaders-grid {{ grid-template-columns: repeat(2, 1fr); gap: 8px; }}
    .info-label {{ width: 80px; }}
  }}
</style>
</head>
<body>

<div class="card">
  <div class="header">
    <h1>📊 开盘5分钟检查</h1>
    <div class="date">{today} {timestamp}</div>
  </div>
  
  <div class="action-banner">
    {data.get("action_text", "⚠️ 谨慎出手")}
    <div class="sub">{data.get("action_sub", "仓位三成，低吸为主")}</div>
  </div>
  
  <div class="section-title">【核心指标】</div>
  <div class="metrics-grid">
    <div class="metric-box">
      <div class="val red">{max_lb}板</div>
      <div class="label">最高连板</div>
    </div>
    <div class="metric-box">
      <div class="val red">{zt}</div>
      <div class="label">涨停（家）</div>
    </div>
    <div class="metric-box">
      <div class="val orange">{dt}</div>
      <div class="label">跌停（家）</div>
    </div>
    <div class="metric-box">
      <div class="val orange">{prem_str}</div>
      <div class="label">昨日涨停溢价</div>
    </div>
  </div>
</div>

<div class="card">
  <div class="section-title">【第一步：全局情绪】</div>
  <div class="info-table">
    <div class="info-row">
      <span class="info-label">涨跌家数</span>
      <span class="info-value">{data.get("breadth_str", data.get("breadth", "-"))}</span>
    </div>
    <div class="info-row">
      <span class="info-label">昨日涨停</span>
      <span class="info-value">红{up_count} vs 绿{down_count}，均幅 <span class="orange">{prem_str}</span></span>
    </div>
    <div class="info-row">
      <span class="info-label">今日开盘</span>
      <span class="info-value orange">{breadth_text}</span>
    </div>
  </div>
  {risk_html}
</div>

<div class="card">
  <div class="section-title">【第二步：量能分析】</div>
  {index_html}
  <div class="recommend-box" style="margin-top: 12px;">
    <strong>竞价综合判断：</strong>{data.get("vol_judge", "-")} <span class="vol-indicator">平均量比 {data.get("avg_vol", "-")}</span>
  </div>
</div>

<div class="card">
  <div class="section-title">【第三步：连板梯队】</div>
  <div class="info-table">
    <div class="info-row">
      <span class="info-label">梯队完整性</span>
      <span class="info-value orange">{data.get("ladder_text", data.get("ladder", "-"))}</span>
    </div>
    <div class="info-row">
      <span class="info-label">梯队分布</span>
      <span class="info-value">{data.get("ladder_dist", "-")}</span>
    </div>
  </div>
</div>

{('<div class="card"><div class="section-title">【高标龙头 TOP6】</div><div class="leaders-grid">{}</div></div>'.format(leaders_html)) if leaders_html else ''}

<div class="card">
  {('<div class="section-title">【概念板块 TOP3】</div><div class="sector-tags">{}</div>'.format(hot_tags_html)) if hot_tags_html else ''}
  {('<div class="section-title" style="margin-top: 20px;">【行业板块 TOP3】</div><div class="sector-tags">{}</div>'.format(ind_tags_html)) if ind_tags_html else ''}
  
  <div class="info-table" style="margin-top: 16px;">
    <div class="info-row">
      <span class="info-label">主线判断</span>
      <span class="info-value">{data.get("theme", "-")}</span>
    </div>
    <div class="info-row">
      <span class="info-label">市场风格</span>
      <span class="info-value red">{data.get("style", "-")}</span>
    </div>
    <div class="info-row">
      <span class="info-label">涨停集中</span>
      <span class="info-value">{data.get("zt_focus", "-")}</span>
    </div>
  </div>
</div>

<div class="card">
  <div class="section-title">【第五步：最终决策】</div>
  <div class="info-table">
    <div class="info-row">
      <span class="info-label">综合评分</span>
      <span class="info-value orange">{score}分</span>
    </div>
    <div class="info-row">
      <span class="info-label">加减分项</span>
      <span class="info-value">{data.get("reasons", "-")}</span>
    </div>
  </div>
  
  {main_html}
  {recommend_html}
</div>

<div class="footer">
  由 morning_check.py 自动生成 · 仅供参考，不构成投资建议
</div>

</body>
</html>"""
    return html

def send_email(html_content: str, subject: str):
    socket = __import__("socket")
    socket.setdefaulttimeout(30)
    pw = get_password()
    if not pw:
        raise Exception("无法获取邮件密码")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = "18313835@qq.com"
    msg["To"] = "18313835@qq.com"
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.qq.com", 465) as server:
        server.login("18313835@qq.com", pw)
        server.sendmail("18313835@qq.com", ["18313835@qq.com"], msg.as_string())

def main():
    today = date.today().strftime("%Y-%m-%d")
    timestamp = __import__("datetime").datetime.now().strftime("%H:%M:%S")
    
    print(f"📅 {today} 开盘检查开始...")
    print(f"⏳ 调用 morning_check.py 获取实时数据...")

    raw_output, exit_code = run_check()
    print(f"✅ 数据采集完成，exit={exit_code}")

    # 解析数据
    data = parse_output(raw_output)

    # 构建HTML邮件
    html_email = build_html(data, today, timestamp)

    # 发送邮件
    action_map = {0: "正常出手", 1: "谨慎出手", 2: "空仓"}
    action = action_map.get(exit_code, f"?({exit_code})")
    subject = f"【开盘检查 {today}】{action}"

    print(f"📧 发送邮件: {subject}")
    try:
        send_email(html_email, subject)
        print("✅ 邮件发送成功！")
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0  # morning_check的exit_code是决策状态码，不代表失败

if __name__ == "__main__":
    sys.exit(main())
