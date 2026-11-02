"""通勤计算与缓存。

三种方式、两个时段：
- **地铁/公交**：不分高峰平峰。地铁不受路面拥堵影响，8:30 和 14:00 耗时几乎一样，
  真正的差别是拥挤度——那不是耗时能表达的，所以不假装能算。
- **驾车**：分早高峰（次日 8:30）和平峰（次日 14:00），结果展示成区间。
  区间本身有信息量：波动大的位置，通勤更不可靠。
- **骑行**：高德只有自行车路径，电动车按 `EBIKE_FACTOR` 折算，界面上标「近似」。

交通费也在这里算，因为三种方式口径不同：
- 地铁：用接口返回的真实票价
- 驾车：里程 × 每公里成本（油费+损耗）
- 骑行：不记
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .amap import AmapError, biking_route, driving_route, transit_route
from .query import haversine_km

# 自行车 → 电动自行车的折算系数。电动车实测比自行车快约 25%~35%。
EBIKE_FACTOR = 0.75
# 驾车每公里成本（油费 + 保养损耗），元
DRIVING_COST_PER_KM = 0.75
# 每工作日往返次数
TRIPS_PER_DAY = 2
WORKDAYS_PER_MONTH = 22

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS commute_cache (
    origin_lat REAL NOT NULL,
    origin_lng REAL NOT NULL,
    dest_lat   REAL NOT NULL,
    dest_lng   REAL NOT NULL,
    mode       TEXT NOT NULL,
    slot       TEXT NOT NULL,          -- flat | peak | offpeak
    minutes    REAL,
    distance_m REAL,
    fare       REAL,
    transfers  INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (origin_lat, origin_lng, dest_lat, dest_lng, mode, slot)
);
"""


@dataclass(frozen=True, slots=True)
class Commute:
    """一个候选住处的通勤画像。"""

    transit_min: float | None = None
    transit_fare: float | None = None
    transit_transfers: int | None = None
    driving_peak_min: float | None = None
    driving_offpeak_min: float | None = None
    driving_km: float | None = None
    biking_min: float | None = None
    source: str = "api"  # api | cache | estimate
    transit_estimated: bool = False  # True = 没有真实公交方案，用步行/直线估算的

    def minutes(self, mode: str) -> float | None:
        return {
            "transit": self.transit_min,
            "driving": self.driving_peak_min,
            "biking": self.biking_min,
        }.get(mode)

    def monthly_fee(self, mode: str) -> float:
        """每月交通费。"""
        if mode == "transit":
            fare = self.transit_fare if self.transit_fare is not None else 4.0
            return round(fare * TRIPS_PER_DAY * WORKDAYS_PER_MONTH, 2)
        if mode == "driving":
            km = self.driving_km or 0.0
            return round(km * DRIVING_COST_PER_KM * TRIPS_PER_DAY * WORKDAYS_PER_MONTH, 2)
        return 0.0


def _k(value: float) -> float:
    return round(value, 5)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(CACHE_SCHEMA)
    conn.commit()


def _cache_get(conn, origin, dest, mode: str, slot: str) -> Commute | None:
    row = conn.execute(
        """
        SELECT minutes, distance_m, fare, transfers FROM commute_cache
         WHERE origin_lat=? AND origin_lng=? AND dest_lat=? AND dest_lng=?
           AND mode=? AND slot=?
        """,
        (_k(origin[0]), _k(origin[1]), _k(dest[0]), _k(dest[1]), mode, slot),
    ).fetchone()
    return row


