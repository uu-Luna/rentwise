"""成本模型：把「租金 + 通勤 + 交通费」合成一个可比较的数。

设计决定（和产品需求直接相关）
------------------------------
**时间单价不由我们假设，由客户填。**

理由：给「一小时值多少钱」定一个默认值，等于替用户做了价值判断。
一个刚毕业的人和一个工作五年的人，答案完全不同，而这个数字会直接改变推荐结果
（时间越贵，推荐越靠近公司）。

所以分两种模式：

- **不填**：排序按「租金 + 交通费」这个纯现金成本，**同时把每个方案每月要花多少小时
  通勤明确显示出来**。让人自己看着时间做判断。
- **填了**：排序切换成「租金 + 交通费 + 时间成本」，这时才有统一的度量单位。

不填的时候我们绝不偷偷塞一个默认时薪。

另一个容易被忽略的点：通勤成本要按**每月小时数**展示，而不是"单程 35 分钟"。
后者没有冲击力，前者是「你每个月有 26 小时在路上」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

WORKDAYS_PER_MONTH = 22
TRIPS_PER_DAY = 2
WEEKS_PER_MONTH = 4.33


def monthly_commute_hours(minutes_one_way: float, *, workdays: int = WORKDAYS_PER_MONTH) -> float:
    """单程分钟 → 每月通勤小时数（往返 × 工作日）。"""
    return round(minutes_one_way * TRIPS_PER_DAY * workdays / 60.0, 1)


def weekly_commute_hours(minutes_one_way: float, *, workdays: int = WORKDAYS_PER_MONTH) -> float:
    return round(monthly_commute_hours(minutes_one_way, workdays=workdays) / WEEKS_PER_MONTH, 1)


@dataclass(frozen=True, slots=True)
class CommuteCost:
    """一个候选住处的成本拆解。"""

    minutes_one_way: float
    hours_per_month: float
    hours_per_week: float
    transport_fee: float
    rent: float
    cash_cost: float
    time_cost: float | None
    real_cost: float
    hourly_value: float | None

    @property
    def has_time_value(self) -> bool:
        return self.time_cost is not None


def evaluate(
    rent: float,
    minutes_one_way: float,
    transport_fee: float = 0.0,
    hourly_value: float | None = None,
    *,
    workdays: int = WORKDAYS_PER_MONTH,
) -> CommuteCost:
    """算出单个候选的成本。

    hourly_value 为 None 时，time_cost 为 None，real_cost 就等于现金成本——
    这不是"把时间当免费"，而是"我们不替你估价"，界面上必须把两者区分显示。
    """
    hours = monthly_commute_hours(minutes_one_way, workdays=workdays)
    cash = round(rent + transport_fee, 2)
    time_cost = round(hours * hourly_value, 2) if hourly_value not in (None, 0) else None
    real = round(cash + time_cost, 2) if time_cost is not None else cash

    return CommuteCost(
        minutes_one_way=round(minutes_one_way, 1),
        hours_per_month=hours,
        hours_per_week=weekly_commute_hours(minutes_one_way, workdays=workdays),
        transport_fee=round(transport_fee, 2),
        rent=round(rent, 2),
        cash_cost=cash,
        time_cost=time_cost,
        real_cost=real,
        hourly_value=hourly_value,
    )


@dataclass(frozen=True, slots=True)
class Option:
    """一个候选房源（或一张方案里的一个小区）。"""

    kind: str          # community | village
    district: str
    name: str
    layout: str
    rent: float
    minutes_one_way: float
    transport_fee: float = 0.0
    mode: str = "transit"   # transit | driving | biking
    extra: dict = field(default_factory=dict)

    def cost(self, hourly_value: float | None = None) -> CommuteCost:
        return evaluate(self.rent, self.minutes_one_way, self.transport_fee, hourly_value)


@dataclass(frozen=True, slots=True)
class Plan:
    """一种"打法"。同一个候选集合，按不同目标排序就得到不同的计划。"""

    code: str
    title: str
    subtitle: str
    sort_key: Callable[["RankedOption"], float]
    only_kind: str | None = None


@dataclass(frozen=True, slots=True)
class RankedOption:
    option: Option
    cost: CommuteCost
    rank: int = 0


PLANS: tuple[Plan, ...] = (
    Plan("A", "省钱型", "月租最低，愿意用通勤时间换钱", lambda r: r.option.rent),
    Plan("B", "均衡型", "租金 + 交通费最低（填了时间价值则含时间成本）", lambda r: r.cost.real_cost),
    Plan("C", "省时型", "通勤时间最短，租金可能更高", lambda r: r.option.minutes_one_way),
    Plan("D", "城中村型", "只看城中村，低成本 + 中短通勤", lambda r: r.cost.real_cost,
         only_kind="village"),
)


def rank(options: Iterable[Option], hourly_value: float | None = None) -> list[RankedOption]:
    """按真实成本排序（不填时间价值时即现金成本）。"""
    ranked = [RankedOption(o, o.cost(hourly_value)) for o in options]
    ranked.sort(key=lambda r: r.cost.real_cost)
    return [RankedOption(r.option, r.cost, i + 1) for i, r in enumerate(ranked)]


def build_plans(
    options: Sequence[Option],
    hourly_value: float | None = None,
    *,
    top_n: int = 5,
) -> dict[str, list[RankedOption]]:
    """把候选分成 A/B/C/D 四套方案，每套给 top_n 个。

    同一批候选、不同排序目标 → 不同"打法"。这样用户看到的不是一张长列表，
    而是"我有哪几种选择"。空方案会被过滤掉（比如没有城中村候选时不留 D）。
    """
    result: dict[str, list[RankedOption]] = {}
    for plan in PLANS:
        pool = [o for o in options if plan.only_kind is None or o.kind == plan.only_kind]
        if not pool:
            continue
        ranked = [RankedOption(o, o.cost(hourly_value)) for o in pool]
        ranked.sort(key=plan.sort_key)
        result[plan.code] = [
            RankedOption(r.option, r.cost, i + 1) for i, r in enumerate(ranked[:top_n])
        ]
    return result


def pick_tradeoff_pair(options: Sequence[Option]) -> tuple[Option, Option]:
    """挑一对"值得比较"的住处，而不是拿全城最贵去和最便宜比。

    踩过的坑：直接比「租金最低」和「通勤最短」，在南山会得到
    「多花 4,089 元换 8 分钟，折合每分钟 531 元」这种废话——
    因为最快的那个恰好是豪宅，比较失去了意义。

    做法：先定最便宜的，再把"贵得离谱"的排除（租金 ≤ 最便宜 × 1.5），
    在这个区间里找最快的。这样比出来的是真实存在过的取舍。
    """
    if not options:
        raise ValueError("没有候选")
    cheapest = min(options, key=lambda o: o.rent)
    ceiling = cheapest.rent * 1.5
    affordable = [o for o in options if o.rent <= ceiling]
    fastest = min(affordable or options, key=lambda o: o.minutes_one_way)
    if fastest is cheapest:
        # 预算区间里没有更快的，那就退一步用全局最快的，
        # 否则会得出"和自己是同一个小区"这种没用的话
        fastest = min(options, key=lambda o: o.minutes_one_way)
    return cheapest, fastest


def time_tradeoff(cheap: Option, fast: Option, hourly_value: float | None = None) -> str:
    """生成"多花 X 元换 Y 分钟值不值"的一句话结论。

    这是用户真正想看的东西，也是这个项目区别于"排序列表"的地方。
    """
    if cheap is fast:
        return "两个方案是同一个小区，差别只在其他条件。"

    rent_gap = round(fast.rent - cheap.rent, 2)
    minute_gap = round(cheap.minutes_one_way - fast.minutes_one_way, 1)

    if minute_gap <= 0:
        return f"{fast.name} 不仅更省时，租金也只差 {rent_gap:,.0f} 元，直接选它。"
    if rent_gap <= 0:
        return f"{fast.name} 同时更便宜也更快，没有取舍。"

    per_minute = rent_gap / minute_gap
    hours_saved = monthly_commute_hours(minute_gap)
    per_hour = rent_gap / hours_saved if hours_saved else 0.0

    if hourly_value in (None, 0):
        # 不替用户定价：把"每月少在路上多少小时"和"折合每小时多少钱"摊开，让他自己对标
        return (
            f"{fast.name} 比 {cheap.name} 每月贵 {rent_gap:,.0f} 元，"
            f"换来单程少 {minute_gap:,.0f} 分钟（每月少在路上 {hours_saved:.0f} 小时）。"
            f"这相当于每小时 ¥{per_hour:,.0f} —— 你觉得自己的时间比这个更值钱吗？"
        )

    saved_value = round(hours_saved * hourly_value, 2)
    net = round(saved_value - rent_gap, 2)
    verdict = "值得" if net > 0 else "不值得"
    return (
        f"{fast.name} 比 {cheap.name} 每月贵 {rent_gap:,.0f} 元，"
        f"但省下 {hours_saved:.0f} 小时通勤，按时薪 {hourly_value:,.0f} 元折算值 "
        f"{saved_value:,.0f} 元 —— 净{'赚' if net > 0 else '亏'} {abs(net):,.0f} 元，**{verdict}**。"
    )
