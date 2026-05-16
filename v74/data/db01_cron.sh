#!/bin/bash
# DB01 数据下载守护脚本
# 每天 15:05 自动执行，通过 crontab 调度
# 使用方法: ./db01_cron.sh

SCRIPT_DIR="/Users/chenshi/WorkBuddy/Claw/v74/data"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/db01_$(date +%Y%m%d).log"

mkdir -p "$LOG_DIR"

echo "========== $(date '+%Y-%m-%d %H:%M:%S') DB01 任务开始 ==========" >> "$LOG_FILE"

# 切换到工作目录
cd "$SCRIPT_DIR" || { echo "目录不存在: $SCRIPT_DIR" >> "$LOG_FILE"; exit 1; }

# Step 1: 个股快照 + 指数
echo "[$(date '+%H:%M:%S')] Step 1: download_snapshot_sqlite.py" >> "$LOG_FILE"
python3 download_snapshot_sqlite.py >> "$LOG_FILE" 2>&1
STEP1_EXIT=$?

if [ $STEP1_EXIT -eq 0 ]; then
    echo "[$(date '+%H:%M:%S')] Step 1 成功" >> "$LOG_FILE"
else
    echo "[$(date '+%H:%M:%S')] Step 1 失败 (exit=$STEP1_EXIT)" >> "$LOG_FILE"
fi

# Step 2: 概念板块（防限流，已内置延迟）
echo "[$(date '+%H:%M:%S')] Step 2: v9_concept_board_download.py" >> "$LOG_FILE"
python3 v9_concept_board_download.py >> "$LOG_FILE" 2>&1
STEP2_EXIT=$?

if [ $STEP2_EXIT -eq 0 ]; then
    echo "[$(date '+%H:%M:%S')] Step 2 成功" >> "$LOG_FILE"
else
    echo "[$(date '+%H:%M:%S')] Step 2 失败 (exit=$STEP2_EXIT)" >> "$LOG_FILE"
fi

# 数据核查
echo "[$(date '+%H:%M:%S')] 数据核查:" >> "$LOG_FILE"
sqlite3 /Users/chenshi/WorkBuddy/Claw/data/db/market.db \
  "SELECT 'daily_kline', MAX(trade_date) FROM daily_kline
   UNION ALL SELECT 'concept_heat', MAX(trade_date) FROM concept_heat
   UNION ALL SELECT 'v9_index_daily', MAX(trade_date) FROM v9_index_daily;" >> "$LOG_FILE" 2>&1

echo "========== $(date '+%Y-%m-%d %H:%M:%S') DB01 任务完成 ==========" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"
