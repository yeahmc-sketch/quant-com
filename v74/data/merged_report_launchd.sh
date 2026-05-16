#!/bin/bash
# 5策略赛马 — 合并报告发送
# 每日15:30触发（所有策略跑完后）
# 功能：读取5个策略状态，生成一封合并HTML邮件

PYTHON="/usr/bin/python3"
LOG_DIR="/Users/chenshi/WorkBuddy/Claw/v74/data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/merged_report_$(date +%Y%m%d).log"

source "/Users/chenshi/WorkBuddy/Claw/v74/data/is_trading_day.sh"
if ! is_trading_day; then
    echo "$(date) 非交易日，合并报告跳过" > "$LOG_FILE"
    exit 0
fi

cd "/Users/chenshi/WorkBuddy/Claw" || exit 1
echo "========== $(date '+%Y-%m-%d %H:%M:%S') 合并报告发送 ==========" > "$LOG_FILE"
python3 v74/backtest/merged_daily_report.py >> "$LOG_FILE" 2>&1
EXIT=$?
echo "========== $(date '+%Y-%m-%d %H:%M:%S') 结束 (exit=$EXIT) ==========" >> "$LOG_FILE"
exit $EXIT
