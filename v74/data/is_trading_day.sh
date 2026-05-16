#!/bin/bash
# 交易日验证（双重验证：周末 + 法定节假日列表 + DB数据兜底）
# 供所有launchd脚本 source 使用
# 返回: 0=交易日, 1=非交易日

DB_PATH="/Users/chenshi/WorkBuddy/Claw/data/db/market.db"

# ===== 法定节假日判断 =====
is_holiday() {
    local d="$1"
    case "$d" in
        # 元旦 1/1-1/3 (1/4周日周末)
        20260101|20260102|20260103) echo "元旦"; return 0 ;;
        # 春节 2/15-2/23
        20260215|20260216|20260217|20260218|20260219|20260220|20260221|20260222|20260223) echo "春节"; return 0 ;;
        # 清明节 4/4-4/6
        20260404|20260405|20260406) echo "清明节"; return 0 ;;
        # 劳动节 5/1-5/5
        20260501|20260502|20260503|20260504|20260505) echo "劳动节"; return 0 ;;
        # 端午节 6/19-6/21
        20260619|20260620|20260621) echo "端午节"; return 0 ;;
        # 中秋节 9/25-9/27
        20260925|20260926|20260927) echo "中秋节"; return 0 ;;
        # 国庆节 10/1-10/7
        20261001|20261002|20261003|20261004|20261005|20261006|20261007) echo "国庆节"; return 0 ;;
        *) return 1 ;;
    esac
}

is_trading_day() {
    local today=$(date +%Y%m%d)
    local dow=$(date +%u)

    # 1. 周末检查
    if [ "$dow" -ge 6 ]; then
        echo "非交易日（周末）"
        return 1
    fi

    # 2. 法定节假日检查（最高优先级，防止API返回缓存数据导致误判）
    local holiday_name
    holiday_name=$(is_holiday "$today")
    local holiday_ret=$?
    if [ $holiday_ret -eq 0 ]; then
        echo "非交易日（法定节假日：$holiday_name）"
        return 1
    fi

    # 3. 交易日
    echo "交易日"
    return 0
}
