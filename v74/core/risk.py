"""
V74 风控模块

出场优先级（从高到低）：
1. 量价背离（主力出货信号）    → 当日收盘清
2. 高位长上影滞涨              → 当日收盘清
3. ATR 追踪止盈（盈利 >= 12%）→ 当日触发清
4. 硬止损（-7%）              → 当日触发清
5. 时间止损（25天未盈利 5%）   → 次日清
6. 最大持有天数（50天）        → 次日清

市场过滤（极端环境下暂停开仓）：
- 宽松版：MA20 上方占比 >= 35%，斜率正向 >= 30%
"""
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Dict, Iterable, Optional

from v74.core.parameters import V74Parameters
from v74.core.portfolio import Position


@dataclass
class MarketContext:
    trade_date: str
    ready_count: int
    breadth_above_ma20: float
    breadth_positive_slope: float
    average_pct_chg: float
    score: float
    is_risk_on: bool

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ExitCheck:
    reason: Optional[str]
    current_return: float
    peak_return: float
    peak_drawdown: float


def build_market_context(
    trade_date: str,
    feature_records: Iterable[Dict[str, float]],
    params: V74Parameters,
) -> MarketContext:
    records = list(feature_records)
    if not records:
        return MarketContext(
            trade_date=trade_date,
            ready_count=0,
            breadth_above_ma20=0.0,
            breadth_positive_slope=0.0,
            average_pct_chg=0.0,
            score=0.0,
            is_risk_on=False,
        )

    ready_count = len(records)
    breadth_above_ma20 = sum(1 for r in records if r["price_vs_ma20"] >= 0.0) / ready_count
    breadth_positive_slope = sum(1 for r in records if r["ma20_slope_10d"] > 0.0) / ready_count
    average_pct_chg = mean(r["pct_chg"] for r in records)
    pct_component = max(0.0, min(1.0, (average_pct_chg + 1.5) / 3.0))
    score = breadth_above_ma20 * 0.45 + breadth_positive_slope * 0.35 + pct_component * 0.20

    is_risk_on = (
        ready_count >= params.market_min_ready_count
        and breadth_above_ma20 >= params.market_breadth_min
        and breadth_positive_slope >= params.market_slope_ratio_min
        and score >= params.market_score_min
    )
    return MarketContext(
        trade_date=trade_date,
        ready_count=ready_count,
        breadth_above_ma20=round(breadth_above_ma20, 4),
        breadth_positive_slope=round(breadth_positive_slope, 4),
        average_pct_chg=round(average_pct_chg, 4),
        score=round(score, 4),
        is_risk_on=is_risk_on,
    )


def position_scale_for_loss_streak(loss_streak: int, params: V74Parameters) -> float:
    if loss_streak <= 0:
        return 1.0
    if loss_streak == 1:
        return params.one_loss_position_scale
    if loss_streak == 2:
        return params.two_loss_position_scale
    return params.three_plus_loss_position_scale


def evaluate_exit(
    position: Position,
    high_price: float,
    close_price: float,
    params: V74Parameters,
    features: Optional[Dict[str, float]] = None,
) -> ExitCheck:
    """
    V74 出场评估，核心改动：
    - ATR 追踪止盈（而非固定 2% 回撤）
    - 量价背离检测
    - 高位长上影检测
    - 取消"盈亏平衡保护"（早期宽松持有）
    """
    peak_price = max(position.peak_price, high_price) if high_price > 0 else position.peak_price
    current_return = close_price / position.entry_price - 1.0 if position.entry_price else 0.0
    peak_return = peak_price / position.entry_price - 1.0 if position.entry_price and peak_price else 0.0
    peak_drawdown = close_price / peak_price - 1.0 if peak_price else 0.0

    # ── 1. 硬止损（最优先，早期宽松期也要止损） ────────────────
    if current_return <= params.stop_loss:
        return ExitCheck("stop_loss", current_return, peak_return, peak_drawdown)

    # ── 2. 量价背离（主力出货，持仓 >= divergence_min_hold_days）──
    if features and position.hold_days >= params.divergence_min_hold_days:
        # 今日价格创新高（超过近 10 天峰值），但量能萎缩
        peak_close_10d = features.get("peak_close_10d", 0.0)
        peak_vol_10d = features.get("peak_vol_10d", 0.0)
        vol_ma20 = features.get("vol_ma20", 1.0)
        today_vol = features.get("vol_ratio", 1.0) * vol_ma20
        if (
            close_price > peak_close_10d * 1.001     # 今日价格确实创近 10 天新高
            and peak_vol_10d > 0
            and today_vol < peak_vol_10d * params.divergence_volume_ratio  # 量能缩
            and current_return >= 0.10                # 且已有 10% 以上利润（高位才触发）
        ):
            return ExitCheck("volume_divergence", current_return, peak_return, peak_drawdown)

    # ── 3. 高位长上影滞涨 ────────────────────────────────────────
    if features and position.hold_days >= params.upper_shadow_days:
        upper_shadow = features.get("upper_shadow", 0.0)
        body_len = features.get("body_len", 1.0)
        if (
            body_len > 0
            and upper_shadow > body_len * params.upper_shadow_ratio
            and current_return >= params.upper_shadow_min_profit
        ):
            # 用一个计数器需要有状态——简化：连续 2 天皆长上影才触发
            # 回测中我们看前一天特征，feature 里没有历史形态信息
            # 保守处理：1 天长上影 + 足够盈利就触发（避免状态复杂化）
            return ExitCheck("upper_shadow_stall", current_return, peak_return, peak_drawdown)

    # ── 4. ATR 追踪止盈（盈利 >= trail_trigger 后才激活） ───────
    if peak_return >= params.trail_trigger:
        atr = features.get("atr", close_price * 0.03) if features else close_price * 0.03
        # 从峰值回撤超过 atr_multiple × ATR 则出场
        trail_threshold = -(params.trail_atr_multiple * atr / peak_price) if peak_price > 0 else -0.08
        if peak_drawdown <= trail_threshold:
            return ExitCheck("trailing_stop_atr", current_return, peak_return, peak_drawdown)

    # ── 5. 时间止损 ──────────────────────────────────────────────
    if position.hold_days >= params.time_stop_days and current_return <= params.time_stop_return_floor:
        return ExitCheck("time_stop", current_return, peak_return, peak_drawdown)

    # ── 6. 最大持有天数 ──────────────────────────────────────────
    if position.hold_days >= params.max_hold_days:
        return ExitCheck("max_hold_days", current_return, peak_return, peak_drawdown)

    return ExitCheck(None, current_return, peak_return, peak_drawdown)
