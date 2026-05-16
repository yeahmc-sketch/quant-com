#!/usr/bin/env python3
"""
Fusion20 + LGBM Top3-10d + DMA 确认每日选股器
================================================
配置：
  - 因子集：Fusion20 (20 因子等权 Z-score)
  - 选股：Top3 (每天选 3 只)
  - 持有期：10 天
  - LGBM 权重：2 倍 (w=2)
  - DMA 确认：个股 DMA(5,35,5) > AMA 才买入

回测基线 (2024-2026):
  - +2xLGBM Top3-10d: 年化 42.17%, Sharpe 1.41, 回撤 -14.87%

用法:
  python3 fusion20_daily_selector.py           # 最新交易日
  python3 fusion20_daily_selector.py 20240102  # 指定日期回填
"""

import sys
import json
import pickle
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import sqlite3

# ===== 路径 =====
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
DB_PATH = PROJECT_DIR / "data" / "db" / "market.db"
OUTPUT_DIR = PROJECT_DIR / "output" / "v74" / "multi_factor"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ===== 配置参数 =====
INITIAL_CAPITAL = 50_000
COST_RATE = 0.003
EXCLUDE_PREFIXES = ['688', '30', '8', '4', '920']
HOLD_DAYS = 10
TOP_N = 3
W_LGBM = 2  # LGBM 权重 2 倍

# ===== Fusion20 因子定义 =====
MF_FACTOR_COLS = [
    'neg_volatility_20', 'neg_ma_bias', 'close_to_high', 'rev_5',
    'neg_pe_ttm', 'neg_pb', 'neg_ps_ttm', 'neg_ln_mv',
    'netprofit_yoy', 'op_yoy', 'or_yoy', 'roe',
    'avg_turnover_20', 'no_zt_5',
]
F101_CORE = ['alpha_16', 'alpha_13', 'alpha_40', 'alpha_88']
MAIN_FLOWS = ['main_pct', 'main_pct_5d']
FUSION_FACTOR_COLS = MF_FACTOR_COLS + F101_CORE + MAIN_FLOWS
SKIP_ZSCORE = {'no_zt_5'}

# ===== LGBM 模型加载 =====
LGBM_MODEL_PATH = OUTPUT_DIR / "phase2_a_results.pkl"

def load_lgbm_model():
    """加载 LGBM 模型和特征映射"""
    if not LGBM_MODEL_PATH.exists():
        print(f"⚠️  LGBM 模型不存在：{LGBM_MODEL_PATH}")
        return None, None
    
    try:
        with open(LGBM_MODEL_PATH, 'rb') as f:
            model_data = pickle.load(f)
        
        lgbm_model = model_data.get('model')
        feature_map = model_data.get('feature_map')  # {factor_name: index}
        
        print(f"✅ LGBM 模型加载成功")
        print(f"   IC: {model_data.get('ic', 'N/A')}")
        print(f"   特征数：{len(feature_map)}")
        
        return lgbm_model, feature_map
    except Exception as e:
        print(f"❌ LGBM 模型加载失败：{e}")
        return None, None


