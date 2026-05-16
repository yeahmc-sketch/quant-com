#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开盘5分钟极简检查脚本 v2（对齐实战清单）
运行时间：每个交易日 9:30-9:35
功能：五步快速对照 → 市场情绪 / 资金意愿 / 连板梯队 / 赚钱主线 / 最终决策

依赖：
    pip install akshare pandas requests

用法：
    python3 morning_check.py          # 标准运行（~17秒）
    python3 morning_check.py --debug  # 打印原始数据
    python3 morning_check.py --full   # 含全市场涨跌家数（约80秒）
"""

import sys
import time
import sqlite3
import requests
from datetime import datetime, date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
# 实际DB在 data/ 目录，而非 v74/db/
DB_PATH = Path("/Users/chenshi/WorkBuddy/Claw/data/db/market.db")

# ── 颜色（A股惯例：红涨绿跌） ────────────────────────────────────────────────
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def cprint(text, color="", bold=False, newline=True):
    prefix = BOLD if bold else ""
    end = "\n" if newline else ""
    print(f"{prefix}{color}{text}{RESET}", end=end)


# ═══════════════════════════════════════════════════════════════════════════════
# 数据获取层
# ═══════════════════════════════════════════════════════════════════════════════

def get_market_index_with_vol():
    """
    获取五大指数 + 量比信息（腾讯API，稳定快速，~2秒）
    返回: dict {名称: {"pct": float, "vol_ratio": float, "amount": float(亿元)}}
    字段: f[1]=名称 f[3]=现价 f[32]=涨跌幅% f[37]=成交额(万元) f[49]=量比
    """
    import re
    code_map = {
        "sh000001": "上证指数",
        "sh000300": "沪深300",
        "sh000016": "上证50",
        "sz399001": "深证成指",
        "sz399006": "创业板指",
    }
    codes = ",".join(code_map.keys())
    result = {}
    try:
        r = requests.get(
            f"https://qt.gtimg.cn/q={codes}",
            headers={"Referer": "https://finance.qq.com"},
            timeout=5
        )
        for line in r.text.strip().split("\n"):
            m = re.search(r"v_([a-z]{2}\d+)=", line)
            if not m:
                continue
            key = m.group(1)
            name = code_map.get(key)
            if not name:
                continue
            fields = line.split("~")
            if len(fields) < 50:
                continue
            pct = float(fields[32]) if fields[32] else 0.0
            amt = float(fields[37]) / 1e4 if fields[37] else 0.0   # 万元→亿元
            vr  = float(fields[49]) if fields[49] else 1.0
            result[name] = {"pct": round(pct, 2), "vol_ratio": round(vr, 2), "amount": round(amt, 1)}
    except Exception as e:
        pass
    return result


def get_limit_pool(date_str):
    """
    获取涨停池+跌停池+连板高度+连板梯队分布（直连 push2ex 东方财富，无 AkShare 依赖）
    接口：push2ex.eastmoney.com/getTopicZTPool & getTopicDTPool
    字段：c=代码 n=名称 lbc=连板数 zdp=涨跌幅% p=价格(×1000) fund=封板资金 hybk=行业
    """
    # ── 涨停池 ─────────────────────────────────────────────────────────────
    zt_items = []
    try:
        r = requests.get(
            "https://push2ex.eastmoney.com/getTopicZTPool",
            params={
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "dpt": "wz.ztzt",
                "Pageindex": "0",
                "pagesize": "10000",
                "sort": "fbt:asc",
                "date": date_str,
            },
            timeout=8,
        )
        pool = r.json().get("data") or {}
        zt_items = pool.get("pool") or []
    except Exception as e:
        cprint(f"  ⚠️ 涨停池失败: {e}", YELLOW)

    # ── 跌停池 ─────────────────────────────────────────────────────────────
    dt_items = []
    try:
        r2 = requests.get(
            "https://push2ex.eastmoney.com/getTopicDTPool",
            params={
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "dpt": "wz.ztzt",
                "Pageindex": "0",
                "pagesize": "10000",
                "sort": "fund:asc",
                "date": date_str,
            },
            timeout=8,
        )
        pool2 = r2.json().get("data") or {}
        dt_items = pool2.get("pool") or []
    except Exception:
        pass

    zt_count = len(zt_items)
    dt_count = len(dt_items)

    # ── 连板梯队统计 ──────────────────────────────────────────────────────
    max_lb = 0
    lianban_stocks = []
    lb_stage_counts = {2: 0, 3: 0, 4: 0, 5: 0}

    for it in zt_items:
        lb = int(it.get("lbc") or 0)
        if lb > max_lb:
            max_lb = lb
        if lb >= 2:
            # 统一为 morning_check analyze() 期望的列名格式
            lianban_stocks.append({
                "代码":     it.get("c", ""),
                "名称":     it.get("n", ""),
                "连板数":   lb,
                "涨跌幅":   round((it.get("zdp") or 0), 2),
                "最新价":   round((it.get("p") or 0) / 1000, 2),
                "所属行业": it.get("hybk", ""),
                "封板资金": it.get("fund", 0),
            })

    # 按连板数降序排，取前15
    lianban_stocks.sort(key=lambda x: x["连板数"], reverse=True)
    lianban_stocks = lianban_stocks[:15]

    for stage in [2, 3, 4]:
        lb_stage_counts[stage] = sum(1 for it in zt_items if int(it.get("lbc") or 0) == stage)
    lb_stage_counts[5] = sum(1 for it in zt_items if int(it.get("lbc") or 0) >= 5)

    return {
        "zt": zt_items, "dt": dt_items,
        "max_lianban": max_lb,
        "lianban_stocks": lianban_stocks,
        "lb_stage_counts": lb_stage_counts,
        "today_zt_count": zt_count,
        "today_dt_count": dt_count,
    }


def _empty_limit_pool():
    return {
        "zt": [], "dt": [],
        "max_lianban": 0, "lianban_stocks": [],
        "lb_stage_counts": {2: 0, 3: 0, 4: 0, 5: 0},
        "today_zt_count": 0, "today_dt_count": 0,
    }


def get_hot_sectors():
    """概念板块涨幅排行（push2delay，稳定）"""
    url = "https://push2delay.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": 600, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f3",
        "fs": "m:90+t:2",
        "fields": "f12,f14,f3,f62",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    }
    try:
        r = requests.get(url, params=params, timeout=8)
        data = r.json().get("data", {}).get("diff", [])
        # push2delay 返回的 f3 已经是百分比（如 5.69 = 5.69%），直接使用
        result = []
        for d in data:
            if not isinstance(d.get("f3"), (int, float)):
                continue
            result.append({
                "板块名称": d.get("f14", ""),
                "涨跌幅":   d["f3"],
                "主力净流入_亿": round((d.get("f62") or 0) / 1e8, 2),
            })
        result.sort(key=lambda x: x["涨跌幅"], reverse=True)
        return result[:20]
    except Exception:
        return []


def get_industry_sectors():
    """行业板块涨幅排行（push2delay，稳定）"""
    url = "https://push2delay.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": 600, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f3",
        "fs": "m:90+t:3",
        "fields": "f12,f14,f3,f62",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    }
    try:
        r = requests.get(url, params=params, timeout=8)
        data = r.json().get("data", {}).get("diff", [])
        result = []
        for d in data:
            if not isinstance(d.get("f3"), (int, float)):
                continue
            result.append({
                "板块名称": d.get("f14", ""),
                "涨跌幅":   d["f3"],
                "主力净流入_亿": round((d.get("f62") or 0) / 1e8, 2),
            })
        result.sort(key=lambda x: x["涨跌幅"], reverse=True)
        return result[:10]
    except Exception:
        return []


def get_market_breadth_full():
    """
    全市场涨跌家数（~80秒，按需启用）
    返回: {"total_up": int, "total_down": int, "ratio": float}
    """
    import akshare as ak
    df = ak.stock_zh_a_spot_em()
    df = df[df["代码"].str.len() == 6].copy()
    sh = df[df["代码"].str.match(r"^(6|688)")]
    sz = df[df["代码"].str.match(r"^(0|1|2|3|4|5|7|8|9)")]
    sz = sz[~sz["代码"].str.startswith("688")]
    sh_up   = int((sh["最新价"] > sh["昨收"]).sum())
    sh_down = int((sh["最新价"] < sh["昨收"]).sum())
    sz_up   = int((sz["最新价"] > sz["昨收"]).sum())
    sz_down = int((sz["最新价"] < sz["昨收"]).sum())
    total   = sh_up + sh_down + sz_up + sz_down
    total_up   = sh_up + sz_up
    total_down = sh_down + sz_down
    ratio = total_up / total * 100 if total > 0 else 50.0
    return {"total_up": total_up, "total_down": total_down, "total": total, "ratio": round(ratio, 1)}


def get_yesterday_db_data():
    """
    从本地DB查昨日涨停/连板股（无网络，毫秒级）
    返回: {"zt_stocks": [...], "lb_stocks": [...], "yesterday": str}
    """
    if not DB_PATH.exists():
        return {"zt_stocks": [], "lb_stocks": [], "yesterday": ""}

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT trade_date FROM daily_kline GROUP BY trade_date ORDER BY trade_date DESC LIMIT 5"
    ).fetchall()
    if len(rows) < 2:
        conn.close()
        return {"zt_stocks": [], "lb_stocks": [], "yesterday": ""}

    # 找到最近两个不同的交易日
    dates = [rows[i]["trade_date"] for i in range(min(5, len(rows)))]
    unique_dates = []
    for d in dates:
        if not unique_dates or d != unique_dates[-1]:
            unique_dates.append(d)
        if len(unique_dates) >= 3:
            break

    if len(unique_dates) < 2:
        conn.close()
        return {"zt_stocks": [], "lb_stocks": [], "yesterday": ""}

    # 今日已入库 → yesterday = 最近日期，two_days_ago = 次近日期
    # 今日未入库 → yesterday = 次近日期，two_days_ago = 第三近日期
    today_str = date.today().strftime("%Y%m%d")
    if unique_dates[0] == today_str:
        yesterday    = unique_dates[1]  # 已入库的最近交易日（真正的"昨日"）
        two_days_ago = unique_dates[2] if len(unique_dates) > 2 else unique_dates[1]
    else:
        yesterday    = unique_dates[0]  # 今天未入库，把最近日期当"昨日"
        two_days_ago = unique_dates[1] if len(unique_dates) > 1 else unique_dates[0]

    # ── 昨日涨停股（东财pct_chg阈值口径）────────────────────────────────────
    # 非ST: pct_chg >= 9.9%  /  ST: pct_chg >= 4.9%（统一门槛）
    def _is_zt_pct(name, pct_chg):
        """根据pct_chg判断是否收盘涨停（东财口径）"""
        if pct_chg is None:
            return False
        if name and ('ST' in name or '*ST' in name):
            return pct_chg >= 4.9
        return pct_chg >= 9.9

    all_rows = conn.execute(
        """SELECT k.ts_code, s.name, k.close as y_close, k.pre_close as y_pre_close, k.pct_chg as y_pct
           FROM daily_kline k JOIN stocks s ON k.ts_code = s.ts_code
           WHERE k.trade_date = ?
             AND s.name NOT LIKE '%ST' AND s.name NOT LIKE '%*ST'
           ORDER BY k.pct_chg DESC""",
        (yesterday,)
    ).fetchall()

    zt_rows = [r for r in all_rows if _is_zt_pct(r[1], r[4])]
    zt_stocks = [dict(r) for r in zt_rows[:30]]

    # ── 昨日连板股（连续两天都达到东财涨停阈值）──────────────────────────────
    yzts_codes = {r[0] for r in zt_rows}  # 昨日涨停的代码集合

    # 找前天也涨停的股
    prev_rows = conn.execute(
        """SELECT k.ts_code, s.name, k.close, k.pre_close, k.pct_chg
           FROM daily_kline k JOIN stocks s ON k.ts_code = s.ts_code
           WHERE k.trade_date = ?
             AND s.name NOT LIKE '%ST' AND s.name NOT LIKE '%*ST'""",
        (two_days_ago,)
    ).fetchall()
    prev_zt_codes = {r[0] for r in prev_rows if _is_zt_pct(r[1], r[4])}

    lb_codes = yzts_codes & prev_zt_codes
    lb_stocks = [
        dict(r) for r in zt_rows if r[0] in lb_codes
    ]
    conn.close()
    return {"zt_stocks": zt_stocks, "lb_stocks": lb_stocks, "yesterday": yesterday}


def get_yesterday_zt_premium(zt_stocks):
    """
    通过腾讯API查询昨日涨停股今日开盘溢价（约3-5秒）
    返回: list of dict，补充了今日涨跌幅和溢价状态
    注意：集合竞价期间返回的价格≈今日开盘价
    """
    if not zt_stocks:
        return []

    # 构造腾讯代码列表（如 sz300422, sh688268）
    codes = []
    for s in zt_stocks:
        raw_code = s["ts_code"]
        pure_code = raw_code.split(".")[0]
        suffix = raw_code.split(".")[1] if "." in raw_code else ""
        if suffix == "SH" or pure_code.startswith("6") or pure_code.startswith("688"):
            codes.append(f"sh{pure_code}")
        else:
            codes.append(f"sz{pure_code}")
    codes_str = ",".join(codes[:30])  # 最多30只，防止URL过长

    try:
        r = requests.get(
            f"https://qt.gtimg.cn/q={codes_str}",
            headers={"Referer": "https://finance.qq.com"},
            timeout=8
        )
        price_map = {}
        for line in r.text.strip().split("\n"):
            if "v_" not in line:
                continue
            fields = line.split("~")
            if len(fields) < 35:
                continue
            # 字段4=今开价（集合竞价成交价），字段5=昨收价，字段3=现价，字段32=涨跌幅%
            try:
                raw_key = fields[0]   # 格式: v_sh600000="1
                # 去掉前缀 v_ 和尾部的 ="数字
                code_raw = raw_key.replace("v_", "").split("=")[0]  # → sh600000 或 sz000001
                open_price = float(fields[4]) if fields[4] else 0.0  # 今日开盘价
                y_close    = float(fields[5]) if fields[5] else 0.0  # 昨日收盘价
                cur_price  = float(fields[3]) if fields[3] else 0.0  # 现价
                pct_today  = float(fields[32]) if fields[32] else 0.0
                if y_close > 0 and open_price > 0:
                    # 集合竞价溢价率：以今日开盘价相对昨日收盘价的涨跌幅度
                    open_premium = (open_price - y_close) / y_close * 100
                    price_map[code_raw] = {
                        "open_price": open_price,
                        "cur_price": cur_price,
                        "y_close": y_close,
                        "pct_today": pct_today,
                        "open_premium": round(open_premium, 2),
                    }
            except (ValueError, IndexError):
                continue

        # 合并到原始数据
        # DB中ts_code格式为 300422.SZ / 688268.SH，需要先去掉后缀
        enriched = []
        for s in zt_stocks:
            raw_code = s["ts_code"]  # 格式: 300422.SZ
            # 去掉 .SZ / .SH 后缀
            pure_code = raw_code.split(".")[0]  # → 300422
            suffix     = raw_code.split(".")[1] if "." in raw_code else ""  # → SZ
            if suffix == "SH" or pure_code.startswith("6") or pure_code.startswith("688"):
                key = f"sh{pure_code}"
            else:
                key = f"sz{pure_code}"
            info = price_map.get(key, {})
            enriched.append({
                **s,
                "cur_price": info.get("cur_price", 0),
                "open_price": info.get("open_price", 0),
                "pct_today": info.get("pct_today", 0),
                "open_premium": info.get("open_premium", None),
            })
        return enriched
    except Exception:
        return zt_stocks


def detect_style(lianban_stocks, hot_sectors, industry_sectors):
    """
    根据数据特征判断当日市场风格
    返回: "连板妖股" / "趋势业绩" / "弱势修复"
    """
    # 特征1：连板高度高 + 涨停数量多 → 情绪炒作风格（列名固定为"连板数"）
    max_lb = lianban_stocks[0].get("连板数", 0) if lianban_stocks else 0

    # 特征2：行业板块整体涨幅大 → 趋势/机构风格
    top_ind_pct = max((s.get("涨跌幅", 0) or 0) for s in industry_sectors[:3]) if industry_sectors else 0

    # 特征3：概念涨幅分散，无集中主线 → 弱势修复
    top3_concept_pct = sum((s.get("涨跌幅", 0) or 0) for s in hot_sectors[:3]) if hot_sectors else 0

    if max_lb >= 3:
        return "连板妖股（情绪炒作风格）"
    elif top_ind_pct > top3_concept_pct * 0.5 and top_ind_pct > 2.0:
        return "趋势业绩（机构抱团风格）"
    else:
        return "弱势修复（低位首板套利）"


# ═══════════════════════════════════════════════════════════════════════════════
# 分析决策引擎（完全对齐清单五步）
# ═══════════════════════════════════════════════════════════════════════════════

def analyze(indices, limit_pool, hot_sectors, industry_sectors,
            ydata, breadth_full, yesterday_premium, is_debug=False):
    """
    五步分析：
      第一步：全局情绪定调
      第二步：量能 + 量比 判定资金意愿
      第三步：连板梯队 + 高标龙头 + 梯队完整性
      第四步：赚钱效应题材 + 风格判定
      第五步：最终决策
    """

    print()
    cprint("═" * 60, CYAN, bold=True)
    cprint("  📊  开盘5分钟 · 极简实战检查清单 v2", CYAN, bold=True)
    cprint(f"  🕐  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", CYAN)
    cprint("═" * 60, CYAN, bold=True)

    zt_count       = limit_pool["today_zt_count"]
    dt_count       = limit_pool["today_dt_count"]
    max_lb         = limit_pool["max_lianban"]
    lb_list        = limit_pool["lianban_stocks"]
    lb_stage_cnts  = limit_pool["lb_stage_counts"]
    yzdata         = ydata.get("zt_stocks", [])
    ybdata         = ydata.get("lb_stocks", [])
    ydate          = ydata.get("yesterday", "")
    hs300_vr       = indices.get("沪深300", {}).get("vol_ratio", 1.0)
    sh300_vr       = indices.get("上证指数", {}).get("vol_ratio", 1.0)
    sh_index_pct   = indices.get("上证指数", {}).get("pct", 0)
    cy_index_pct   = indices.get("创业板指", {}).get("pct", 0)

    # ══════════════════════════════════════════════════════════════════════════
    # 第一步：全局情绪定调
    # ══════════════════════════════════════════════════════════════════════════
    print()
    cprint("┌─────────────────────────────────────────────┐", CYAN)
    cprint("│  【第一步】全局情绪定调（30秒）              │", CYAN)
    cprint("└─────────────────────────────────────────────┘", CYAN)

    # ① 涨跌家数
    if breadth_full:
        tu = breadth_full["total_up"]
        td = breadth_full["total_down"]
        ratio = breadth_full["ratio"]
        tu_c = RED if ratio >= 50 else GREEN
        if ratio > 55:
            breadth_tag = f"{tu_c}偏暖（容错率高）{RESET}"
        elif ratio < 45:
            breadth_tag = f"{RED}偏弱（少追高轻仓）{RESET}"
        else:
            breadth_tag = f"{YELLOW}分化（只做主线核心）{RESET}"
        print(f"  涨跌家数：🔴 {tu} vs 🟢 {td}  上涨 {ratio:.1f}%  → {breadth_tag}")
    else:
        ratio = 50.0
        if sh_index_pct > 0.5:
            ratio = 60; breadth_tag = f"{RED}偏暖{RESET}"
        elif sh_index_pct < -0.5:
            ratio = 38; breadth_tag = f"{GREEN}偏弱{RESET}"
        else:
            breadth_tag = f"{YELLOW}分化{RESET}"
        print(f"  涨跌家数：{breadth_tag}（无全量数据，以指数估算）")

    # ② 昨日涨停今日溢价
    if yesterday_premium:
        prem_values = [s["open_premium"] for s in yesterday_premium if s.get("open_premium") is not None]
        avg_prem = sum(prem_values) / len(prem_values) if prem_values else 0
        red_count = sum(1 for v in prem_values if v > 0)
        green_count = sum(1 for v in prem_values if v <= 0)
        if avg_prem > 1.0:
            prem_tag = f"{RED}红盘高开 >1%（短线溢价在线，赚钱效应强）{RESET}"
        elif avg_prem < -1.0:
            prem_tag = f"{GREEN}绿盘低开/闷杀（核按钮多，亏钱效应主导）{RESET}"
        else:
            prem_tag = f"{YELLOW}平开微绿（分歧市，只低吸不接力）{RESET}"
        print(f"  昨日涨停({len(yesterday_premium)}只)今日溢价：")
        print(f"    均幅 {avg_prem:+.2f}%  红{RED}{red_count}{RESET}  绿{green_count}  → {prem_tag}")
        if is_debug:
            print(f"    [DEBUG] 溢价明细（前5）:")
            for s in yesterday_premium[:5]:
                p = s.get("open_premium")
                tag = f"{RED}{p:+.2f}%{RESET}" if p and p > 0 else f"{GREEN}{p:+.2f}%{RESET}" if p else "N/A"
                print(f"      {s['name']}: {tag}")
    elif yzdata:
        print(f"  昨日涨停({ydate}) {len(yzdata)}只 → DB已记录，今日溢价数据待开盘后获取")
    else:
        print(f"  昨日涨停数据：暂无（DB无记录）")

    # ③ 跌停/大面数量
    if dt_count < 5:
        dt_tag = f"{GREEN}环境安全{RESET}"
    elif dt_count >= 10:
        dt_tag = f"{RED}退潮期，管住手⚠️{RESET}"
    else:
        dt_tag = f"{YELLOW}数量偏高{RESET}"
    print(f"  跌停数量：{RED}{dt_count}家{RESET}  → {dt_tag}")

    # ══════════════════════════════════════════════════════════════════════════
    # 第二步：量能 + 量比 判定资金意愿
    # ══════════════════════════════════════════════════════════════════════════
    print()
    cprint("┌─────────────────────────────────────────────┐", CYAN)
    cprint("│  【第二步】量能 & 量比 资金意愿（1分钟）      │", CYAN)
    cprint("└─────────────────────────────────────────────┘", CYAN)

    # 两市竞价成交额（以沪深300量比代理竞价活跃度）
    print(f"  主要指数量比：")
    for name in ["上证指数", "深证成指", "创业板指", "沪深300"]:
        info = indices.get(name, {})
        vr = info.get("vol_ratio", 1.0)
        pct = info.get("pct", 0)
        sign = "+" if pct > 0 else ""
        pct_c = RED if pct > 0 else GREEN
        if vr >= 1.5:
            vr_tag = f"{RED}明显放量{RESET}"
        elif vr >= 1.0:
            vr_tag = f"{YELLOW}温和放量{RESET}"
        elif vr >= 0.8:
            vr_tag = f"{YELLOW}缩量观望{RESET}"
        else:
            vr_tag = f"{GREEN}极度缩量{RESET}"
        print(f"    {name}：{pct_c}{sign}{pct}%{RESET}  量比 {vr:.2f} → {vr_tag}")

    # 综合竞价判断
    avg_vr = sum(indices.get(n, {}).get("vol_ratio", 1.0) for n in ["上证指数", "深证成指", "创业板指"]) / 3
    if avg_vr >= 1.5:
        vol_tag = f"{RED}资金进攻意愿强，适合做多{RESET}"
        vol_score = 2
    elif avg_vr >= 1.0:
        vol_tag = f"{YELLOW}量能正常，结构性机会{RESET}"
        vol_score = 1
    elif avg_vr >= 0.8:
        vol_tag = f"{YELLOW}缩量，资金观望，回避高位{RESET}"
        vol_score = -1
    else:
        vol_tag = f"{GREEN}极度缩量，全天无量，少出手{RESET}"
        vol_score = -2
    print(f"\n  竞价综合判断：{vol_tag}  (量比{avg_vr:.2f})")

    # ══════════════════════════════════════════════════════════════════════════
    # 第三步：连板梯队 + 龙头 + 梯队完整性
    # ══════════════════════════════════════════════════════════════════════════
    print()
    cprint("┌─────────────────────────────────────────────┐", CYAN)
    cprint("│  【第三步】连板梯队 & 高标龙头（1分钟）        │", CYAN)
    cprint("└─────────────────────────────────────────────┘", CYAN)

    # 连板高度
    if max_lb >= 5:
        lb_tag = f"{RED}情绪高潮（赚钱效应拉满，可重仓）{RESET}"
        lb_score = 3
    elif max_lb >= 3:
        lb_tag = f"{YELLOW}情绪中性（局部行情，聚焦主线）{RESET}"
        lb_score = 1
    elif max_lb >= 1:
        lb_tag = f"{RED}情绪冰点（严禁接力）{RESET}"
        lb_score = -1
    else:
        lb_tag = f"{YELLOW}暂无连板（情绪极弱）{RESET}"
        lb_score = -2
    print(f"  市场最高连板：{RED}{max_lb}板{RESET}  → {lb_tag}")

    # 梯队完整性
    s2 = lb_stage_cnts.get(2, 0)
    s3 = lb_stage_cnts.get(3, 0)
    s4 = lb_stage_cnts.get(4, 0)
    s5 = lb_stage_cnts.get(5, 0)
    print(f"  梯队分布：")
    print(f"    首板→1进2：{s2}只   2进3：{s3}只   3进4：{s4}只   4进5+：{s5}只")
    if s2 > 0 and s3 > 0 and s4 > 0:
        ladder_tag = f"{GREEN}梯队完整（题材持续性强）{RESET}"
        ladder_score = 2
    elif s2 > 0 and s3 > 0:
        ladder_tag = f"{YELLOW}梯队基本完整{RESET}"
        ladder_score = 1
    elif s2 > 0 and s3 == 0:
        ladder_tag = f"{RED}梯队断层（2板封顶，题材一日游）{RESET}"
        ladder_score = -1
    else:
        ladder_tag = f"{RED}梯队缺失（无接力资金）{RESET}"
        ladder_score = -2
    print(f"  梯队完整性：{ladder_tag}")

    # 高标龙头详情
    if lb_list:
        # 找到连板数列名（AkShare列名可能是"连板数"/"连续板数"等）
        lb_key = next((c for c in lb_list[0].keys() if "连板" in c), None)
        print(f"\n  {'代码':<8} {'名称':<8} {'连板':>4} {'今日涨幅':>8} {'行业':<10}")
        print(f"  {'-'*50}")
        for s in lb_list[:8]:
            code = str(s.get("代码", ""))
            name = str(s.get("名称", ""))[:6]
            lb = int(float(s[lb_key])) if lb_key else 0
            pct = s.get("涨跌幅", 0) or 0
            ind = str(s.get("所属行业", ""))[:10]
            pct_c = RED if pct > 0 else GREEN
            sign = "+" if pct > 0 else ""
            print(f"  {code:<8} {name:<8} {lb:>4}板 {pct_c}{sign}{pct:>5.1f}%{RESET}  {ind}")
        # 输出易解析的机器可读格式（用于邮件解析）
        print("【连板龙头】")
        for s in lb_list[:6]:
            code = str(s.get("代码", ""))
            name = str(s.get("名称", ""))[:6]
            lb = int(float(s[lb_key])) if lb_key else 0
            pct = s.get("涨跌幅", 0) or 0
            sign = "+" if pct > 0 else ""
            print(f"LB:{code}|{name}|{lb}板|{sign}{pct:.1f}%")

    # ══════════════════════════════════════════════════════════════════════════
    # 第四步：赚钱效应主线 + 风格判定
    # ══════════════════════════════════════════════════════════════════════════
    print()
    cprint("┌─────────────────────────────────────────────┐", CYAN)
    cprint("│  【第四步】赚钱效应题材 & 风格（1分钟）       │", CYAN)
    cprint("└─────────────────────────────────────────────┘", CYAN)

    if hot_sectors:
        print(f"  {'概念板块TOP10':<14} {'涨跌幅':>7}")
        print(f"  {'-'*28}")
        for s in hot_sectors[:10]:
            name = str(s.get("板块名称", ""))[:12]
            pct  = s.get("涨跌幅", 0) or 0
            pct_c = RED if pct > 0 else GREEN
            sign = "+" if pct > 0 else ""
            print(f"  {name:<14} {pct_c}{sign}{pct:>5.1f}%{RESET}")

        top3 = sum(s.get("涨跌幅", 0) or 0 for s in hot_sectors[:3])
        top5 = sum(s.get("涨跌幅", 0) or 0 for s in hot_sectors[:5])
        if top3 > 15:
            theme_tag = f"{GREEN}主线明确（三大板块共振强势）{RESET}"
        elif top3 > 8:
            theme_tag = f"{YELLOW}主线局部（方向集中，容错尚可）{RESET}"
        else:
            theme_tag = f"{RED}无清晰主线（板块轮动快，不参与杂毛）{RESET}"
        print(f"\n  主线判断：{theme_tag}")
    else:
        print("  ⚠️ 概念板块数据暂缺")

    if industry_sectors:
        print(f"\n  {'行业板块TOP5':<14} {'涨跌幅':>7}")
        print(f"  {'-'*28}")
        for s in industry_sectors[:5]:
            name = str(s.get("板块名称", ""))[:12]
            pct  = s.get("涨跌幅", 0) or 0
            pct_c = RED if pct > 0 else GREEN
            sign = "+" if pct > 0 else ""
            print(f"  {name:<14} {pct_c}{sign}{pct:>5.1f}%{RESET}")

    # 风格判定
    style = detect_style(lb_list, hot_sectors, industry_sectors)
    print(f"\n  风格判定：{RED}{style}{RESET}")

    # ══════════════════════════════════════════════════════════════════════════
    # 第五步：最终决策
    # ══════════════════════════════════════════════════════════════════════════
    print()
    cprint("┌─────────────────────────────────────────────┐", CYAN)
    cprint("│  【第五步】最终决策（30秒）                  │", CYAN)
    cprint("└─────────────────────────────────────────────┘", CYAN)

    # ── 综合评分 ───────────────────────────────────────────────────────────
    score = 0
    reasons = []

    # 涨跌家数
    if breadth_full:
        r = breadth_full["ratio"]
        if r >= 60:   score += 2; reasons.append("上涨家数>60%")
        elif r <= 40: score -= 2; reasons.append("上涨家数<40%")
        elif r >= 55:  score += 1; reasons.append("上涨家数偏多")
        elif r <= 45: score -= 1; reasons.append("上涨家数偏少")

    # 昨日涨停溢价
    if yesterday_premium:
        prem_vals = [s["open_premium"] for s in yesterday_premium if s.get("open_premium") is not None]
        if prem_vals:
            avg_p = sum(prem_vals) / len(prem_vals)
            if avg_p > 1.0:   score += 2; reasons.append("昨日涨停溢价高")
            elif avg_p < -1.0: score -= 2; reasons.append("昨日涨停闷杀")
            elif avg_p > 0:   score += 1; reasons.append("昨日涨停小盈")

    # 涨停数量
    if zt_count >= 50:  score += 2; reasons.append(f"涨停极多({zt_count}家)")
    elif zt_count >= 30: score += 1; reasons.append(f"涨停充足({zt_count}家)")
    elif zt_count < 10:  score -= 1; reasons.append(f"涨停偏少({zt_count}家)")

    # 跌停数量
    if dt_count >= 10:  score -= 3; reasons.append(f"跌停过多⚠️({dt_count}家)")
    elif dt_count >= 5:  score -= 1; reasons.append(f"跌停略多({dt_count}家)")

    # 连板高度
    score += lb_score
    if max_lb >= 5:   reasons.append(f"{max_lb}连板强")
    elif max_lb >= 3: reasons.append(f"{max_lb}连板中性")

    # 梯队完整性
    score += ladder_score

    # 量能
    score += vol_score

    # 双板共振
    if sh_index_pct > 0.5 and cy_index_pct > 0.5:
        score += 2; reasons.append("双板共振上涨")
    elif sh_index_pct < -0.5 and cy_index_pct < -0.5:
        score -= 2; reasons.append("双板共振大跌⚠️")
    elif sh_index_pct < -1.0:
        score -= 1; reasons.append("上证<-1%⚠️")

    # ── 三档决策 ───────────────────────────────────────────────────────────
    print(f"  综合评分：{BOLD}{score}分{RESET}")
    print(f"  加分/减分项：{'  '.join(reasons) if reasons else '无明显极端信号'}")

    print()
    if score >= 5:
        print(f"  ┌─────────────────────────────────────────────┐")
        print(f"  │  ✅ 正常出手（仓位六成以上）                 │")
        print(f"  │  条件：涨停溢价在线 + 连板高度正常 +         │")
        print(f"  │        主线清晰 + 竞价温和放量               │")
        print(f"  │  推荐：追主线1进2连板 + 龙头反包             │")
        print(f"  └─────────────────────────────────────────────┘")
        final_action = "normal"
    elif score >= 1:
        print(f"  ┌─────────────────────────────────────────────┐")
        print(f"  │  ⚠️ 谨慎出手（仓位三成，低吸为主）          │")
        print(f"  │  条件：涨跌分化 / 中位股炸板预期 /          │")
        print(f"  │        题材轮动快 / 两市整体缩量            │")
        print(f"  │  推荐：只低吸主线核心，不追跟风杂毛         │")
        print(f"  └─────────────────────────────────────────────┘")
        final_action = "caution"
    else:
        print(f"  ┌─────────────────────────────────────────────┐")
        print(f"  │  ❌ 直接空仓（严禁接力）                      │")
        print(f"  │  条件：涨停闷杀 + 连板高度压缩 + 跌停激增    │")
        print(f"  │        + 无主线全是杂毛一日游                │")
        print(f"  │  推荐：空仓观望，若做只做低位首板套利         │")
        print(f"  └─────────────────────────────────────────────┘")
        final_action = "empty"

    # ── 今日选股方向 ──────────────────────────────────────────────────────
    print()
    if hot_sectors:
        top1 = hot_sectors[0]
        top2 = hot_sectors[1] if len(hot_sectors) > 1 else None
        top3 = hot_sectors[2] if len(hot_sectors) > 2 else None

        s1n = top1.get("板块名称", "")
        s1p = top1.get("涨跌幅", 0) or 0
        s1s = "+" if s1p > 0 else ""
        print(f"  今日主线：{RED}{s1n}{RESET}  {s1s}{s1p:.1f}%")
        if top2:
            s2n = top2.get("板块名称", "")
            s2p = top2.get("涨跌幅", 0) or 0
            s2s = "+" if s2p > 0 else ""
            print(f"  辅线：{YELLOW}{s2n}{RESET}  {s2s}{s2p:.1f}%")
        if top3:
            s3n = top3.get("板块名称", "")
            s3p = top3.get("涨跌幅", 0) or 0
            s3s = "+" if s3p > 0 else ""
            print(f"  轮动：{s3n}  {s3s}{s3p:.1f}%")

        # 涨停集中板块
        if lb_list:
            sec_cnt = {}
            for s in lb_list:
                ind = s.get("所属行业", "")
                if ind:
                    sec_cnt[ind] = sec_cnt.get(ind, 0) + 1
            if sec_cnt:
                top_secs = sorted(sec_cnt.items(), key=lambda x: -x[1])[:3]
                secs_str = " / ".join([f"{n}({c}只)" for n, c in top_secs])
                print(f"  涨停集中：{secs_str}")

    # ── 风险提示 ─────────────────────────────────────────────────────────
    print()
    risk_items = []
    if dt_count >= 10:
        risk_items.append(f"跌停{dt_count}家，核按钮遍地")
    if max_lb >= 6:
        risk_items.append(f"{max_lb}板高位，注意炸板风险")
    if yesterday_premium:
        green_count = sum(1 for s in yesterday_premium
                           if s.get("open_premium") is not None and s["open_premium"] < 0)
        if green_count >= 5:
            risk_items.append(f"昨日涨停{green_count}只低开/闷杀")
    if risk_items:
        for item in risk_items:
            print(f"  ⚠️ 风险提示：{RED}{item}{RESET}")

    print()
    cprint("═" * 60, CYAN, bold=True)

    # ── 机器可读指标行（供邮件脚本解析，无ANSI，无emoji）─────────────────
    prem_val = ""
    if yesterday_premium:
        prem_list = [s["open_premium"] for s in yesterday_premium if s.get("open_premium") is not None]
        if prem_list:
            prem_val = f"{sum(prem_list)/len(prem_list):+.2f}"
    top_s = hot_sectors[0].get("板块名称", "") if hot_sectors else ""
    top_sp = f"{hot_sectors[0].get('涨跌幅', 0):+.1f}" if hot_sectors else "0"
    ind_s = industry_sectors[0].get("板块名称", "") if industry_sectors else ""
    ind_sp = f"{industry_sectors[0].get('涨跌幅', 0):+.1f}" if industry_sectors else "0"
    breadth_val = f"{tu}/{td}" if breadth_full else f"~{int(ratio)}pct"
    risk_str = " | ".join(risk_items) if risk_items else "无"
    # 板块名可能有空格，用下划线替代，避免破坏space-split解析
    ts_name = top_s.replace(" ", "_") if top_s else ""
    is_name = ind_s.replace(" ", "_") if ind_s else ""
    print(f"METRICS: zt={zt_count} dt={dt_count} up={breadth_val} prem={prem_val} "
          f"maxlb={max_lb} score={score} action={final_action} "
          f"topsector={ts_name}({top_sp}) indsector={is_name}({ind_sp}) "
          f"style={style} risk={risk_str}")

    return {
        "action":   final_action,
        "score":    score,
        "style":    style,
        "zt":       zt_count,
        "dt":       dt_count,
        "max_lb":   max_lb,
        "vol_ratio": round(avg_vr, 2),
        "top_sector": hot_sectors[0].get("板块名称", "") if hot_sectors else "",
        "top_sector_pct": hot_sectors[0].get("涨跌幅", 0) or 0 if hot_sectors else 0,
        "sh_index_pct": sh_index_pct,
        "cy_index_pct": cy_index_pct,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 主程序
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    is_debug = "--debug" in sys.argv
    is_full   = "--full"   in sys.argv

    print()
    cprint("🔍 开盘数据采集中...", CYAN, bold=True)
    print()

    today_str = date.today().strftime("%Y%m%d")
    start = time.time()

    # ① 大盘指数+量比（~3秒）
    cprint("  [1/6] 大盘指数+量比...", YELLOW)
    indices = get_market_index_with_vol()
    if is_debug:
        for n, v in indices.items():
            print(f"     {n}: pct={v['pct']}%, vol_ratio={v['vol_ratio']}")

    # ② 涨跌停池+连板梯队（~5秒）
    cprint("  [2/6] 涨跌停池+连板梯队...", YELLOW)
    limit_pool = get_limit_pool(today_str)
    if is_debug and limit_pool["zt"]:
        print(f"     涨停:{limit_pool['today_zt_count']} 跌停:{limit_pool['today_dt_count']} "
              f"最高连板:{limit_pool['max_lianban']}板")
        print(f"     梯队: {limit_pool['lb_stage_counts']}")

    # ③ 概念板块（~5秒）
    cprint("  [3/6] 概念板块...", YELLOW)
    hot_sectors = get_hot_sectors()
    if is_debug:
        print(f"     top5: {[s.get('板块名称') for s in hot_sectors[:5]]}")

    # ④ 行业板块（~5秒）
    cprint("  [4/6] 行业板块...", YELLOW)
    industry_sectors = get_industry_sectors()

    # ⑤ 昨日DB数据（毫秒）
    cprint("  [5/6] 昨日涨停/连板查询...", YELLOW)
    ydata = get_yesterday_db_data()
    if is_debug:
        print(f"     昨日涨停:{len(ydata.get('zt_stocks',[]))} 昨日连板:{len(ydata.get('lb_stocks',[]))}")

    # ⑥ 昨日涨停今日溢价（腾讯API，~3-5秒）
    cprint("  [6/6] 昨日涨停今日溢价...", YELLOW)
    yesterday_premium = get_yesterday_zt_premium(ydata.get("zt_stocks", []))
    if is_debug and yesterday_premium:
        for s in yesterday_premium[:5]:
            print(f"     {s['name']}: 溢价{s.get('open_premium','N/A')}")

    # ⑦ 全市场涨跌家数（可选，约80秒）
    breadth_full = None
    if is_full:
        print()
        cprint("  [+] 全市场涨跌家数（--full，约80秒）...", YELLOW)
        breadth_full = get_market_breadth_full()
        if breadth_full:
            print(f"     上涨{breadth_full['total_up']} / 下跌{breadth_full['total_down']}  "
                  f"比例{breadth_full['ratio']}%")

    elapsed = time.time() - start
    print(f"\n  ✅ 数据采集完成，耗时 {elapsed:.1f}秒\n")

    result = analyze(
        indices=indices,
        limit_pool=limit_pool,
        hot_sectors=hot_sectors,
        industry_sectors=industry_sectors,
        ydata=ydata,
        breadth_full=breadth_full,
        yesterday_premium=yesterday_premium,
        is_debug=is_debug,
    )

    return 0 if result["action"] == "normal" else (1 if result["action"] == "caution" else 2)


if __name__ == "__main__":
    sys.exit(main())
