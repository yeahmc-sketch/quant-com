#!/bin/bash
# LGBM因子每日预测 - launchd执行脚本
# 由 launchd 调度，A股交易日 15:01 执行（DB01下载之后）
# 功能：加载已训练模型，对当天数据做预测，保存分数

LOG_DIR="/Users/chenshi/WorkBuddy/Claw/v74/data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/lgbm_$(date +%Y%m%d).log"

source "/Users/chenshi/WorkBuddy/Claw/v74/data/is_trading_day.sh"
if ! is_trading_day; then
    echo "$(date) $(is_trading_day 2>&1 | tail -1) — LGBM因子预测跳过" > "$LOG_FILE"
    exit 0
fi

cd "/Users/chenshi/WorkBuddy/Claw" || exit 1
echo "========== $(date '+%Y-%m-%d %H:%M:%S') LGBM因子预测开始 ==========" > "$LOG_FILE"

# 预测当日LGBM分数
python3 v74/backtest/mf_lgbm_predict_today.py >> "$LOG_FILE" 2>&1
PRED_EXIT=$?

echo "========== $(date '+%Y-%m-%d %H:%M:%S') LGBM因子预测结束 (exit=$PRED_EXIT) ==========" >> "$LOG_FILE"

# 如果预测成功，通知日志
if [ $PRED_EXIT -eq 0 ]; then
    echo "LGBM预测完成" >> "$LOG_FILE"
else
    echo "LGBM预测失败" >> "$LOG_FILE"
fi

exit $PRED_EXIT
