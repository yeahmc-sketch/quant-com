#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开盘检查报告邮件发送脚本
由 automation 任务 V5 调用（周一至周五 9:32）
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
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
FROM_ADDR = "18313835@qq.com"
TO_ADDR   = "18313835@qq.com"

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
    result = subprocess.run(
        ["python3", SCRIPT],
        capture_output=True,
        text=True,
        timeout=180,
        cwd="/Users/chenshi/WorkBuddy/Claw/v74"
    )
    return result.stdout + result.stderr, result.returncode

def parse_output(raw: str) -> dict:
    """从脚本原始输出中提取关键数据（ANSI已清理）"""
    lines = raw.split("\n")
    data = {}

    # ── 优先解析结构化 METRICS 行（无ANSI/emoji，最可靠）─────────────────────
    for line in lines:
        if line.startswith("METRICS:"):
            parts = line[8:].strip().split()
            for p in parts:
                if "=" in p:
                    k, v = p.split("=", 1)
                    data[k] = v
            # 拆分 up=123/456 → up=123, down=456
            if "up" in data and "/" in data["up"]:
                up_parts = data["up"].split("/")
                data["up"] = up_parts[0]
                data["down"] = up_parts[1]
            # 类型转换（build_email 需要数值类型）
            for _k in ("zt", "dt", "maxlb", "score"):
                if _k in data:
                    try: data[_k] = int(data[_k])
                    except: pass
            for _k in ("prem", "topsector_pct", "indsector_pct"):
                if _k in data:
                    try: data[_k] = float(data[_k])
                    except: pass
            # 别名映射（build_email 用不同名字）
            if "maxlb" in data:   data["max_lb"] = data.pop("maxlb")
            if "prem" in data:    data["premium"] = data.pop("prem")
            if "topsector" in data:
                top_val = data.pop("topsector")
                m_top = re.match(r"(.+)\(([+-]?\d+\.?\d*)\)", top_val)
                data["hot_sectors"] = [{
                    "name": m_top.group(1) if m_top else top_val,
                    "pct":  m_top.group(2) if m_top else data.pop("topsector_pct", "0")
                }]
            if "indsector" in data:
                ind_val = data.pop("indsector")
                m_ind = re.match(r"(.+)\(([+-]?\d+\.?\d*)\)", ind_val)
                data["industry_sectors"] = [{
                    "name": m_ind.group(1) if m_ind else ind_val,
                    "pct":  m_ind.group(2) if m_ind else data.pop("indsector_pct", "0")
                }]
            break

    # ── 兼容解析：补充正则兜底（METRICS 已覆盖则跳过）────────────────────────

    for line in lines:
        # 综合评分（如：综合评分：  1m5分）
        m = re.search(r"综合评分[:：]\s*(?:1m)?(\d+)分", line)
        if m:
            data["score"] = m.group(1)

        # 涨跌家数
        m = re.search(r"涨跌家数[:：].*?(\d+)\s+vs\s+(\d+)", line)
        if m:
            data["up"] = int(m.group(1))
            data["down"] = int(m.group(2))
        elif "偏暖" in line or "偏多" in line:
            data["breadth"] = "偏暖"
        elif "偏弱" in line:
            data["breadth"] = "偏弱"
        elif "分化" in line:
            data["breadth"] = "分化"

        # 昨日涨停溢价
        m = re.search(r"均幅\s+([+-]?\d+\.\d+)%", line)
        if m:
            data["premium"] = float(m.group(1))

        # 涨停数：可能在「跌停数量：X家」行，或「涨停(XX家)」在加分项里
        m = re.search(r"涨停数量[:：]\s*(\d+)家", line)
        if m:
            data["zt"] = int(m.group(1))
        m = re.search(r"跌停数量[:：]\s*(\d+)家", line)
        if m:
            data["dt"] = int(m.group(1))
        # 涨停数也可能在「加分/减分项」里（如"涨停充足(30家)"）
        if "zt" not in data:
            m = re.search(r"涨停(?:极多|充足|偏少)?\((\d+)家\)", line)
            if m:
                data["zt"] = int(m.group(1))

        # 最高连板
        m = re.search(r"市场最高连板[:：].*?(\d+)板", line)
        if m:
            data["max_lb"] = int(m.group(1))

        # 梯队完整性
        if "梯队完整" in line:
            data["ladder"] = "完整"
        elif "梯队断层" in line:
            data["ladder"] = "断层"

        # 解析机器可读格式的连板龙头：LB:000001|平安银行|3板|+10.0%
        if "【连板龙头】" in line:
            data["leaders"] = []
            continue
        if data.get("leaders") is not None and isinstance(data.get("leaders"), list):
            m_lb = re.match(r"^LB:(\d{6})\|(.+?)\|(\d+)板\|([+-]?\d+\.?\d*)%$", line.strip())
            if m_lb:
                data["leaders"].append({
                    "code": m_lb.group(1),
                    "name": m_lb.group(2),
                    "lb": m_lb.group(3),
                    "pct": m_lb.group(4)
                })

        # 解析概念板块TOP3（格式：船舶制造     +  5.9%）
        if re.search(r"概念板块TOP", line):
            data["_in_concept"] = True
            if not data.get("hot_sectors"):
                data["hot_sectors"] = []
        elif re.search(r"行业板块TOP|连板龙头|风格判定", line):
            data["_in_concept"] = False
        if data.get("_in_concept") and data.get("hot_sectors") is not None \
                and isinstance(data["hot_sectors"], list) and len(data["hot_sectors"]) < 3:
            m_concept = re.match(r"^\s*(.+?)\s+([+-]?\s*\d+\.?\d*)%", line.strip())
            if m_concept and not re.search(r"\d{6}\s", line):
                name = m_concept.group(1).strip()
                pct = m_concept.group(2).replace(" ", "").replace("+", "")
                # 去重：已有同名板块则跳过
                if name not in [s.get("name") for s in data["hot_sectors"]]:
                    data["hot_sectors"].append({"name": name, "pct": pct})

        # 解析行业板块TOP3
        if re.search(r"行业板块TOP", line):
            data["_in_ind"] = True
            if not data.get("industry_sectors"):
                data["industry_sectors"] = []
        elif re.search(r"风格判定|连板龙头|今日主线", line):
            data["_in_ind"] = False
        if data.get("_in_ind") and data.get("industry_sectors") is not None \
                and isinstance(data["industry_sectors"], list) and len(data["industry_sectors"]) < 3:
            m_ind = re.match(r"^\s*(.+?)\s+([+-]?\s*\d+\.?\d*)%", line.strip())
            if m_ind and not re.search(r"\d{6}\s", line):
                name = m_ind.group(1).strip()
                pct = m_ind.group(2).replace(" ", "").replace("+", "")
                if name not in [s.get("name") for s in data["industry_sectors"]]:
                    data["industry_sectors"].append({"name": name, "pct": pct})

        # 今日主线
        m = re.search(r"今日主线[:：]\s*(.+)", line)
        if m:
            data["main"] = m.group(1).strip()

        # 主线判断
        m = re.search(r"主线判断[:：]\s*(.+)", line)
        if m:
            data["theme"] = m.group(1).strip()

        # 风格判定
        m = re.search(r"风格判定[:：]\s*(.+)", line)
        if m:
            data["style"] = m.group(1).strip()

        # 竞价综合判断（取箭头后部分，清理量比残留）
        if "竞价综合判断" in line:
            if "→" in line:
                vol_part = line.split("→")[-1].strip()
            else:
                vol_part = line.split("竞价综合判断")[-1].strip()
            # 去掉所有ANSI颜色码和量比残留
            vol_clean = re.sub(r"\(量比[\d.]+\)", "", vol_part).strip()
            vol_clean = vol_clean.lstrip("：:：").strip()
            data["vol"] = vol_clean

        # 加分/减分
        m = re.search(r"加分/减分项[:：]\s*(.+)", line)
        if m:
            data["reasons"] = m.group(1).strip()

        # 风险提示
        if "风险提示" in line:
            data["risk"] = line.split("风险提示：")[-1].strip()

        # 三档结论
        if "正常出手" in line and ("仓位六成" in line or "✅" in line):
            data["action"] = "normal"
            data["action_text"] = "✅ 正常出手（仓位六成以上）"
        elif "谨慎出手" in line and "仓位三成" in line:
            data["action"] = "caution"
            data["action_text"] = "⚠️ 谨慎出手（仓位三成，低吸为主）"
        elif "空仓" in line and "严禁接力" in line:
            data["action"] = "empty"
            data["action_text"] = "❌ 直接空仓（严禁接力）"

    return data

