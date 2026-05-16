#!/bin/bash
# MF v2.3 盘后模拟交易 - launchd 执行脚本
# 由 ~/Library/LaunchAgents/com.chenshi.mfv22.plist 调度
# A股交易日 15:20 执行
# v2.3 新增: 资金流向因子 main_pct + main_pct_5d

PYTHON="/usr/bin/python3"
LOG_DIR="/Users/chenshi/WorkBuddy/Claw/v74/data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/mfv22_$(date +%Y%m%d).log"

source "/Users/chenshi/WorkBuddy/Claw/v74/data/is_trading_day.sh"
if ! is_trading_day; then
    echo "$(is_trading_day 2>&1 | tail -1)，MF v2.3跳过" > "$LOG_FILE"
    exit 0
fi

cd "/Users/chenshi/WorkBuddy/Claw" || exit 1
echo "========== $(date '+%Y-%m-%d %H:%M:%S') MF v2.3 模拟交易开始 ==========" > "$LOG_FILE"
$PYTHON v74/backtest/mf22_paper_trade.py >> "$LOG_FILE" 2>&1
BT_EXIT=$?
echo "========== $(date '+%Y-%m-%d %H:%M:%S') MF v2.3 完成 ==========" >> "$LOG_FILE"

exit $BT_EXIT
