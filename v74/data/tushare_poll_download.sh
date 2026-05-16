#!/bin/bash
# Tushare 数据轮询下载
# ====================
# 15:30 ~ 16:30 每15分钟轮询一次Tushare
# 数据到位后自动下载，记录时间，然后退出
# 由 launchd 在 15:30 触发

WORK_DIR="/Users/chenshi/WorkBuddy/Claw/v74/data"
LOG_DIR="$WORK_DIR/logs"
mkdir -p "$LOG_DIR"

# 交易日检查
source "$WORK_DIR/is_trading_day.sh"
if ! is_trading_day; then
    echo "[$(date '+%H:%M')] 非交易日，跳过" >> "$LOG_DIR/tushare_poll.log"
    exit 0
fi

POLL_LOG="$LOG_DIR/tushare_poll_$(date +%Y%m%d).log"

echo "========== $(date '+%H:%M') 开始轮询 Tushare ==========" > "$POLL_LOG"

MAX_ATTEMPTS=5          # 15:30, 15:45, 16:00, 16:15, 16:30
INTERVAL=900            # 15分钟
FIRST_ATTEMPT_AT=""     # 记录首次到位时间

cd "$WORK_DIR" || exit 1

for i in $(seq 1 $MAX_ATTEMPTS); do
    NOW=$(date '+%H:%M')
    echo "[$NOW] 第${i}次检查..." >> "$POLL_LOG"
    
    # 检查Tushare是否有今日数据
    RESULT=$(python3 -c "
import json, urllib.request
try:
    payload = json.dumps({'api_name':'daily','token':'2e50aa62898e603850c324723dbcf05fbb5fa671c6160d26e4593f41','params':{'trade_date':'$(date +%Y%m%d)','adj':'qfq'}}).encode()
    resp = json.loads(urllib.request.urlopen(urllib.request.Request('https://api.tushare.pro',data=payload,headers={'Content-Type':'application/json'}),timeout=15).read())
    items = resp.get('data',{}).get('items',[])
    print(len(items))
except:
    print('0')
" 2>&1)
    
    if [ "$RESULT" -gt 100 ]; then
        FIRST_ATTEMPT_AT="$NOW"
        echo "  ✅ 数据已到位! (${RESULT}只)" >> "$POLL_LOG"
        
        # 下载Tushare日线（覆盖腾讯数据）
        python3 download_tushare_daily_kline.py --today >> "$POLL_LOG" 2>&1
        TUSHARE_EXIT=$?

        # 下载资金流向（Tushare批量，几秒完成）
        python3 download_fund_flow_fast.py >> "$POLL_LOG" 2>&1
        FUND_EXIT=$?

        # 记录到位时间
        python3 -c "
import csv
from datetime import datetime
CSV = '/Users/chenshi/WorkBuddy/Claw/data/tushare_update_log.csv'
with open(CSV, 'a', newline='') as f:
    w = csv.writer(f)
    w.writerow(['$(date +%Y%m%d)', '$NOW', '$RESULT', '轮询到位'])
" >> "$POLL_LOG" 2>&1
        
        echo "[$(date '+%H:%M')] ✅ 完成 (Tushare exit=$TUSHARE_EXIT)" >> "$POLL_LOG"
        
        # 发送邮件通知
        python3 send_tushare_poll_email.py "$NOW" "$RESULT" >> "$POLL_LOG" 2>&1
        
        exit 0
    else
        echo "  ❌ 尚无数据 (${RESULT}只)" >> "$POLL_LOG"
    fi
    
    # 最后一次不等待
    if [ "$i" -lt "$MAX_ATTEMPTS" ]; then
        echo "  ⏳ 等待15分钟后重试..." >> "$POLL_LOG"
        sleep $INTERVAL
    fi
done

echo "[$(date '+%H:%M')] ⚠️ 轮询结束，Tushare今日未更新" >> "$POLL_LOG"
# 记录到CSV
python3 -c "
import csv
from datetime import datetime
CSV = '/Users/chenshi/WorkBuddy/Claw/data/tushare_update_log.csv'
with open(CSV, 'a', newline='') as f:
    w = csv.writer(f)
    w.writerow(['$(date +%Y%m%d)', '16:30后', '0', '轮询超时'])
" >> "$POLL_LOG" 2>&1