"""从「用户输入的地址」到「候选住处」。

流程：
    地址 → 地理编码拿坐标 → 周边搜索拿小区点 → 与库内租金按名称匹配 → 候选列表

为什么不做"全量补坐标"
----------------------
给 3635 个小区逐个查坐标要 3635 次请求、40 分钟以上连续网络。
而周边搜索一次请求就能拿回 25 个**带坐标**的小区，10 次请求覆盖目的地 4 公里内全部候选。
请求量差两个数量级，而且天然只返回"目的地附近"的房子——这正是需求本身。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from .amap import Poi, core_name, geocode_address, normalize_name, search_around

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


@dataclass(frozen=True, slots=True)
class Candidate:
    """一个有租金、有坐标的候选住处。"""

    kind: str            # community | village
    district: str
    name: str            # 库里的名称（用户搜索时用这个）
    layout: str
    rent: float
    lat: float
    lng: float
    distance_km: float
    poi_name: str        # 高德返回的名称
    minutes_one_way: float | None = None
    mode: str = "transit"
    commute: object | None = None   # 由 commute.apply_to_candidates 填充


@dataclass(frozen=True, slots=True)
class RentEntry:
    kind: str
    district: str
    name: str
    rent: float


def build_rent_index(conn: sqlite3.Connection, layout: str) -> dict[tuple[str, str], RentEntry]:
    """建「区 + 归一化名称 → 租金」索引。

    只在住宅小区和城中村里找，商办公寓和产业厂房不参与推荐。
    """
    rows = conn.execute(
        """
        SELECT q.kind, q.district, q.name, q.monthly_rent_adj
          FROM rent_quotes q
         WHERE q.layout = ?
           AND q.monthly_rent_adj IS NOT NULL
           AND (
                q.kind = 'village'
                OR (q.kind = 'community'
                    AND q.ref_id IN (SELECT id FROM communities WHERE property_type = 'residential'))
           )
        """,
        (layout,),
    ).fetchall()

    index: dict[tuple[str, str], RentEntry] = {}
    for row in rows:
        entry = RentEntry(row["kind"], row["district"], row["name"], float(row["monthly_rent_adj"]))
        district = row["district"]
        # 全名 和 核心名 都建索引，提高命中率
        for key in {normalize_name(row["name"]), core_name(row["name"])}:
            if key:
                index.setdefault((district, key), entry)
    return index


def _lookup(index: dict[tuple[str, str], RentEntry], poi: Poi) -> RentEntry | None:
    if not poi.name:
        return None
    for name_variant in {normalize_name(poi.name), core_name(poi.name)}:
        if not name_variant:
            continue
        entry = index.get((poi.district, name_variant))
        if entry is not None:
            return entry
    return None


def resolve_address(address: str) -> tuple[float, float] | None:
    """地址文本 → 坐标。地理编码失败时退化为 POI 检索。"""
    point = geocode_address(address)
    if point is not None:
        return point

    from .amap import search_poi

    for poi in search_poi(address, region="深圳", types=None, page_size=5):
        return (poi.lat, poi.lng)
    return None


def find_candidates(
    conn: sqlite3.Connection,
    lat: float,
    lng: float,
    layout: str,
    *,
    radius_m: int = 4000,
    max_pages: int = 4,
    limit: int = 40,
) -> list[Candidate]:
    """搜周边小区，匹配库内租金，按直线距离排序取前 limit 个。"""
    index = build_rent_index(conn, layout)
    if not index:
        return []

    seen: set[tuple[str, str]] = set()
    candidates: list[Candidate] = []

    for page in range(1, max_pages + 1):
        try:
            pois = search_around(lat, lng, radius_m=radius_m, page_num=page)
        except Exception:
            break
        if not pois:
            break
        for poi in pois:
            entry = _lookup(index, poi)
            if entry is None:
                continue
            key = (entry.district, entry.name)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                Candidate(
                    kind=entry.kind,
                    district=entry.district,
                    name=entry.name,
                    layout=layout,
                    rent=entry.rent,
                    lat=poi.lat,
                    lng=poi.lng,
                    distance_km=round(haversine_km(lat, lng, poi.lat, poi.lng), 2),
                    poi_name=poi.name,
                )
            )

    candidates.sort(key=lambda c: c.distance_km)
    return candidates[:limit]


def rent_stats(
    conn: sqlite3.Connection,
    layout: str,
    district: str | None = None,
    kind: str | None = None,
) -> dict:
    """某户型（可按区、按类型）的租金分布。用来给界面设参照值。"""
    sql = """
        SELECT q.kind, q.monthly_rent_adj AS rent
          FROM rent_quotes q
         WHERE q.layout = ?
           AND q.monthly_rent_adj IS NOT NULL
           AND (
                q.kind = 'village'
                OR (q.kind = 'community'
                    AND q.ref_id IN (SELECT id FROM communities WHERE property_type = 'residential'))
           )
    """
    params: list[object] = [layout]
    if district:
        sql += " AND q.district = ?"
        params.append(district)
    if kind:
        sql += " AND q.kind = ?"
        params.append(kind)

    rows = [float(r[1]) for r in conn.execute(sql, params)]
    if not rows:
        return {"n": 0, "min": None, "median": None, "max": None}
    rows.sort()
    return {
        "n": len(rows),
        "min": rows[0],
        "median": rows[len(rows) // 2],
        "max": rows[-1],
    }
