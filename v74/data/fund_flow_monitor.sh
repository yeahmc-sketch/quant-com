#!/bin/bash
# 资金流向全量下载监控脚本
# 每10分钟检查一次，下载挂了就自动重启

LOG_DIR="/Users/chenshi/WorkBuddy/Claw/v74/data/logs"
MONITOR_LOG="$LOG_DIR/fund_flow_monitor.log"
SCRIPT="/Users/chenshi/WorkBuddy/Claw/v74/data/download_fund_flow.py"
DB="/Users/chenshi/WorkBuddy/Claw/data/db/market.db"
TARGET_STOCKS=3000

echo "========== $(date '+%Y-%m-%d %H:%M:%S') 监控启动 ==========" >> "$MONITOR_LOG"

while true; do
    sleep 600  # 10分钟

    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    
    # Step 1: 检查进程
    PID=$(pgrep -f "download_fund_flow.py" | grep -v $$)
    
    if [ -n "$PID" ]; then
        # 进程还在跑 → 检查日志进度
        LAST_LINE=$(tail -1 "$LOG_DIR/fund_flow_full_download.log" 2>/dev/null)
        echo "[$TIMESTAMP] 进程运行中 (PID $PID) | $LAST_LINE" >> "$MONITOR_LOG"
        continue
    fi
    
    echo "[$TIMESTAMP] ⚠️ 进程已退出，检查数据完整性..." >> "$MONITOR_LOG"
    
    # Step 2: 查询数据库覆盖股票数
    STOCK_COUNT=$(python3 -c "
import sqlite3
conn = sqlite3.connect('$DB')
cur = conn.cursor()
cur.execute('SELECT COUNT(DISTINCT ts_code) FROM fund_flow')
print(cur.fetchone()[0])
conn.close()
" 2>/dev/null)
    
    if [ -z "$STOCK_COUNT" ]; then
        STOCK_COUNT=0
    fi
    
    echo "[$TIMESTAMP] 当前覆盖股票: $STOCK_COUNT" >> "$MONITOR_LOG"
    
    if [ "$STOCK_COUNT" -ge "$TARGET_STOCKS" ]; then
        echo "[$TIMESTAMP] ✅ 全量下载完成！共覆盖 ${STOCK_COUNT}只股票" >> "$MONITOR_LOG"
        exit 0
    fi
    
    # Step 3: 重启下载
    echo "[$TIMESTAMP] 🔄 重启全量下载 (已覆盖 ${STOCK_COUNT}/3196)" >> "$MONITOR_LOG"
    nohup /usr/bin/python3 "$SCRIPT" > "$LOG_DIR/fund_flow_full_download.log" 2>&1 &
    echo "[$TIMESTAMP] 重启完成 PID $!" >> "$MONITOR_LOG"
done
