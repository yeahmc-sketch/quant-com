#!/usr/bin/env python3
"""B2 策略回填脚本：从4月22日到5月15日"""
import sys
import subprocess
from pathlib import Path

dates = [
    '20260422','20260423','20260424','20260427','20260428',
    '20260429','20260430','20260506','20260507','20260508',
    '20260511','20260512','20260513','20260514','20260515'
]

total = len(dates)
for i, d in enumerate(dates):
    print(f'[{i+1}/{total}] {d}...', flush=True)
    r = subprocess.run(
        [sys.executable, '-u', str(Path(__file__).parent / 'b2_paper_trade.py'),
         '--force-date', d],
        capture_output=True, text=True, timeout=300
    )
    # 只打印关键行（买入/卖出/净值/错误）
    for line in r.stdout.split('\n'):
        stripped = line.strip()
        if any(kw in stripped for kw in ['买入','卖出','净值','保护','====','现金','完成','⚠️','截面','跳过','✅','LGBM']):
            print(f'  {stripped}')
    if r.returncode != 0:
        print(f'  ERROR exit={r.returncode}: {r.stderr[-300:]}')
        # 尝试继续
    # 打印一个小状态行
    try:
        import json
        sf = Path('../output/v74/portfolio/b2_trade_state.json')
        if sf.exists():
            s = json.loads(sf.read_text())
            ec = s.get('equity_curve', [])
            last_nav = ec[-1]['nav'] if ec else 0
            print(f'  现金:{s["cash"]:.0f} 持仓:{len(s["positions"])} 净值:{last_nav:.0f}')
    except:
        pass

print(f'\n全部完成! 共 {total} 个交易日')
