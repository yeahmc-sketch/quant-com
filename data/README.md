# 数据说明

## 主数据文件

**fusion20_master.parquet** (≈600MB)
- 路径: `C:\ML_STATION\LGBM_ML_Package\data\fusion20_master.parquet`
- 行数: ~5,294,078
- 覆盖: 2019-01-02 ~ 2026-04-23
- 股票数: ~5,000 只A股

### 核心列
| 列名 | 类型 | 说明 |
|------|------|------|
| ts_code | str | 股票代码 |
| trade_date | str | 交易日 YYYYMMDD |
| close | float | 收盘价 |
| open | float | 开盘价 |
| pct_chg | float | 涨跌幅(%) |
| _target | float | 横截面排名化目标(rank_pct) |
| _fwd_ret | float | 原始5日forward return |

### 因子列 (121+)
详见 `factor_list.py` 中34个精英因子的完整说明。

## 训练中间文件

**bt_elite_preproc.parquet** (~700MB)
- 路径: `C:\ML_STATION\LGBM_ML_Package\data\bt_elite_preproc.parquet`
- 包含: ts_code, trade_date, _target, _fwd_ret + 34个精英因子

**bt_scores/scores_*.parquet** (~4MB each × 21)
- 路径: `C:\Users\Administrator\WorkBuddy\Claw\bt_scores\`
- 每个窗口的验证集上模型预测分数

## 数据来源
- A股日线OHLCV: Tushare Pro + market.db
- 财务数据: fina_indicator
- 融资融券: margin_detail
- 股东人数: holder_number