def _cache_put(conn, origin, dest, mode: str, slot: str, result) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO commute_cache
            (origin_lat, origin_lng, dest_lat, dest_lng, mode, slot,
             minutes, distance_m, fare, transfers, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _k(origin[0]), _k(origin[1]), _k(dest[0]), _k(dest[1]), mode, slot,
            result.minutes, result.distance_m, result.fare, result.transfers,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def _departure_timestamp(hour: int, minute: int = 0) -> int:
    """取次日某个时刻（高德的 departure_time 只支持未来时间）。"""
    moment = (datetime.now() + timedelta(days=1)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return int(moment.timestamp())


def compute_commute(
    conn: sqlite3.Connection,
    destination: tuple[float, float],
    home: tuple[float, float],
    *,
    with_driving: bool = True,
    with_biking: bool = True,
    use_cache: bool = True,
) -> Commute:
    """算一个候选住处到目的地的通勤。任何一项失败都退化为 None，不抛异常。"""
    _ensure_schema(conn)
    result = Commute()

    # ---- 地铁/公交
    cached = _cache_get(conn, destination, home, "transit", "flat") if use_cache else None
    if cached is not None:
        result = replace(
            result,
            transit_min=cached["minutes"],
            transit_fare=cached["fare"],
            transit_transfers=cached["transfers"],
        )
    else:
        try:
            route = transit_route(destination, home)
            if route is not None:
                result = replace(
                    result,
                    transit_min=route.minutes,
                    transit_fare=route.fare,
                    transit_transfers=route.transfers,
                )
                _cache_put(conn, destination, home, "transit", "flat", route)
        except AmapError:
            pass

    # 近距离可能根本没有公交方案（走过去更快），这时给出标注过的估算，
    # 而不是留空——留空会让这个候选直接从排名里消失，用户看不到近的房子。
    if result.transit_min is None:
        straight_km = haversine_km(destination[0], destination[1], home[0], home[1])
        if straight_km <= 1.5:
            result = replace(
                result, transit_min=round(straight_km / 5.0 * 60, 1),
                transit_fare=0.0, source="estimate", transit_estimated=True,
            )
        else:
            result = replace(
                result, transit_min=round(straight_km * 1.35 / 20.0 * 60, 1),
                transit_fare=2.0, source="estimate", transit_estimated=True,
            )

    # ---- 驾车（高峰 / 平峰）
    if with_driving:
        for slot, hour in (("peak", 8), ("offpeak", 14)):
            cached = _cache_get(conn, destination, home, "driving", slot) if use_cache else None
            if cached is not None:
                route_min, route_km = cached["minutes"], cached["distance_m"]
            else:
                try:
                    route = driving_route(
                        destination, home, departure_time=_departure_timestamp(hour)
                    )
                except AmapError:
                    route = None
                if route is None:
                    continue
                route_min, route_km = route.minutes, route.distance_m
                _cache_put(conn, destination, home, "driving", slot, route)

            if slot == "peak":
                result = replace(result, driving_peak_min=route_min, driving_km=(route_km or 0) / 1000)
            else:
                result = replace(result, driving_offpeak_min=route_min)

    # ---- 骑行（自行车折算电动车）
    if with_biking:
        cached = _cache_get(conn, destination, home, "biking", "flat") if use_cache else None
        if cached is not None:
            result = replace(result, biking_min=cached["minutes"])
        else:
            try:
                route = biking_route(destination, home)
            except AmapError:
                route = None
            if route is not None:
                minutes = round(route.minutes * EBIKE_FACTOR, 1)
                result = replace(result, biking_min=minutes)
                _cache_put(conn, destination, home, "biking", "flat", route)

    return result


def apply_to_candidates(
    conn: sqlite3.Connection,
    destination: tuple[float, float],
    candidates: list,
    *,
    primary_mode: str = "transit",
    extra_limit: int = 10,
) -> list:
    """给候选批量补通勤。

    关键修正：**用户选的那种方式，必须给全部候选都算**。
    之前只给最近的 10 个算驾车/骑行，结果切到「驾车」时，
    剩下 6 个候选因为没有驾车数据被直接过滤掉，页面显示"没有候选"。

    次要方式（地铁通常已经算了）只给最近的若干个补，省配额。
    """
    enriched = []
    for index, candidate in enumerate(candidates):
        home = (candidate.lat, candidate.lng)
        within_extra = index < extra_limit
        commute = compute_commute(
            conn,
            destination,
            home,
            with_driving=(primary_mode == "driving") or within_extra,
            with_biking=(primary_mode == "biking") or within_extra,
        )
        enriched.append(replace(candidate, commute=commute))
    return enriched
