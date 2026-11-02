"""全局配置：户型定义、数据基准、路径。

为什么户型面积不自己拍：
深圳市住建局《房屋租赁参考价格》对城中村私宅明确规定了"标准户型"建筑面积
（单间 19㎡、一房一厅 35㎡、两房 60㎡、三房及以上 85㎡）。
小区数据是按 元/㎡·月 给的，所以面积是我们乘上去的假设值。

这里刻意让"小区乘的面积"和"城中村标准户型面积"**保持一致**，
否则同一个"二房一厅"在两类房源之间就不是同一个东西，比出来的性价比没有意义。

复式是唯一两类数据口径不一致的：小区按面积可以算，城中村没有这个类目
（深圳农民房基本没有复式）。所以复式标记为"仅小区"。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "rentwise.db"

COMMUNITIES_CSV = RAW_DIR / "communities.csv"
VILLAGES_CSV = RAW_DIR / "villages.csv"

# 数据来源与基准（写进数据库 meta 表，界面上要展示）
SOURCE_NAME = "深圳市住房和建设局 · 深圳市房屋租赁参考价格查询平台"
SOURCE_URL = "https://zjj.sz.gov.cn/fwzljgcx/"
BASELINE_YEAR = 2022
CITY = "深圳"

# 价格异常的合理区间（元/㎡·月），用于导入时的数据体检
RATE_MIN, RATE_MAX = 10.0, 400.0


@dataclass(frozen=True, slots=True)
class Layout:
    """一种户型。

    village_field: 城中村数据里对应的字段名；None 表示城中村没有这个类目。
    area_source: 面积假设的来源，必须诚实标注：
        measured  = 用真实挂牌房源实测出来的中位数
        policy    = 政府公布的城中村标准户型面积
        estimated = 没有数据，按经验估的（可被后续样本替换）
    """

    code: str
    label: str
    area_sqm: float
    village_field: str | None
    area_source: str
    note: str = ""

    @property
    def supported_by_village(self) -> bool:
        return self.village_field is not None


LAYOUTS: tuple[Layout, ...] = (
    Layout("studio", "单间", 19.0, "rent_single", "policy",
           "城中村标准户型 19㎡；小区小户型房源少，暂用同一面积"),
    # 35㎡ 是城中村政策户型，不是商品房的实际面积。
    # 实测 16 条住宅挂牌房源，一房一厅中位 42.9㎡，所以这里用 43㎡。
    Layout("one_bed", "一房一厅", 43.0, "rent_one", "measured",
           "实测住宅挂牌中位 42.9㎡（n=16）"),
    Layout("two_bed", "二房一厅", 60.0, "rent_two", "estimated",
           "待补样本，暂用政府标准户型 60㎡"),
    Layout("three_bed", "三房及以上", 85.0, "rent_multi_room", "estimated",
           "待补样本，暂用政府标准户型 85㎡"),
    Layout("duplex", "复式", 80.0, None, "estimated",
           "城中村无此户型，仅小区可选"),
)

LAYOUT_BY_CODE: dict[str, Layout] = {item.code: item for item in LAYOUTS}
DEFAULT_LAYOUT = "one_bed"

# 政府参考价 vs 真实挂牌价的整体修正系数。
# 实测：16 条住宅类挂牌房源的「挂牌单价 / 政府参考单价」中位数为 1.045，
# 即政府参考价整体比市场低约 4.5%。只对住宅类成立；
# 商办公寓的比值中位只有 0.791，不适用（会被单独排除）。
MARKET_ADJUST = 1.045
MARKET_ADJUST_NOTE = "实测住宅类挂牌单价/政府参考单价 中位 1.045（n=16）"

# 物业类型：按名称规则分类。这是**规则推断**，不是政府标注。
# 之所以必须分开：住宅类 Spearman 0.943，商办公寓只有 0.252，混在一起会互相污染。
COMMERCIAL_PATTERN = r"公寓|大厦|广场|商厦|商务中心|时代|金融中心|写字楼|SOHO|soho"

# 政府「小区」列表里混进了产业园区的宿舍和厂房（如「恒丰工业城」「布心工业区」），
# 它们不是住宅，不参与租房推荐。
INDUSTRIAL_PATTERN = r"工业区|工业城|工业园|厂房|厂区|园区|宿舍"
