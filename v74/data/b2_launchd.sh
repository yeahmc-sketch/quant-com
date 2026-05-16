#!/bin/bash
# B2 爆发力 - launchd 执行脚本
# 每日15:25触发（DB01 15:01 → V15 15:05 → V15SS 15:10 → V15SS N 15:15 → MF v2.3 15:20 → B2 15:25）
# 功能：加载Fusion20因子 + 2xLGBM预测分数 → Top1选股 → 20天持仓 → 邮件通知

PYTHON="/usr/bin/python3"
LOG_DIR="/Users/chenshi/WorkBuddy/Claw/v74/data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/b2_$(date +%Y%m%d).log"

source "/Users/chenshi/WorkBuddy/Claw/v74/data/is_trading_day.sh"
if ! is_trading_day; then
    echo "$(date) 非交易日，B2 爆发力跳过" > "$LOG_FILE"
    exit 0
fi

cd "/Users/chenshi/WorkBuddy/Claw" || exit 1
echo "========== $(date '+%Y-%m-%d %H:%M:%S') B2 爆发力开始 ==========" >> "$LOG_FILE"

# 执行B2模拟交易
$PYTHON v74/backtest/b2_paper_trade.py >> "$LOG_FILE" 2>&1
B2_EXIT=$?

echo "========== $(date '+%Y-%m-%d %H:%M:%S') B2 爆发力结束 (exit=$B2_EXIT) ==========" >> "$LOG_FILE"

exit $B2_EXIT
