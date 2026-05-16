#!/usr/bin/env python3
"""34个精英因子列表 — A股量化选股 LGBM"""
ELITE_FACTORS = [
    # === 动量/反转 (7) ===
    'momentum_1m',           # 1月动量
    'momentum_3m',           # 3月动量
    'momentum_12m',          # 12月动量
    'overnight_ret',         # 隔夜收益
    'intraday_ret',          # 日内收益

    # === 波动/风险 (4) ===
    'intraday_volatility_5', # 5日日内波动率
    'downside_vol_20d',      # 20日下行波动
    'jump_vol_ratio',        # 跳空波动比
    'skewness_20d',          # 20日偏度
    'kurtosis_20d',          # 20日峰度

    # === 量价技术 (7) ===
    'adx_14',                # 14日ADX
    'pv_corr_20',            # 20日量价相关性
    'coil_amplitude',        # 盘整振幅
    'volume_momentum',       # 量能动量
    'vol_breakout',          # 波动率突破
    'avg_turnover_20',       # 20日平均换手率
    'amihud_illiq_20d',      # 20日非流动性指标

    # === 资金流 (3) ===
    'main_pct_5d',           # 5日主力资金净流入占比
    'main_pct_5d_sq',        # 主力资金占比平方项
    'amount_surge',          # 成交额突增

    # === 爆发力 (4) ===
    'gap_up_ma_bias',        # 高开均线偏离
    'follow_up',             # 高开跟随动能
    'surge_efficiency',      # 冲高效率
    'burst_pattern',         # 爆发模式

    # === 融资融券 (2) ===
    'margin_buy_ratio',      # 融资买入占比
    'margin_balance_growth_5d', # 5日融资余额增长率

    # === 财务 (6) ===
    'netprofit_yoy',         # 净利润同比
    'neg_debt_ratio',        # 负负债率（高=低杠杆）
    'gross_margin',          # 毛利率
    'asset_turn',            # 资产周转率
    'neg_pb_cs',             # 负PB横截面排名
    'ep_cs',                 # 盈利收益率横截面排名

    # === 其他 (2) ===
    'no_zt_5',               # 5日内无涨停
    'holder_num_chg',        # 股东人数变化率（正交新增）
]

# 因子分类统计
CATEGORIES = {
    '动量/反转': 5,
    '波动/风险': 5,
    '量价技术': 7,
    '资金流': 3,
    '爆发力': 4,
    '融资融券': 2,
    '财务': 6,
    '其他': 2,
}

if __name__ == '__main__':
    print(f"Total: {len(ELITE_FACTORS)} factors")
    for cat, count in CATEGORIES.items():
        print(f"  {cat}: {count}")
    print(f"\nFactors:")
    for i, f in enumerate(ELITE_FACTORS):
        print(f"  {i+1:2d}. {f}")
