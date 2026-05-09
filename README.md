# A股量化选股策略 — Elite LGBM Walk-Forward

## 概述

基于34个精英因子的LightGBM多因子选股模型，Walk-Forward严格时序验证，真实交易约束回测。

**当前最优**: 单LGBM(seed=42) + Top2 + 5日持有 + ConsecLoss风控

## 回测基准结果

**回测区间**: 2020-12-30 ~ 2026-03-17 (5.2年，1260交易日)
**初始本金**: ¥50,000

### 真实交易版本

| 指标 | 数值 |
|------|------|
| CAGR | **65.3%** |
| Sharpe | **3.89** |
| MaxDD | **-10.8%** |
| WinRate | 57.6% |
| 最终净值 | **¥628,892** (12.6x) |

### 交易约束
- **买入价**: T+1日开盘价
- **卖出价**: 到期日收盘价（跌停则延期至次日）
- **涨停过滤**: pct_chg ≥ 9.0 无法买入，退选次优
- **跌停延期**: 到期日跌停则持仓延期
- **成本**: 买入0.30%（佣金0.10%+滑点0.20%），卖出0.35%（+印花税0.05%）
- **风控**: ConsecLoss（连续2亏→半仓，4亏→空仓）

## 34个精英因子

```
# 动量/反转
momentum_1m, momentum_3m, momentum_12m, overnight_ret, intraday_ret

# 波动/风险
intraday_volatility_5, downside_vol_20d, jump_vol_ratio,
skewness_20d, kurtosis_20d

# 量价技术
adx_14, pv_corr_20, coil_amplitude, volume_momentum,
vol_breakout, avg_turnover_20, amihud_illiq_20d

# 资金流
main_pct_5d, main_pct_5d_sq, amount_surge

# 爆发力
gap_up_ma_bias, follow_up, surge_efficiency, burst_pattern

# 融资融券
margin_buy_ratio, margin_balance_growth_5d

# 财务
netprofit_yoy, neg_debt_ratio, gross_margin, asset_turn,
neg_pb_cs, ep_cs

# 其他
no_zt_5, holder_num_chg
```

## 模型架构

### LGBM参数（激进正则化）
```
max_depth=5, num_leaves=23, min_data_in_leaf=500
learning_rate=0.02, n_estimators=500
feature_fraction=0.6, bagging_fraction=0.7, bagging_freq=3
lambda_l1=5.0, lambda_l2=5.0
min_gain_to_split=1.0
early_stopping=20
random_state=42
```

### Walk-Forward框架
- **训练窗口**: 480交易日 (~2年)
- **Embargo**: 5交易日（训练集与验证集隔离）
- **验证窗口**: 60交易日 (~3个月)
- **步进**: 60交易日
- **总窗口数**: 21
- **目标变量**: 5日 forward return 横截面百分位排名

### 过拟合防护
1. Walk-Forward + 5日 Embargo（无前视偏差）
2. 激进正则化（min_data=500, L1/L2=5.0, max_depth=5）
3. Early Stopping 20轮
4. 双种子交叉验证（seed=42/123）
5. 真实交易约束（T+1开盘、涨跌停、交易成本）

## 数据

### 因子来源
- `fusion20_master.parquet`: 530万行，121+因子列，覆盖2019-2026年A股全部股票
- 因子类别：量价技术、资金流、财务、融资融券、Alpha101、股东人数变化

### 数据字段
- ts_code, trade_date: 股票代码和交易日
- close, open, pct_chg: 价格数据
- 34个精英因子 + 目标变量 (_target, _fwd_ret)

## 环境

### Windows
- Python 3.13.12
- pyarrow, pandas, numpy, scipy, lightgbm

### WSL GPU (训练)
- Ubuntu 22.04, CUDA 12.9, RTX 5060 Ti 16GB
- Python 3.10.12, LightGBM 4.6.0 CUDA版
- device='cuda'（不是 'gpu'）

## 网格搜索结论 (2026-05-09)

| 参数 | 最优值 | 结论 |
|------|--------|------|
| TopK | 2 | Top2 > Top3 > Top5 |
| 持有天数 | 5 | H5 >> H10 |
| 双模集成 | 否 | 单模优于双模（A股LGBM高度相关）|
| 交易价格 | T+1开盘 | 比T日收盘CAGR低~21pp |
| 涨跌停 | 必修 | 真实交易损耗~6pp |

## 回测脚本

| 脚本 | 用途 |
|------|------|
| `bt_embargo_real.py` | 完整回测：GPU训练+真实交易约束 |
| `bt_grid_open.py` | 网格搜索：12种参数组合 |
| `bt_grid.py` | 基础网格（收盘价版本）|

## 文件清单

```
A股量化策略/
├── README.md                    # 本文档
├── STRATEGY.md                  # 策略规格说明书
├── scripts/
│   ├── bt_embargo_real.py      # 核心回测脚本
│   ├── bt_grid_open.py         # 网格搜索脚本
│   └── factor_list.py          # 因子列表
├── results/
│   ├── bt_grid_open_result.json # T+1开盘价回测结果
│   ├── bt_grid_result.json     # 收盘价网格结果
│   └── bt_embargo_real_result.json # 真实交易单组结果
└── data/
    └── README.md               # 数据说明
```
