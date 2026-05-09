# DTK10：波动率驱动动态仓位（Dynamic TK v2）

> **策略代号：DTK10**（Dynamic TK, HD=10）
> **状态：定版（Final）** — 供外部 AI 审查使用
> **仓库：** yeahmc-sketch/quant-com
> **核心结论：** CAGR=215.0%，Sharpe=1.85，MaxDD=-16.7%（¥50k → ¥593k，2.1年）

---

## 一、策略概要

| 项目 | 内容 |
|------|--------|
| 策略类型 | A股量化选股，Top-K持仓，持有固定天数 |
| 选股依据 | LGBM/XGBoost 模型预计算评分（`grid_scores/*.parquet`） |
| 仓位管理 | 动态 TK（持仓数量），由市场波动率驱动 |
| 基准对比 | Fixed TK=5（CAGR=121.8%，Sharpe=1.44） |
| 净值得益 | ¥50k → ¥593k（**11.9x**，2.1年） |

---

## 二、策略逻辑

### 2.1 主循环（逐日）

```
for each trading day:
    1. 卖出：持仓到期 or 跌停无法卖出（顺延）
    2. 计算当日市值（mark-to-market）
    3. 动态TK调整（见2.3）
    4. CL2/3风控（见2.4）
    5. 买入：按评分选Top-K，T+1开盘价成交
```

### 2.2 选股与交易

| 参数 | 值 | 说明 |
|------|-----|------|
| TK（持仓数量） | 动态 3/4/5 | 由波动率分位数决定 |
| HD（持有天数） | 10 | 买入后持有10交易日 |
| 选股 | Top-K（按score降序） | 从`grid_scores/*.parquet`读取 |
| 买入价 | T+1 开盘价 × (1+滑点) | 跌停买不进 |
| 卖出价 | 持有到期日收盘价 × (1-滑点) | 涨停卖不出 |
| 交易成本 | 买入0.3% / 卖出0.35% | 佣金+印花税+滑点 |

### 2.3 动态TK逻辑（核心创新）

每天根据**历史净值波动率**调整TK：

```
if di >= 20（vol_window）:
    recent_ret = log(nav[t-19]/nav[t-20]), ..., log(nav[t]/nav[t-1])
    vol = std(recent_ret)
    
    # 计算vol在历史所有vol中的分位数
    all_vol = [std(ret_j) for j in range(20, t)]
    pct = percentile(vol, all_vol)   # 0~1
    
    if pct > 0.5:   TK = 3   # 高波动 → 降仓
    elif pct > 0.3: TK = 4
    else:          TK = 5   # 低波动 → 满仓
```

**关键发现：A股高波动是回调前兆，提前降仓是保护而非缺陷。**

### 2.4 CL2/3 风控

| 条件 | 动作 |
|------|------|
| 当日净值/昨日净值 - 1 < -0.1% | 连续微亏计数 cn +1 |
| cn >= 2 | 仓位减半（sc_val=0.5） |
| cn >= 3 | 清仓（sc_val=0.0） |
| 否则 | sc_val=1.0（正常） |

---

## 三、回测结果

### 3.1 核心指标对比

| 策略 | CAGR | Sharpe(NO) | MaxDD | 最终净值 |
|------|-------|-------------|--------|----------|
| Fixed TK=5（基线） | 121.8% | 1.44 | -16.7% | ¥278,551 |
| **v2（Pctl=0.5）** 🏆 | **215.0%** | **1.85** | -16.7% | **¥593,437** |
| v2（Pctl=0.6） | 157.6% | - | - | - |
| v3（dd>-5%才降仓） | 135.2% | 1.34 | -17.1% | ¥315,981 |

> Sharpe(NO) = 非重叠采样（每5天取1个独立样本），避免自相关虚高

### 3.2 月度详细数据

见 `results/dynamic_tk_v2_percentile_monthly.csv`

关键观察：
- 2024Q4（强市）：TK降为3~4，规避了部分回调
- MaxDD发生在2024年（策略初期），之后回撤控制在-5%以内

---

## 四、数据来源