def load_fusion_factors(conn, start_date, end_date, factor_cols, exclude_prefixes):
    """加载 Fusion20 因子值"""
    print("  加载 Fusion20 因子...")
    
    # 获取股票列表
    stocks_df = pd.read_sql('SELECT ts_code, name FROM stocks', conn)
    for prefix in exclude_prefixes:
        stocks_df = stocks_df[~stocks_df['ts_code'].str.startswith(prefix)]
    
    valid_codes = set(stocks_df['ts_code'])
    print(f"   有效股票：{len(valid_codes)}只")
    
    # 加载日线数据
    kline = pd.read_sql(f'''
        SELECT ts_code, trade_date, open, high, low, close, pre_close, pct_chg, vol, amount
        FROM daily_kline
        WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
    ''', conn)
    kline = kline[kline['ts_code'].isin(valid_codes)]
    kline = kline.sort_values(['ts_code', 'trade_date'])
    
    # 计算 MF 因子
    print("   计算 MF 因子...")
    g = kline.groupby('ts_code')
    
    # 波动率
    kline['neg_vol_20'] = -g['pct_chg'].transform(lambda s: s.rolling(20).std())
    
    # 均线偏离
    kline['ma20'] = g['close'].transform(lambda s: s.rolling(20).mean())
    kline['neg_ma_bias'] = (kline['close'] - kline['ma20']) / kline['ma20']
    
    # 收盘价/最高价
    kline['close_to_high_raw'] = kline['close'] / g['high'].transform(lambda s: s.rolling(20).max())
    
    # 5 日反转
    kline['ret_5'] = g['pct_chg'].transform(lambda s: s.rolling(5).sum()) / 100
    
    # 估值因子
    basic = pd.read_sql(f'''
        SELECT ts_code, trade_date, pe_ttm, pb, ps_ttm, total_mv
        FROM daily_basic
        WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
    ''', conn)
    
    kline = kline.merge(basic, on=['ts_code', 'trade_date'], how='left')
    kline['neg_pe_ttm'] = -kline['pe_ttm']
    kline['neg_pb'] = -kline['pb']
    kline['neg_ps_ttm'] = -kline['ps_ttm']
    kline['neg_ln_mv'] = -np.log(kline['total_mv'])
    
    # 成长因子
    fina = pd.read_sql(f'''
        SELECT ts_code, ann_date, netprofit_yoy, op_yoy, or_yoy, roe
        FROM fina_indicator
        WHERE ann_date IS NOT NULL
    ''', conn)
    fina = fina.ffill()  # 向前填充财报日期
    
    # 将财报日期转换为最近交易日
    fina['trade_date'] = kline['trade_date'].max()
    kline = kline.merge(fina[['ts_code', 'netprofit_yoy', 'op_yoy', 'or_yoy', 'roe']], 
                        on='ts_code', how='left')
    
    # 流动性因子
    kline['avg_turnover_20'] = g['vol'].transform(lambda s: s.rolling(20).mean())
    
    # 二分类情绪因子
    kline['is_limit'] = (kline['pct_chg'] >= 9.5) & (kline['pre_close'] > 0)
    kline['no_zt_5'] = 1 - 2 * g['is_limit'].transform(lambda s: s.rolling(5).max().fillna(0))
    
    # 资金流因子 (简化版，用成交额占比代替)
    kline['amount_ma5'] = g['amount'].transform(lambda s: s.rolling(5).mean())
    kline['main_pct'] = kline['amount'] / kline['amount_ma5'] - 1
    kline['main_pct_5d'] = kline['main_pct'].rolling(5).mean()
    
    # 提取因子列
    factor_df = kline[factor_cols].copy()
    
    return factor_df, stocks_df


def preprocess_factors(factor_df, skip_zscore):
    """MAD 去极值 + Z-score 标准化"""
    print("  预处理因子...")
    
    result = pd.DataFrame(index=factor_df.index)
    
    for col in factor_df.columns:
        if col in skip_zscore:
            result[col] = factor_df[col]
        else:
            # MAD 去极值
            median = factor_df[col].median()
            mad = (factor_df[col] - median).abs().median()
            if mad > 1e-8:
                normalized = (factor_df[col] - median) / (1.4826 * mad)
            else:
                normalized = pd.Series(0, index=factor_df.index)
            
            # Z-score 标准化
            mean = normalized.mean()
            std = normalized.std()
            if std > 1e-8:
                result[col] = (normalized - mean) / std
            else:
                result[col] = 0
    
    return result


def calculate_ic_ir(factor_df, returns_df, window=12):
    """计算 IC_IR (信息系数 / 信息比率)"""
    print("  计算 IC_IR...")
    
    n_dates = len(factor_df)
    ic_scores = []
    ic_ir_scores = []
    
    for i in range(window, n_dates):
        # 滚动窗口
        fac_subset = factor_df.iloc[i-window:i]
        ret_subset = returns_df.iloc[i-window:i]
        
        # 计算 IC
        ic = fac_subset.corrwith(ret_subset)['pct_chg']
        
        # 计算 IC_IR (IC / IC 的标准差)
        ic_series = fac_subset.corrwith(ret_subset)['pct_chg']
        if ic_series.std() > 1e-8:
            ic_ir = ic / ic_series.std()
        else:
            ic_ir = 0
        
        ic_scores.append(ic)
        ic_ir_scores.append(ic_ir)
    
    ic_series = pd.Series(ic_scores, index=factor_df.index[window:])
    ic_ir_series = pd.Series(ic_ir_scores, index=factor_df.index[window:])
    
    return ic_series, ic_ir_series