def build_email(data: dict, today: str) -> str:
    """构建简洁HTML邮件"""

    # 状态颜色
    colors = {
        "normal":  "#1a7a1a",
        "caution": "#b36b00",
        "empty":   "#cc0000",
    }
    bg_colors = {
        "normal":  "#e6f4ea",
        "caution": "#fff3e0",
        "empty":   "#fde8e8",
    }

    action = data.get("action", "caution")
    color  = colors.get(action, "#333")
    bg     = bg_colors.get(action, "#f5f5f5")
    action_text = data.get("action_text", "⚠️ 谨慎出手")

    # 关键指标行
    zt = data.get("zt", "-")
    dt = data.get("dt", "-")
    max_lb = data.get("max_lb", "-")
    score = data.get("score", "-")
    vol = data.get("vol", "-")

    if "up" in data and "down" in data:
        breadth_str = f"🔴{data['up']} vs 🟢{data['down']}"
    else:
        breadth_str = data.get("breadth", "-")

    premium = data.get("premium")
    if premium is not None:
        prem_str = f"{'+' if premium >= 0 else ''}{premium:.2f}%"
    else:
        prem_str = "-"

    main_sec = data.get("main", "-")
    style = data.get("style", "-")
    theme = data.get("theme", "-")
    reasons = data.get("reasons", "-")
    risk = data.get("risk", "-")

    # 连板龙头
    leaders = data.get("leaders", [])
    leaders_html = ""
    if leaders:
        leaders_html = """<div class="section" style="margin-top: 16px;">
  <div class="section-title">&#128081; 高标龙头</div>
  <div class="leaders-grid">"""
        for leader in leaders[:6]:
            lb_pct = leader.get("pct", "0")
            is_up = not lb_pct.startswith("-")
            pct_color = "red" if is_up else "green"
            leaders_html += f"""<div class="leader-card">
    <div class="leader-name">{leader.get('name', '')}</div>
    <div class="leader-lb">{leader.get('lb', '')}板</div>
    <div class="leader-pct {pct_color}">{lb_pct}%</div>
  </div>"""
        leaders_html += "</div></div>"

    # 概念板块
    hot_sectors = data.get("hot_sectors", [])
    hot_html = ""
    if hot_sectors:
        hot_html = """<div class="sector-tags">"""
        for s in hot_sectors[:3]:
            pct = s.get("pct", "0")
            is_up = not pct.startswith("-")
            pct_color = "red" if is_up else "green"
            hot_html += f'<span class="sector-tag {pct_color}">{s.get("name","")} {pct}%</span>'
        hot_html += "</div>"

    # 行业板块
    industry_sectors = data.get("industry_sectors", [])
    ind_html = ""
    if industry_sectors:
        ind_html = """<div class="sector-tags">"""
        for s in industry_sectors[:3]:
            pct = s.get("pct", "0")
            is_up = not pct.startswith("-")
            pct_color = "red" if is_up else "green"
            ind_html += f'<span class="sector-tag {pct_color}">{s.get("name","")} {pct}%</span>'
        ind_html += "</div>"

    # 先计算条件片段（避免 f-string 内嵌反斜杠）
    risk_html = f'<div class="risk">&#9888; 风险提示：{risk}</div>' if risk and risk != "-" else ""
    # 构建HTML卡片内容
    leaders_cards = ""
    if leaders:
        cards_html = ""
        for l in leaders[:6]:
            cards_html += f'<div class="leader-card"><div class="leader-name">{l["name"]}</div><div class="leader-lb">{l["lb"]}板</div><div class="leader-pct">{l["pct"]}%</div></div>'
        leaders_cards = f'<div class="card"><div class="section-title">【高标龙头 TOP6】</div><div class="leaders-grid">{cards_html}</div></div>'
    
    hot_cards = ""
    if hot_sectors:
        tags_html = "".join([f'<span class="sector-tag up">{s["name"]} {s["pct"]}%</span>' for s in hot_sectors[:3]])
        hot_cards = f'<div class="card"><div class="section-title">【概念板块 TOP3】</div><div class="sector-tags">{tags_html}</div></div>'
    
    ind_cards = ""
    if industry_sectors:
        tags_html = "".join([f'<span class="sector-tag up">{s["name"]} {s["pct"]}%</span>' for s in industry_sectors[:3]])
        ind_cards = f'<div class="card"><div class="section-title">【行业板块 TOP3】</div><div class="sector-tags">{tags_html}</div></div>'
    
    risk_box = f'<div class="risk-box">&#9888; 风险提示: {risk}</div>' if risk and risk != "-" else ""
    
    # HTML美化版本
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
  .action {{ text-align: center; padding: 16px; border-radius: 8px; font-size: 18px; font-weight: 700; margin-bottom: 20px; }}
  .action.green {{ background: linear-gradient(135deg, #e8f5e9, #c8e6c9); color: #2e7d32; border: 1px solid #a5d6a7; }}
  .action.yellow {{ background: linear-gradient(135deg, #fff8e1, #ffecb3); color: #f57c00; border: 1px solid #ffcc80; }}
  .action.red {{ background: linear-gradient(135deg, #ffebee, #ffcdd2); color: #c62828; border: 1px solid #ef9a9a; }}
  .section-title {{ font-size: 14px; font-weight: 700; color: #1a1a1a; margin: 0 0 12px 0; padding-bottom: 8px; border-bottom: 2px solid {color}; display: inline-block; }}
  .metrics-grid {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; }}
  .metric-box {{ background: #f8f9fa; border-radius: 8px; padding: 12px 16px; min-width: 100px; text-align: center; flex: 1; }}
  .metric-box .val {{ font-size: 22px; font-weight: 700; }}
  .metric-box .val.up {{ color: #d4380d; }}
  .metric-box .val.down {{ color: #52c41a; }}
  .metric-box .val.neutral {{ color: #333; }}
  .metric-box .label {{ font-size: 12px; color: #888; margin-top: 2px; }}
  .info-table {{ width: 100%; }}
  .info-row {{ display: flex; padding: 8px 0; border-bottom: 1px solid #f0f0f0; }}
  .info-row:last-child {{ border-bottom: none; }}
  .info-label {{ color: #666; width: 90px; flex-shrink: 0; font-size: 14px; }}
  .info-value {{ color: #1a1a1a; font-weight: 600; font-size: 14px; word-break: break-word; }}
  .leaders-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }}
  .leader-card {{ background: #fff8f8; border-radius: 8px; padding: 12px; text-align: center; border: 1px solid #ffebee; }}
  .leader-name {{ font-size: 14px; font-weight: 700; color: #333; margin-bottom: 4px; }}
  .leader-lb {{ font-size: 16px; font-weight: 700; color: #d4380d; }}
  .leader-pct {{ font-size: 12px; color: #888; }}
  .sector-tags {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }}
  .sector-tag {{ display: inline-block; padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: 600; }}
  .sector-tag.up {{ background: #fff2e8; color: #d4380d; }}
  .sector-tag.down {{ background: #f6ffed; color: #52c41a; }}
  .risk-box {{ background: #fff3cd; border-left: 4px solid #ffc107; padding: 12px 16px; border-radius: 4px; color: #7a4a00; font-size: 14px; }}
  .footer {{ text-align: center; color: #bbb; font-size: 12px; margin-top: 20px; padding-top: 16px; border-top: 1px solid #eee; }}
  /* 手机适配 */
  @media screen and (max-width: 480px) {{
    body {{ padding: 12px; font-size: 14px; }}
    .card {{ padding: 16px; border-radius: 8px; }}
    .metrics-grid {{ gap: 8px; }}
    .metric-box {{ min-width: calc(50% - 4px); padding: 10px 8px; }}
    .metric-box .val {{ font-size: 18px; }}
    .leaders-grid {{ grid-template-columns: repeat(2, 1fr); gap: 8px; }}
    .info-label {{ width: 75px; font-size: 13px; }}
    .info-value {{ font-size: 13px; }}
  }}
</style>
</head>
<body>

<div class="card">
  <div class="header">
    <h1>📊 开盘5分钟检查</h1>
    <div class="date">{today}</div>
  </div>
  
  <div class="action {action}">{action_text}</div>
  
  <div class="section-title">【核心指标】</div>
  <div class="metrics-grid">
    <div class="metric-box">
      <div class="val up">{zt}</div>
      <div class="label">涨停（家）</div>
    </div>
    <div class="metric-box">
      <div class="val down">{dt}</div>
      <div class="label">跌停（家）</div>
    </div>
    <div class="metric-box">
      <div class="val up">{max_lb}板</div>
      <div class="label">最高连板</div>
    </div>
    <div class="metric-box">
      <div class="val neutral">{score}分</div>
      <div class="label">综合评分</div>
    </div>
    <div class="metric-box">
      <div class="val neutral">{prem_str}</div>
      <div class="label">昨日涨停溢价</div>
    </div>
  </div>
  
  <div class="section-title">【市场状态】</div>
  <div class="info-table">
    <div class="info-row">
      <span class="info-label">涨跌家数</span>
      <span class="info-value">{breadth_str}</span>
    </div>
    <div class="info-row">
      <span class="info-label">竞价量能</span>
      <span class="info-value">{vol}</span>
    </div>
    <div class="info-row">
      <span class="info-label">今日主线</span>
      <span class="info-value">{main_sec}</span>
    </div>
    <div class="info-row">
      <span class="info-label">市场风格</span>
      <span class="info-value">{style}</span>
    </div>
    <div class="info-row">
      <span class="info-label">主线判断</span>
      <span class="info-value">{theme}</span>
    </div>
    <div class="info-row">
      <span class="info-label">加减分项</span>
      <span class="info-value">{reasons}</span>
    </div>
  </div>
</div>

{leaders_cards}

{hot_cards}

{ind_cards}

{risk_box}

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
        print("❌ 无法获取邮件密码")
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = FROM_ADDR
    msg["To"]      = TO_ADDR
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(FROM_ADDR, pw)
        server.sendmail(FROM_ADDR, [TO_ADDR], msg.as_string())

def main():
    today = date.today().strftime("%Y-%m-%d")
    print(f"📅 {today} 开盘检查开始...")

    # 交易日判断：调用 is_trading_day.sh 双重验证（周末+法定节假日）
    import subprocess as _sp
    import os as _os
    trading_sh = _os.path.expanduser("~/WorkBuddy/Claw/v74/data/is_trading_day.sh")
    try:
        # source脚本并调用is_trading_day函数（text=False避免中文编码异常）
        result = _sp.run(
            ["bash", "-c", f"source '{trading_sh}' && is_trading_day"],
            capture_output=True, text=False, timeout=10
        )
        is_trading = (result.returncode == 0)
        if not is_trading:
            msg = result.stdout.decode('utf-8', errors='replace').strip()
            print(f"⏭️  {today} 非交易日（{msg}）")
            sys.exit(0)
    except Exception as e:
        # fallback: 周末 + 法定节假日双重检查
        import datetime as _dt
        today_ymd = _dt.date.today().strftime('%Y%m%d')
        dow = _dt.date.today().weekday()
        # 内联节假日列表（与 is_trading_day.sh 同步）
        holidays = {
            '20260101','20260102','20260103',
            '20260215','20260216','20260217','20260218','20260219','20260220','20260221','20260222','20260223',
            '20260404','20260405','20260406',
            '20260501','20260502','20260503','20260504','20260505',
            '20260619','20260620','20260621',
            '20260925','20260926','20260927',
            '20261001','20261002','20261003','20261004','20261005','20261006','20261007',
        }
        if dow >= 5 or today_ymd in holidays:
            print(f"⏭️  {today} 非交易日（{'周末' if dow>=5 else '法定节假日'}），开盘检查跳过")
            sys.exit(0)

    raw_output, exit_code = run_check()

    # 清理ANSI颜色码，再解析
    clean_output = strip_ansi(raw_output)

    action_map = {0: "✅正常出手", 1: "⚠️谨慎出手", 2: "❌空仓"}
    action = action_map.get(exit_code, f"?({exit_code})")
    subject = f"【开盘检查 {today}】{action}"

    print(f"📊 脚本执行完成，exit={exit_code}")

    # 解析关键数据
    data = parse_output(clean_output)
    data["_exit_code"] = exit_code

    # 构建简洁邮件
    html_email = build_email(data, today)

    print(f"📧 发送邮件: {subject}")
    try:
        send_email(html_email, subject)
        print("✅ 邮件发送成功")
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")
        sys.exit(1)

    # 邮件正文也打印到控制台，方便调试
    print("\n" + "="*50)
    print("邮件预览：")
    print(html_email)

    return 0 if exit_code == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
