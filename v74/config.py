from dataclasses import dataclass
from pathlib import Path

# 复用 v73 的数据层，不重复存储数据
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = WORKSPACE_ROOT / "data"
METADATA_DIR = DATA_ROOT / "metadata"
RAW_SNAPSHOT_DIR = DATA_ROOT / "raw" / "snapshots"
OUTPUT_DIR = WORKSPACE_ROOT / "output" / "v74"
REPORT_DIR = OUTPUT_DIR / "reports"
PAPER_DIR = OUTPUT_DIR / "paper"

DEFAULT_START_DATE = "20150101"
DEFAULT_END_DATE = "20251231"


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 20000.0
    max_positions: int = 3
    # 爆发策略：每只仓位固定 20%，最多同时持 3 只
    normal_target_weight: float = 0.20
    strong_target_weight: float = 0.25
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.001
    transfer_fee_rate: float = 0.00005  # 过户费万0.5（2022年9月后标准）
    max_new_positions_per_day: int = 1   # 每天最多买 1 只，避免同日扎堆
    enable_progress_log: bool = False
    progress_every_days: int = 20


for _path in [OUTPUT_DIR, REPORT_DIR, PAPER_DIR]:
    _path.mkdir(parents=True, exist_ok=True)