def get_weighted_score(factor_df, ic_ir_series, lgbm_model, feature_map):
    """计算加权得分 (IC_IR 加权 + LGBM 加权)"""
    print("  计算加权得分...")
    
    # 基础分数 (IC_IR 加权)
    if ic_ir_series is not None and len(ic_ir_series) > 0:
        ic_weights = ic_ir_series.abs().clip(upper=3)  # 截断极端值
        ic_weights = (ic_weights - ic_weights.min()) / (ic_weights.max() - ic_weights.min() + 1e-8)
        
        base_score = (factor_df * ic_weights).sum(axis=1) / ic_weights.sum()
    else:
        # 回退到等权
        base_score = factor_df.mean(axis=1)
    
    # LGBM 预测分 (如果有模型)
    lgbm_score = None
    if lgbm_model is not None and feature_map is not None:
        # 准备 LGBM 输入特征
        lgbm_features = factor_df[list(feature_map.keys())].copy()
        
        # 缺失值填充
        for col in lgbm_features.columns:
            if col not in lgbm_features.columns:
                lgbm_features[col] = 0
        
        # 标准化 (使用训练时的均值和标准差)
        # 这里简化处理，直接用当前数据的 Z-score
        lgbm_features = preprocess_factors(lgbm_features, set())
        
        # 预测
        lgbm_pred = lgbm_model.predict(lgbm_features)
        lgbm_score = pd.Series(lgbm_pred, index=lgbm_features.index)
        
        # 合并 LGBM 分数
        base_score = base_score * (1 - W_LGBM / (1 + W_LGBM)) + lgbm_score * (W_LGBM / (1 + W_LGBM))
    
    return base_score, lgbm_score


def calc_dma(signal_df, fast=5, mid=35, slow=5):
    """DMA(快线，慢线，加速线) 择时"""
    print("  计算 DMA 择时...")
    
    # MA5 - MA35 = DMA
    dma = signal_df['close'].rolling(fast).mean() - signal_df['close'].rolling(slow).mean()
    
    # AMA = MA5(DMA)
    ama = dma.rolling(fast).mean()
    
    # 多头信号：DMA > AMA
    long_signal = dma > ama
    
    return dma, ama, long_signal


