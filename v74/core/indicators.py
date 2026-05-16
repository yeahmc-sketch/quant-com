"""
V74 滚动指标状态机

相比 V73 的 RollingState，新增：
- 成交量历史（用于放量检测、量价背离）
- 高低价历史（用于突破检测、ATR 估算、上影线检测）
- ATR（简化版：近 14 日 True Range 均值）
"""
from collections import deque
from dataclasses import dataclass, field
from statistics import mean, pstdev
from typing import Deque, Dict, Optional


@dataclass
class V74RollingState:
    # 价格序列（最近 31 天，覆盖 MA20 + 斜率计算）
    closes_adj: Deque[float] = field(default_factory=lambda: deque(maxlen=32))
    highs_adj: Deque[float] = field(default_factory=lambda: deque(maxlen=32))
    lows_adj: Deque[float] = field(default_factory=lambda: deque(maxlen=32))
    # 成交量序列（近 25 天，用于 MA20 量和缩量统计）
    volumes: Deque[float] = field(default_factory=lambda: deque(maxlen=32))
    # MA20 历史（用于计算斜率）
    ma20_history: Deque[float] = field(default_factory=lambda: deque(maxlen=11))
    # 连续上涨天数
    prev_close_adj: Optional[float] = None
    # 冷却计时
    days_since_last_signal: int = 10_000
    # 近 30 天最大单日涨幅（用于过滤"已被拉过"）
    recent_max_pct: float = 0.0
    pct_history: Deque[float] = field(default_factory=lambda: deque(maxlen=30))

    def update(
        self,
        close_adj: float,
        high_adj: float,
        low_adj: float,
        volume: float,
        pct_chg: float,
    ) -> None:
        self.closes_adj.append(close_adj)
        self.highs_adj.append(high_adj)
        self.lows_adj.append(low_adj)
        self.volumes.append(volume if volume > 0 else 0.0)
        self.pct_history.append(abs(pct_chg))
        self.recent_max_pct = max(self.pct_history) if self.pct_history else 0.0

        if len(self.closes_adj) >= 20:
            self.ma20_history.append(mean(list(self.closes_adj)[-20:]))

        self.prev_close_adj = close_adj
        self.days_since_last_signal += 1

    @property
    def ready(self) -> bool:
        return (
            len(self.closes_adj) >= 22
            and len(self.volumes) >= 21
            and len(self.ma20_history) >= 6
        )

    def mark_signal(self) -> None:
        self.days_since_last_signal = 0

    def snapshot(
        self,
        close_adj: float,
        high_adj: float,
        low_adj: float,
        volume: float,
        open_adj: float,
    ) -> Optional[Dict[str, float]]:
        if not self.ready:
            return None

        closes = list(self.closes_adj)
        highs = list(self.highs_adj)
        lows = list(self.lows_adj)
        vols = list(self.volumes)

        ma5 = mean(closes[-5:])
        ma20 = self.ma20_history[-1]
        ma20_5d_ago = self.ma20_history[-6]

        # 均线指标
        ma20_slope_5d = (ma20 - ma20_5d_ago) / ma20_5d_ago * 100 if ma20_5d_ago > 0 else 0.0
        price_vs_ma20 = (close_adj - ma20) / ma20 * 100 if ma20 > 0 else 0.0
        ma5_ma20_gap = (ma5 - ma20) / ma20 * 100 if ma20 > 0 else 0.0

        # 成交量指标
        vol_ma20 = mean(vols[-20:]) if len(vols) >= 20 else mean(vols)
        vol_ratio = volume / vol_ma20 if vol_ma20 > 0 else 0.0

        # 近 20 天中缩量天数（量 < vol_ma20 × 0.85）
        prior_vols = vols[-21:-1]  # 不含今天
        shrink_days = sum(1 for v in prior_vols if v < vol_ma20 * 0.85)

        # 近 15 天价格幅度（横盘判断）
        recent_highs_15 = highs[-16:-1]
        recent_lows_15 = lows[-16:-1]
        if recent_highs_15 and recent_lows_15:
            range_base = mean(recent_lows_15)
            consolidation_range = (
                (max(recent_highs_15) - min(recent_lows_15)) / range_base * 100
                if range_base > 0 else 999.0
            )
        else:
            consolidation_range = 999.0

        # 近 20 天最高价（价格突破判断）
        lookback_highs = highs[-21:-1]
        high_20d = max(lookback_highs) if lookback_highs else close_adj

        # 蜡烛实体占比
        candle_range = high_adj - low_adj
        candle_body = abs(close_adj - open_adj)
        body_ratio = candle_body / candle_range if candle_range > 0 else 0.0

        # 收盘在日内位置（高位收盘）
        close_position = (
            (close_adj - low_adj) / candle_range if candle_range > 0 else 0.5
        )

        # ATR（简化版：近 14 天每日 high-low 均值）
        tr_list = [h - l for h, l in zip(highs[-14:], lows[-14:]) if h > l]
        atr = mean(tr_list) if tr_list else close_adj * 0.03

        # 上影线长度（用于高位滞涨判断）
        upper_shadow = high_adj - max(close_adj, open_adj)
        body_len = abs(close_adj - open_adj)

        # 近 10 天峰值信息（量价背离）
        recent_closes_10 = closes[-11:-1]
        recent_vols_10 = vols[-11:-1]
        peak_close_10d = max(recent_closes_10) if recent_closes_10 else close_adj
        if recent_closes_10 and peak_close_10d > 0:
            peak_idx = recent_closes_10.index(peak_close_10d)
            peak_vol_10d = recent_vols_10[peak_idx] if peak_idx < len(recent_vols_10) else vol_ma20
        else:
            peak_vol_10d = vol_ma20

        # 近 20 天MA20斜率（市场宽度用，沿用 v73 方式）
        ma20_slope_10d = 0.0
        if len(self.ma20_history) >= 11:
            ma20_10d_ago = self.ma20_history[0]
            ma20_slope_10d = (ma20 - ma20_10d_ago) / ma20_10d_ago * 100 if ma20_10d_ago > 0 else 0.0

        return {
            # 均线
            "ma5": ma5,
            "ma20": ma20,
            "ma20_slope_5d": round(ma20_slope_5d, 3),
            "ma20_slope_10d": round(ma20_slope_10d, 3),   # 供市场宽度计算
            "price_vs_ma20": round(price_vs_ma20, 3),
            "ma5_ma20_gap": round(ma5_ma20_gap, 3),
            # 量能
            "vol_ma20": round(vol_ma20, 1),
            "vol_ratio": round(vol_ratio, 3),              # 今日量 / MA20量
            "shrink_days": float(shrink_days),             # 近 20 天缩量天数
            # 突破
            "consolidation_range": round(consolidation_range, 2),  # 近 15 天价格幅度%
            "high_20d": round(high_20d, 4),                # 近 20 天最高价
            # 蜡烛形态
            "body_ratio": round(body_ratio, 3),            # 实体占比
            "close_position": round(close_position, 3),   # 收盘在日内位置
            # ATR
            "atr": round(atr, 4),
            # 上影线
            "upper_shadow": round(upper_shadow, 4),
            "body_len": round(body_len, 4),
            # 量价背离
            "peak_close_10d": round(peak_close_10d, 4),
            "peak_vol_10d": round(peak_vol_10d, 1),
            # 近期最大涨幅（过滤"已被拉过"）
            "recent_max_pct": round(self.recent_max_pct, 2),
            # 冷却
            "days_since_last_signal": float(self.days_since_last_signal),
        }