| 数据 | 路径 | 说明 |
|------|------|------|
| 评分数据 | `grid_scores/*.parquet` | LGBM模型输出，每行：`ts_code, trade_date, score` |
| 价格数据 | `fusion20_master.parquet` | 日线：open/close/pct_chg |
| 因子数据 | `fusion20_all_factors.parquet` | 121个因子（用于生成评分） |

> **⚠️ 数据文件未上传GitHub**（超过100MB限制）
> 审查者如需复现，请联系作者获取，或用自己的数据源替换

---

## 五、模型参数

### 5.1 LGBM/XGBoost（评分模型）

| 参数 | 值 |
|------|-----|
| 模型 | LGBM 4.6.0 / XGBoost 3.2.0 |
| device | CUDA（RTX 5060 Ti 16GB） |
| num_iterations | 500 |
| learning_rate | 0.05 |
| max_depth | 5 |
| min_data_in_leaf | 500 |
| lambda_l1 | 5.0 |
| lambda_l2 | 5.0 |
| early_stopping | 20 rounds |

### 5.2 回测参数

| 参数 | 值 |
|------|-----|
| 本金 | ¥50,000 |
| TK（基线） | 5 |
| HD（持有天数） | 10 |
| vol_window | 20 |
| vol_pctl_threshold | 0.5 |
| min_hold_days | 10 |
| 买入成本 | 0.3%（佣金0.025% + 滑点0.275%） |
| 卖出成本 | 0.35%（佣金0.025% + 印花税0.1% + 滑点0.225%） |

---

## 六、潜在过拟合风险（供审查）

### 🔴 高风险

1. **Walk-Forward未使用**
   - 当前回测使用简单滚动窗口，未做Walk-Forward验证
   - 建议：用24月训练+3月验证的Walk-Forward重新跑

2. **波动率分位数阈值（0.5）未做参数扫描**
   - 当前0.5是最优吗？扫描0.3~0.8确认
   - 参数太少，过拟合风险较低，但仍需验证

### 🟡 中风险

3. **评分数据可能包含未来信息**
   - `grid_scores/*.parquet` 的生成逻辑需要审查
   - 确认没有使用当日close计算评分（forward-looking bias）

4. **2023年后因子IC衰减**
   - 训练数据包含2023-2024年，但因子IC在2023年后显著衰减
   - 策略在2023年后的表现需要单独验证

### 🟢 低风险

5. **交易成本假设**
   - 当前成本假设（0.3%/0.35%）偏乐观
   - 实际高频交易的成本可能更高

6. **跌停/涨停过滤**
   - 当前逻辑：跌停卖不出（顺延），涨停买不进
   - 这是合理的真实交易约束

---

## 七、复现步骤

```bash
# 1. 准备数据
#    - 将 grid_scores/*.parquet 放到 ./grid_scores/
#    - 将 fusion20_master.parquet 放到 C:/ML_STATION/LGBM_ML_Package/data/

# 2. 安装依赖
pip install pandas numpy pyarrow xgboost scipy

# 3. 运行回测
python scripts/dynamic_tk_v2.py

# 4. 输出
#    - 日志：dynamic_tk_v2_HHMMSS.log
#    - 月度数据：dynamic_tk_v2_Dynamic_TK_v2_percentile_monthly.csv
```

---

## 八、待审查问题清单

请外部AI重点审查以下问题：

- [ ] 评分生成逻辑是否有前视偏差（forward-looking bias）？
- [ ] 波动率计算是否正确（用log return？用净值还是价格？）？
- [ ] 动态TK逻辑是否有过拟合风险（分位数阈值0.5）？
- [ ] Sharpe计算是否正确（非重叠采样）？
- [ ] 2023年后因子IC衰减是否影响策略有效性？
- [ ] 跌停/涨停过滤逻辑是否完整？

---

## 九、文件清单

| 文件 | 说明 |
|------|------|
| `scripts/dynamic_tk_v2.py` | v2定版策略代码 |
| `results/dynamic_tk_v2_percentile_monthly.csv` | 月度回测结果 |
| `docs/strategy_v2.md` | 本文件 |
| `data/README.md` | 数据schema说明 |
| `factor_list.py` | 因子列表（35个核心因子） |

---

*最后更新：2026-05-09*
