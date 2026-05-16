#!/usr/bin/env python3
"""测试邮件HTML模板渲染"""
import sys
sys.path.insert(0, '/Users/chenshi/WorkBuddy/Claw/v74')

from run_morning_check_and_email import build_email

# 模拟数据
test_data = {
    "action": "normal",
    "action_text": "✅ 正常出手（仓位六成以上）",
    "zt": "30",
    "dt": "15",
    "max_lb": "3",
    "score": "5",
    "premium": 0.57,
    "up": 2800,
    "down": 1800,
    "breadth": "偏暖",
    "vol": "资金进攻意愿强，适合做多",
    "main": "AI应用",
    "style": "连板妖股（情绪炒作风格）",
    "theme": "主线明确（三大板块共振强势）",
    "reasons": "涨停充足(30家)  双板共振上涨",
    "risk": "暂无明显风险",
    "leaders": [
        {"code": "000001", "name": "平安银行", "lb": "3", "pct": "+10.0"},
        {"code": "600036", "name": "招商银行", "lb": "3", "pct": "+10.0"},
        {"code": "300750", "name": "宁德时代", "lb": "2", "pct": "+8.5"},
        {"code": "002594", "name": "比亚迪", "lb": "2", "pct": "+7.2"},
        {"code": "000858", "name": "五粮液", "lb": "2", "pct": "+6.8"},
        {"code": "300059", "name": "东方财富", "lb": "2", "pct": "+6.1"},
    ],
    "hot_sectors": [
        {"name": "AI应用", "pct": "+5.8"},
        {"name": "机器人概念", "pct": "+4.2"},
        {"name": "算力", "pct": "+3.6"},
    ],
    "industry_sectors": [
        {"name": "半导体", "pct": "+3.5"},
        {"name": "软件服务", "pct": "+2.8"},
        {"name": "电子元件", "pct": "+2.1"},
    ],
}

html = build_email(test_data, "2026-04-28")

# 保存到文件预览
output_path = "/Users/chenshi/WorkBuddy/Claw/v74/test_email_preview.html"
with open(output_path, 'w', encoding='utf-8') as f:
    f.write(html)

print(f"HTML预览已生成: {output_path}")
print(f"文件大小: {len(html)} 字节")
