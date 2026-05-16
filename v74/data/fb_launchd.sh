#!/bin/bash
# FB 爆发力策略 — 每日执行流程
# 由 launchd 在 15:18 触发

WORK_DIR="/Users/chenshi/WorkBuddy/Claw/v74/backtest"
LOG_DIR="/Users/chenshi/WorkBuddy/Claw/v74/data/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/fb_$(date +%Y%m%d).log"
echo "========== $(date '+%Y-%m-%d %H:%M:%S') FB策略 ==========" > "$LOG_FILE"

# 交易日检查
source /Users/chenshi/WorkBuddy/Claw/v74/data/is_trading_day.sh
if ! is_trading_day; then
    echo "非交易日，跳过" >> "$LOG_FILE"
    exit 0
fi

cd "$WORK_DIR" || { echo "cd failed" >> "$LOG_FILE"; exit 1; }

python3 fb_paper_trade.py >> "$LOG_FILE" 2>&1
EXIT=$?

echo "[$(date '+%H:%M:%S')] FB完成 (exit=$EXIT)" >> "$LOG_FILE"
