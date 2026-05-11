# DTK10 Strategy

**Dynamic Top-K strategy for A-share quantitative trading.**
Finalized: 2026-05-10

---

## Configuration

| Parameter | Value | Description |
|-----------|-------|-------------|
| Model | XGBoost 3.2+ (CUDA) | GPU-accelerated training |
| Factors | K31 (31 factors) | Optimized for 2024+ A-share market |
| Position Count | Dynamic TK 3-5 | Adjusted by 20-day NAV volatility percentile |
| Holding Period | HD=10 | 10 trading days |
| Risk Control | ConsecLoss CL2/3 | 2 consecutive losses → 50% position; 3 → empty |
| Entry Price | T+1 opening | Realistic A-share T+1 constraint |
| Exit Price | T-day closing | Close - slippage |
| Limit-Up Filter | pct_chg >= 9.0% | Skip stocks hitting limit-up |
| Limit-Down Delay | pct_chg <= -9.0% | Delay exit until tradeable |
| Costs | Buy 0.3%, Sell 0.35% | Commission + stamp + slippage |

## K31 Factors

```
avg_turnover_20, amihud_illiq_20d, neg_pb_cs, ep_cs,
overnight_ret, gap_up_ma_bias, holder_num_chg,
main_pct_5d, intraday_ret, jump_vol_ratio, main_pct_5d_sq,
surge_efficiency, skewness_20d, margin_balance_growth_5d,
vol_breakout, momentum_12m, amount_surge, volume_momentum,
momentum_3m, momentum_1m, downside_vol_20d, intraday_volatility_5,
margin_buy_ratio, follow_up, neg_debt_ratio, burst_pattern,
coil_amplitude, kurtosis_20d, netprofit_yoy, gross_margin, asset_turn,
sector_crowdedness
```

## K32 (31 + sector_crowdedness)

Added 2026-05-11: `sector_crowdedness` (IC=-0.029, ICIR=-0.25, max|r|<0.1 vs K31)
- Measures 10-day/60-day industry volume ratio
- Negative signal: crowded sectors underperform (A-share "见光死")
- MaxDD improvement: -24.2% → -19.7% (+4.5pp) without hurting CAGR

## Performance (2024-01 to 2026-03)

### K31 vs K32 Comparison
| Metric | K31 (31) | K32 (+crowdedness) |
|--------|----------|-------------------|
| CAGR | 70.7% | 69.5% |
| Sharpe | 1.66 | 1.67 |
| MaxDD | -24.2% | **-19.7%** |
| 2025 | +102% | +91.6% |

### Original DTK10 (pre-computed scores, K31)
| Metric | Value |
```

Minimum hold: 10 days between TK changes (prevents oscillation).

**Intuition**: In A-shares, high volatility often precedes corrections. Reducing position count during volatile periods cuts drawdown.

## Performance (2024-01 to 2026-03)

| Metric | Value |
|--------|-------|
| Initial Capital | ¥50,000 |
| Final Value | ¥593,437 |
| CAGR | 215.0% |
| Sharpe (non-overlap) | 1.85 |
| Max Drawdown | -16.7% |

## Files

| File | Purpose |
|------|---------|
| `dtk10_backtest.py` | Main backtest: XGBoost + pre-computed scores |
| `dtk10_walk_forward.py` | Walk-Forward comparison: same config, rolling training |
| `dtk10_strategy.md` | This document |

## Maintenance

⚠️ **Monthly Retraining Required:**
Pre-computed scores should be regenerated every 3 months with the latest data to prevent signal decay.
Use `dtk10_backtest.py` with updated `grid_scores/` data.

## Key Design Decisions (2026-05-10)

- **Walk-Forward rejected**: Rolling 24-month training windows produced lower Sharpe (1.66 vs 1.85) and worse MaxDD (-24.2% vs -16.7%) due to insufficient training data.
- **Elite 38 factors rejected**: Adding 7 extra factors (adx_14, pv_corr_20, no_zt_5, volume_divergence_5, max_return_5d, volume_weighted_momentum, turnover_rate) slightly improved 2024 CAGR (49% vs 32%) but degraded MaxDD.
- **Fixed TK=5 baseline**: CAGR 121.8%, Sharpe 1.44 — dynamic TK adds 93pp CAGR and 0.41 Sharpe via volatility-adaptive position sizing.
