#!/bin/bash
# 开盘检查美化邮件发送脚本
# 由 launchd 每天 9:32 执行（非交易日电脑本来也不开）

SCRIPT_DIR="/Users/chenshi/WorkBuddy/Claw/v74"
PYTHON_SCRIPT="$SCRIPT_DIR/send_beautiful_email.py"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/morning_beautiful_$(date +%Y%m%d).log"

mkdir -p "$LOG_DIR"

echo "$(date '+%Y-%m-%d %H:%M:%S') 开始发送美化版开盘检查邮件" >> "$LOG_FILE"
cd "$SCRIPT_DIR"
python3 "$PYTHON_SCRIPT" >> "$LOG_FILE" 2>&1
exit_code=$?

if [ $exit_code -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') 邮件发送成功" >> "$LOG_FILE"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') 邮件发送失败 (exit=$exit_code)" >> "$LOG_FILE"
fi

exit $exit_code
