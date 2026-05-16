#!/bin/bash
# DB01 数据下载 - launchd 执行脚本（v2 — 全 Tushare 统一下载）
# 由 ~/Library/LaunchAgents/com.chenshi.db01.plist 调度
# A股交易日 16:30 执行
#
# 2026-05-12 v2: 合并 tushare_poll_download.sh，移除冗余腾讯API步骤
#   - 删除 Step 1 (fast_tencent_daily.py) — 被 Tushare INSERT OR REPLACE 完全覆盖
#   - Step 1.5 改用 Tushare index_daily — 更稳定，额外提供 amount/pct_chg/pre_close
#   - 新增资金流向直接下载 — 不再轮询，16:30 Tushare 数据已到位
#   - 删除 com.chenshi.tushare_poll launchd

LOG_DIR="/Users/chenshi/WorkBuddy/Claw/v74/data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/db01_$(date +%Y%m%d).log"

# ===== 交易日验证 =====
SCRIPT_DIR="/Users/chenshi/WorkBuddy/Claw/v74/data"
source "$SCRIPT_DIR/is_trading_day.sh"
if ! is_trading_day; then
    echo "$(is_trading_day 2>&1 | tail -1)，DB01跳过" > "$LOG_FILE"
    exit 0
fi

echo "========== $(date '+%Y-%m-%d %H:%M:%S') DB01 数据下载开始 (v2 Tushare) ==========" > "$LOG_FILE"

SCRIPT_DIR="/Users/chenshi/WorkBuddy/Claw/v74/data"
cd "$SCRIPT_DIR" || exit 1

# ===== Step 1: Tushare 前复权日线 =====
echo "[$(date '+%H:%M:%S')] Step 1: download_tushare_daily_kline.py --today" >> "$LOG_FILE"
python3 download_tushare_daily_kline.py --today >> "$LOG_FILE" 2>&1
S1=$?
echo "[$(date '+%H:%M:%S')] Step 1 完成 (exit=$S1)" >> "$LOG_FILE"

# Tushare到位时间记录
echo "[$(date '+%H:%M:%S')] Step 1.2: log_tushare_time.py" >> "$LOG_FILE"
python3 log_tushare_time.py >> "$LOG_FILE" 2>&1

# ===== Step 1.5: 指数日线（Tushare index_daily） =====
echo "[$(date '+%H:%M:%S')] Step 1.5: download_tushare_index_daily.py" >> "$LOG_FILE"
python3 download_tushare_index_daily.py >> "$LOG_FILE" 2>&1
S15=$?
echo "[$(date '+%H:%M:%S')] Step 1.5 完成 (exit=$S15)" >> "$LOG_FILE"

# ===== Step 1.6: 每日基本面（Tushare daily_basic） =====
echo "[$(date '+%H:%M:%S')] Step 1.6: download_tushare_daily_basic.py" >> "$LOG_FILE"
python3 download_tushare_daily_basic.py >> "$LOG_FILE" 2>&1
S16=$?
echo "[$(date '+%H:%M:%S')] Step 1.6 完成 (exit=$S16)" >> "$LOG_FILE"

# ===== Step 1.8: 概念板块热度（东方财富，V15/V15SS依赖） =====
echo "[$(date '+%H:%M:%S')] Step 1.8: v9_concept_board_download.py" >> "$LOG_FILE"
python3 v9_concept_board_download.py >> "$LOG_FILE" 2>&1
S18=$?
echo "[$(date '+%H:%M:%S')] Step 1.8 完成 (exit=$S18)" >> "$LOG_FILE"

# ===== Step 2: 板块资金流向（东方财富） =====
echo "[$(date '+%H:%M:%S')] Step 2: download_sector_fund_flow.py" >> "$LOG_FILE"
python3 download_sector_fund_flow.py >> "$LOG_FILE" 2>&1
S2=$?
if [ $S2 -ne 0 ]; then
    echo "[$(date '+%H:%M:%S')] ⚠️ 东方财富失败，保留已有板块数据（不覆盖）" >> "$LOG_FILE"
fi
echo "[$(date '+%H:%M:%S')] Step 2 完成 (exit=$S2)" >> "$LOG_FILE"

# ===== Step 3: 个股资金流向（Tushare moneyflow，合并自 tushare_poll） =====
echo "[$(date '+%H:%M:%S')] Step 3: download_fund_flow_fast.py (Tushare)" >> "$LOG_FILE"
python3 download_fund_flow_fast.py >> "$LOG_FILE" 2>&1
S3=$?
echo "[$(date '+%H:%M:%S')] Step 3 完成 (exit=$S3)" >> "$LOG_FILE"

# ===== 邮件通知 =====
EXIT_CODE=$((S1 * 64 + S15 * 8 + S16 * 4 + S18 * 32 + S2 * 2 + S3))
echo "[$(date '+%H:%M:%S')] 发送邮件通知 (exit_code=$EXIT_CODE)" >> "$LOG_FILE"
python3 send_db01_email.py $EXIT_CODE >> "$LOG_FILE" 2>&1
echo "[$(date '+%H:%M:%S')] 邮件发送完成" >> "$LOG_FILE"

echo "========== $(date '+%Y-%m-%d %H:%M:%S') DB01 完成 ==========" >> "$LOG_FILE"
