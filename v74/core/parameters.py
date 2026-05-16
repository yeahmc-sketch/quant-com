from dataclasses import asdict, dataclass, replace
from typing import Dict


@dataclass(frozen=True)
class V74Parameters:
    # ── 入场：横盘吸筹判断 ──────────────────────────────────────
    # 近 20 天中至少 N 天成交量 < MA20量 × shrink_ratio（缩量横盘）
    prior_shrink_days_min: int = 8
    prior_shrink_ratio: float = 0.85       # 当日量 < MA20量 × 0.85 算缩量
    # 近 15 天价格幅度 < 此值（%），认为处于横盘区域
    consolidation_range_max: float = 20.0

    # ── 入场：放量突破判断 ──────────────────────────────────────
    breakout_volume_ratio: float = 2.0     # 今日量 >= MA20量 × 2.0
    # 今日收盘须突破近 N 天最高价
    breakout_lookback: int = 20
    # 实体占比：实体 / (最高-最低) >= 此值（过滤长上影假突破）
    candle_body_ratio_min: float = 0.55
    # 收盘价在日内位置（(close-low)/(high-low)）须 >= 此值（收在上半段）
    close_position_min: float = 0.60

    # ── 入场：均线结构 ──────────────────────────────────────────
    # MA5 与 MA20 差距不超过 N%（均线粘合蓄力中）
    ma5_ma20_gap_max: float = 4.0
    # MA20 近 5 天斜率 >= 此值（%）：允许 0，即平着也行（不要求已上翘）
    ma20_slope_5d_min: float = -0.5
    # 价格须在 MA20 上方
    price_must_above_ma20: bool = True

    # ── 入场：过滤条件 ──────────────────────────────────────────
    # 今日涨幅不超过 N%（防追高，但突破可以涨多一些）
    buy_pct_max: float = 9.5               # 允许接近涨停但不追涨停板
    # 近 30 天内无单日涨幅 > N% 的异常K（防止已被拉过）
    recent_spike_days: int = 30
    recent_spike_pct: float = 9.0
    # 流通市值范围（亿元，快照里暂无此字段，先预留）
    # float_mv_min: float = 10.0
    # float_mv_max: float = 200.0
    # 冷却期：同一只股产生信号后，N 天内不重复买入
    cooldown_days: int = 15

    # ── V74C：双阶段建仓参数 ───────────────────────────────────
    # 首突先建半仓，后续回踩确认再补仓
    stage1_position_ratio: float = 0.50
    stage2_position_ratio: float = 0.50

    # ── V74B / V74C：启动-回踩-再确认参数 ───────────────────────
    # 识别到突破后，最多观察 N 天等待缩量回踩
    pullback_scan_days: int = 8
    # 从突破收盘价回撤不超过 N% 仍视作健康回踩
    pullback_max_drawdown_pct: float = 3.5
    # 若突破后继续上冲超过 N%，则不再按"回踩确认"处理
    pullback_max_rise_pct: float = 2.0
    # 回踩日量比需低于此值，证明抛压不重
    pullback_volume_ratio_max: float = 0.85
    # 回踩后再次转强，需在 N 天内出现
    reconfirm_days_limit: int = 3
    # 再确认日涨幅下限
    reconfirm_pct_min: float = 1.5
    # 再确认日量比下限
    reconfirm_volume_ratio_min: float = 1.20
    # 再确认日收盘至少回到突破收盘价的此比例
    reconfirm_breakout_close_ratio: float = 0.995

    # ── 出场：硬止损（早期宽松期） ──────────────────────────────
    # 持仓前 early_hold_days 天只用硬止损，不做移动止盈
    early_hold_days: int = 5
    stop_loss: float = -0.07               # -7% 离场

    # ── 出场：ATR 追踪止盈 ──────────────────────────────────────
    # 触发移动止盈的最低盈利阈值
    trail_trigger: float = 0.12           # 涨够 12% 才开始保护
    # 从最高点回撤 ATR × 倍数 触发出场
    trail_atr_multiple: float = 2.5
    # ATR 估算：用近 14 天日收益率标准差 × 价格（简化版，无高低价时用此）
    atr_window: int = 14

    # ── 出场：量价背离（主力出货信号） ──────────────────────────
    # 价格创新高（比 N 天前高点还高），但今日量 < 前次高点日量 × ratio
    divergence_volume_ratio: float = 0.70
    divergence_lookback: int = 10         # 对比近 10 天高点
    # 触发背离出场所需最低持仓天数（防止买入当天就背离误判）
    divergence_min_hold_days: int = 5

    # ── 出场：高位长上影滞涨 ────────────────────────────────────
    # 连续 N 天出现长上影（上影 > 实体 × ratio）且已盈利 >= min_profit
    upper_shadow_days: int = 2
    upper_shadow_ratio: float = 1.5
    upper_shadow_min_profit: float = 0.15

    # ── 出场：时间止损 ───────────────────────────────────────────
    time_stop_days: int = 25
    time_stop_return_floor: float = 0.05   # 25天内没涨到 5% 就认为爆发落空，离场
    max_hold_days: int = 50

    # ── 市场过滤（宽松版，只过滤极端环境） ──────────────────────
    market_min_ready_count: int = 600
    market_breadth_min: float = 0.35       # 宽松：35% 股票在 MA20 上方就允许开仓
    market_slope_ratio_min: float = 0.30
    market_score_min: float = 0.35

    # ── 连续亏损降仓 ─────────────────────────────────────────────
    one_loss_position_scale: float = 0.90
    two_loss_position_scale: float = 0.75
    three_plus_loss_position_scale: float = 0.55

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


V74_CORE_PARAMETERS = V74Parameters()

V74_PULLBACK_CONFIRM_PARAMETERS = replace(
    V74_CORE_PARAMETERS,
    breakout_volume_ratio=2.2,
    buy_pct_max=8.5,
    market_breadth_min=0.45,
    market_slope_ratio_min=0.35,
    market_score_min=0.42,
    time_stop_days=20,
    time_stop_return_floor=0.03,
)

V74_DUAL_STAGE_PARAMETERS = replace(
    V74_CORE_PARAMETERS,
    breakout_volume_ratio=2.05,
    buy_pct_max=9.0,
    market_breadth_min=0.40,
    market_slope_ratio_min=0.32,
    market_score_min=0.37,
    pullback_scan_days=8,
    pullback_max_drawdown_pct=4.0,
    pullback_max_rise_pct=5.0,
    pullback_volume_ratio_max=0.90,
    reconfirm_days_limit=4,
    reconfirm_pct_min=1.0,
    reconfirm_volume_ratio_min=1.10,
    reconfirm_breakout_close_ratio=0.985,
)
