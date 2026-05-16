#!/bin/bash
# 更新节假日列表
# 每年1月交易所发布全年安排后运行一次
# 用法: ./update_holidays.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAW_FILE="$SCRIPT_DIR/is_trading_day.sh"
V15_FILE="/Users/chenshi/WorkBuddy/20260412151307/is_trade_day.sh"

echo "更新节假日列表"
echo "===================="
echo ""
echo "请从交易所官网获取 $YEAR 年全年休市安排："
echo "  上交所: http://www.sse.com.cn"
echo "  深交所: https://www.szse.cn"
echo ""
echo "手动修改 is_trading_day.sh 和 is_trade_day.sh 中的 is_holiday() 函数"
echo ""
echo "预计需要修改以下节假日（每年不同，以交易所公告为准）："
echo "  1. 元旦 (1月1日前后)"
echo "  2. 春节 (农历正月初一前后)"
echo "  3. 清明节 (4月5日前后)"
echo "  4. 劳动节 (5月1日)"
echo "  5. 端午节 (农历五月初五前后)"
echo "  6. 中秋节 (农历八月十五前后, 有时与国庆重合)"
echo "  7. 国庆节 (10月1-7日)"
echo ""
echo "两个文件需要同步更新:"
echo "  $CLAW_FILE"
echo "  $V15_FILE"