def run_backtest(start_date='20240102', end_date='20260501'):
    """执行回测"""
    print("=" * 60)
    print("Fusion20 + LGBM Top3-10d + DMA 确认回测")
    print("=" * 60)
    print(f"时间范围：{start_date} ~ {end_date}")
    print(f"持仓周期：{HOLD_DAYS}天 | 选股数量：{TOP_N}只 | LGBM 权重：{W_LGBM}x")
    print("-" * 60)
    
    start_time = time.time()
    
    # 1. 加载数据
    print("\n📂 加载数据...")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    
    lgbm_model, feature_map = load_lgbm_model()
    factor_df, stocks_df = load_fusion_factors(
        conn, start_date, end_date, FUSION_FACTOR_COLS, EXCLUDE_PREFIXES
    )
    
    # 获取收益率数据
    kline = pd.read_sql(f'''
        SELECT ts_code, trade_date, close, pct_chg
        FROM daily_kline
        WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
    ''', conn)
    kline = kline.sort_values(['ts_code', 'trade_date'])
    returns_df = kline.groupby('ts_code')['pct_chg'].shift(-HOLD_DAYS)
    
    conn.close()
    
    print(f"   因子数据：{factor_df.shape}")
    print(f"   收益率数据：{returns_df.shape}")
    
    # 2. 计算 IC_IR
    ic_series, ic_ir_series = calculate_ic_ir(factor_df, returns_df)
    
    # 3. 计算每日得分
    print("\n📊 计算每日得分...")
    all_scores = []
    
    for date in factor_df.index:
        if date not in factor_df.index:
            continue
            
        score_df = factor_df.loc[date].to_frame().T
        
        # 计算加权得分
        weighted_score, lgbm_scor = get_weighted_score(
            score_df, ic_ir_series, lgbm_model, feature_map
        )
        
        # DMA 过滤
        kline_day = kline[kline['trade_date'] == date]
        if len(kline_day) == 0:
            continue
            
        _, _, long_signal = calc_dma(kline_day)
        
        # 合并结果
        row = {
            'trade_date': date,
            'ts_code': score_df.index,
            'score': weighted_score.values,
            'lgbm_score': lgbm_scor.values if lgbm_scor is not None else None,
            'long_signal': long_signal.values if isinstance(long_signal, pd.Series) else None,
        }
        all_scores.append(row)
    
    scores_df = pd.DataFrame(all_scores)
    
    # 4. 选股并计算收益
    print("\n🎯 执行选股...")
    selected_stocks = []
    
    for date in scores_df['trade_date'].unique():
        day_data = scores_df[scores_df['trade_date'] == date].copy()
        
        # DMA 过滤
        if day_data['long_signal'].notna().all():
            day_data = day_data[day_data['long_signal'] == True]
        
        if len(day_data) == 0:
            continue
        
        # 按得分排序选 TopN
        top_n = min(TOP_N, len(day_data))
        top_stocks = day_data.nlargest(top_n, 'score')
        
        for _, stock in top_stocks.iterrows():
            selected_stocks.append({
                'trade_date': date,
                'ts_code': stock['ts_code'],
                'score': stock['score'],
                'hold_days': HOLD_DAYS,
            })
    
    selected_df = pd.DataFrame(selected_stocks)
    print(f"   选股记录：{len(selected_df)}条")
    
    # 5. 计算组合收益
    print("\n💰 计算组合收益...")
    
    # 构建价格查找表
    kline = kline.sort_values(['ts_code', 'trade_date'])
    kline['close_future'] = kline.groupby('ts_code')['close'].transform(
        lambda x: x.shift(-HOLD_DAYS)
    )
    
    # 合并价格
    selected_df = selected_df.merge(
        kline[['ts_code', 'trade_date', 'close', 'close_future']],
        on=['ts_code', 'trade_date'],
        how='left'
    )
    
    # 计算收益
    valid = (selected_df['close'] > 0) & (~selected_df['close_future'].isna())
    selected_df.loc[valid, 'ret'] = (selected_df.loc[valid, 'close_future'] / selected_df.loc[valid, 'close']) - 1
    selected_df.loc[~valid, 'ret'] = 0
    
    # 6. 汇总统计
    print("\n 汇总统计...")
    
    valid_rets = selected_df[selected_df['ret'].notna()]['ret']
    if len(valid_rets) == 0:
        print("️  无有效交易数据")
        return
    
    total_ret = (1 + valid_rets).prod() - 1
    win_rate = (valid_rets > 0).mean()
    avg_ret = valid_rets.mean()
    std_ret = valid_rets.std()
    sharpe = (avg_ret / std_ret * np.sqrt(252)) if std_ret > 0 else 0
    
    # 最大回撤
    cum_ret = (1 + valid_rets).cumprod()
    rolling_max = cum_ret.expanding().max()
    drawdown = (cum_ret - rolling_max) / rolling_max
    max_dd = drawdown.min()
    
    # 交易次数
    trade_dates = selected_df['trade_date'].nunique()
    period_years = trade_dates / 252
    
    print("-" * 60)
    print("📊 回测结果")
    print("-" * 60)
    print(f"总收益：{total_ret:.1%}")
    print(f"年化收益：{((1 + total_ret) ** (252 / trade_dates) - 1):.1%}" if trade_dates > 0 else "N/A")
    print(f"Sharpe: {sharpe:.2f}")
    print(f"最大回撤：{max_dd:.1%}")
    print(f"胜率：{win_rate:.1%}")
    print(f"交易次数：{len(valid_rets)}笔 ({trade_dates}个交易日)")
    print("-" * 60)
    
    # 7. 保存结果
    output_file = OUTPUT_DIR / f'fusion20_{HOLDDays}_top{TOP_N}_{datetime.now().strftime("%Y%m%d")}.json'
    result = {
        'strategy': 'Fusion20+LGBM Top3-10d+DMA',
        'params': {
            'hold_days': HOLD_DAYS,
            'top_n': TOP_N,
            'lgbm_weight': W_LGBM,
        },
        'results': {
            'total_return': f"{total_ret:.1%}",
            'annual_return': f"{((1 + total_ret) ** (252 / trade_dates) - 1):.1%}" if trade_dates > 0 else "N/A",
            'sharpe': f"{sharpe:.2f}",
            'max_drawdown': f"{max_dd:.1%}",
            'win_rate': f"{win_rate:.1%}",
            'trades': len(valid_rets),
            'trading_days': trade_dates,
        },
        'generated_at': datetime.now().isoformat(),
    }
    
    with open(output_file, 'w') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 结果已保存：{output_file}")
    print(f"⏱️  耗时：{time.time() - start_time:.1f}秒")
    print("\n✅ 回测完成!")


if __name__ == '__main__':
    # 从命令行参数获取日期
    if len(sys.argv) > 1:
        start_date = sys.argv[1]
        end_date = sys.argv[2] if len(sys.argv) > 2 else '20260501'
    else:
        start_date = '20240102'
        end_date = '20260501'
    
    run_backtest(start_date, end_date)
