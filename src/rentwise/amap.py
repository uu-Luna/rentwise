"""高德 Web 服务封装：把「小区名 + 区」变成经纬度。

为什么需要这一步
----------------
政府数据里只有「区 + 街道 + 小区名」，**没有坐标**。算通勤必须有坐标，
所以只能用 POI 检索把名字还原成地点。

三个必须处理好的工程问题
------------------------
1. **QPS 限流**：个人开发者的 Key 并发配额很低，实测连续请求会返回
   `CUQPS_HAS_EXCEEDED_THE_LIMIT`。所以要限速 + 指数退避重试，
   否则跑一批数据会有一半失败。
2. **匹配质量**：检索结果里混着「XX花园停车场」「XX花园3号楼」这类干扰项，
   必须做名称相似度判断，不能盲取第一条。低置信度的结果要标记出来，
   而不是当成坐标用。
3. **增量与缓存**：已经拿到坐标的小区不再重复请求。这是省钱也是省时间。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PLACE_TEXT = "https://restapi.amap.com/v5/place/text"
PLACE_AROUND = "https://restapi.amap.com/v5/place/around"
GEOCODE = "https://restapi.amap.com/v3/geocode/geo"
ROUTE_TRANSIT = "https://restapi.amap.com/v5/direction/transit/integrated"
ROUTE_DRIVING = "https://restapi.amap.com/v5/direction/driving"
ROUTE_BICYCLING = "https://restapi.amap.com/v5/direction/bicycling"

# 住宅区 POI 分类码；用它可以显著减少停车场、商铺之类的干扰
RESIDENTIAL_TYPES = "120300"


def geocode_address(address: str, *, city: str = "深圳") -> tuple[float, float] | None:
    """把一段地址文本转成经纬度。用户输入公司地址时用这个。

    返回 (lat, lng)，取不到返回 None。
    """
    key = get_api_key()
    params = {"key": key, "address": address, "city": city}
    url = GEOCODE + "?" + urllib.parse.urlencode(params)
    payload = _request(url)

    geocodes = payload.get("geocodes") or []
    if not geocodes:
        return None
    location = str(geocodes[0].get("location", ""))
    parsed = _split_location(location)
    if parsed is None:
        return None
    lat, lng = parsed
    return (lat, lng) if in_shenzhen(lat, lng) else None


# --------------------------------------------------------------------- 周边搜索
def search_around(
    lat: float,
    lng: float,
    *,
    radius_m: int = 4000,
    page_num: int = 1,
    page_size: int = 25,
    types: str | None = RESIDENTIAL_TYPES,
) -> list[Poi]:
    """以某点为中心搜周边的住宅小区。

    这是整个项目的关键取数方式：**一次请求能换回一批带坐标的小区**。
    相比"给 3600 个小区逐个查坐标"，它的请求量小两个数量级，
    而且天然只返回目的地附近的候选。
    """
    key = get_api_key()
    params = {
        "key": key,
        "location": f"{lng:.6f},{lat:.6f}",
        "radius": radius_m,
        "page_num": page_num,
        "page_size": page_size,
        "sortrule": "distance",
    }
    if types:
        params["types"] = types
    payload = _request(PLACE_AROUND + "?" + urllib.parse.urlencode(params))

    pois: list[Poi] = []
    for item in payload.get("pois") or []:
        loc = _split_location(str(item.get("location", "")))
        if loc is None:
            continue
        plat, plng = loc
        if not in_shenzhen(plat, plng):
            continue
        pois.append(
            Poi(
                name=str(item.get("name", "")).strip(),
                district=str(item.get("adname", "")).strip(),
                address=str(item.get("address", "") or "").strip(),
                lat=plat,
                lng=plng,
            )
        )
    return pois


# --------------------------------------------------------------------- 路径规划
@dataclass(frozen=True, slots=True)
class RouteResult:
    """一次通勤的耗时结果。"""

    mode: str
    minutes: float
    distance_m: float
    fare: float | None = None
    transfers: int | None = None
    walking_m: float | None = None


def _loc(point: tuple[float, float]) -> str:
    lat, lng = point
    return f"{lng:.6f},{lat:.6f}"


def _first_path(payload: dict) -> dict | None:
    route = payload.get("route") or {}
    paths = route.get("paths") or []
    return paths[0] if paths else None


def transit_route(
    origin: tuple[float, float],
    destination: tuple[float, float],
    *,
    city_code: str = "440300",
) -> RouteResult | None:
    """公交 / 地铁方案。返回最优一条的耗时、票价、换乘次数。

    两个坑（都实测踩过）：
    - `city1/city2` 必须是**行政区划代码**（深圳 440300），传「深圳」会返回 INVALID_PARAMS
    - 不传 `show_fields=cost` 时，响应里**没有 duration 和票价**
    """
    key = get_api_key()
    params = {
        "key": key,
        "origin": _loc(origin),
        "destination": _loc(destination),
        "city1": city_code,
        "city2": city_code,
        "show_fields": "cost",
    }
    payload = _request(ROUTE_TRANSIT + "?" + urllib.parse.urlencode(params))
    route = payload.get("route") or {}
    transits = route.get("transits") or []
    if not transits:
        return None

    def duration_of(item: dict) -> float:
        cost = item.get("cost") or {}
        return float(cost.get("duration") or item.get("duration") or 1e18)

    best = min(transits, key=duration_of)
    cost = best.get("cost") or {}
    duration = duration_of(best) / 60.0

    try:
        fare = float(cost.get("transit_fee")) if cost.get("transit_fee") not in (None, "") else None
    except (TypeError, ValueError):
        fare = None

    segments = best.get("segments") or []
    rides = sum(1 for s in segments if s.get("bus") or s.get("railway"))
    return RouteResult(
        mode="transit",
        minutes=round(duration, 1),
        distance_m=float(best.get("distance") or 0),
        fare=fare,
        transfers=max(rides - 1, 0),
        walking_m=float(best.get("walking_distance") or 0),
    )


def driving_route(
    origin: tuple[float, float],
    destination: tuple[float, float],
    *,
    departure_time: int | None = None,
) -> RouteResult | None:
    """驾车方案。传 departure_time（unix 秒）可按该时刻的路况预测。

    同样需要 `show_fields=cost`，否则响应里没有 duration。
    """
    key = get_api_key()
    params = {
        "key": key,
        "origin": _loc(origin),
        "destination": _loc(destination),
        "show_fields": "cost",
    }
    if departure_time is not None:
        params["departure_time"] = departure_time

    try:
        payload = _request(ROUTE_DRIVING + "?" + urllib.parse.urlencode(params))
    except AmapError:
        if departure_time is None:
            raise
        params.pop("departure_time", None)
        payload = _request(ROUTE_DRIVING + "?" + urllib.parse.urlencode(params))

    path = _first_path(payload)
    if path is None:
        return None
    cost = path.get("cost") or {}
    tolls = cost.get("tolls") or 0
    return RouteResult(
        mode="driving",
        minutes=round(float(cost.get("duration") or 0) / 60.0, 1),
        distance_m=float(path.get("distance") or 0),
        fare=float(tolls) or None,
    )


def biking_route(
    origin: tuple[float, float], destination: tuple[float, float]
) -> RouteResult | None:
    """骑行方案。高德只有自行车，电动车按系数折算（见 commute 模块）。"""
    key = get_api_key()
    params = {"key": key, "origin": _loc(origin), "destination": _loc(destination)}
    payload = _request(ROUTE_BICYCLING + "?" + urllib.parse.urlencode(params))
    path = _first_path(payload)
    if path is None:
        return None
    distance = float(path.get("distance") or 0)
    return RouteResult(
        mode="biking",
        minutes=round(float(path.get("duration") or 0) / 60.0, 1),
        distance_m=distance,
    )


class AmapError(RuntimeError):
    """高德返回了错误状态或限流。"""


class NetworkUnstable(AmapError):
    """网络/DNS 层面的失败。这类错误应该中止整批任务，而不是逐个记为数据错误。"""


def load_env_file(path: str | Path) -> None:
    """极简 .env 读取：只支持 KEY=VALUE 和 # 注释，避免引入 python-dotenv。"""
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_api_key() -> str:
    # 项目根目录的 .env 优先，其次环境变量
   return "758c888525b9ebecb7210b3e34f3450f"
    root = Path(__file__).resolve().parents[2]
    load_env_file(root / ".env")
    key = os.getenv("AMAP_KEY", "").strip()
    if not key:
        raise AmapError(
            "缺少 AMAP_KEY。复制 .env.example 为 .env 并填入你的高德 Web 服务 Key。"
        )
    return key


@dataclass(frozen=True, slots=True)
class Poi:
    name: str
    district: str
    address: str
    lat: float
    lng: float


def _request(url: str, retries: int = 5, backoff: float = 3.0) -> dict:
    """带重试的 GET。网络错误和限流分开处理，等待时长不同。

    踩过的坑：跑了 2000 多个小区之后网络中断，`getaddrinfo failed`（DNS 解析失败）
    被当成普通错误直接记成 error，结果一口气刷出 1400 条假失败。
    所以网络类错误必须长退避重试，而且调用方要能识别"连续失败=环境故障"并中止。
    """
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = exc
            # 网络/DNS 问题，等待更久再试
            time.sleep(backoff * (attempt + 1))
            continue

        info = str(payload.get("info", ""))
        if payload.get("status") == "1":
            return payload
        if "CUQPS" in info or "LIMIT" in info.upper():
            time.sleep(backoff * (attempt + 1) * 1.5)
            last_error = AmapError(info)
            continue
        raise AmapError(f"高德返回错误：{info}")

    raise NetworkUnstable(f"重试 {retries} 次仍失败：{last_error}")


def _split_location(text: str) -> tuple[float, float] | None:
    if not text or "," not in text:
        return None
    try:
        lng, lat = text.split(",", 1)
        return float(lat), float(lng)
    except ValueError:
        return None


def search_poi(
    keywords: str,
    *,
    region: str = "440300",
    city_limit: bool = True,
    page_size: int = 10,
    types: str | None = RESIDENTIAL_TYPES,
) -> list[Poi]:
    """按名称搜 POI。

    region 默认用深圳市 adcode（440300），而不是「深圳」或区名。
    实测：传「南山区」会返回 0 条（高德把它当城市级区域解析，命中失败），
    传「罗湖区」「宝安区」却正常。为了稳定统一传 adcode，
    区级过滤交给 pick_best 用返回的 adname 判断。
    """
    key = get_api_key()
    params = {
        "key": key,
        "keywords": keywords,
        "region": region,
        "city_limit": "true" if city_limit else "false",
        "page_size": page_size,
    }
    if types:
        params["types"] = types
    url = PLACE_TEXT + "?" + urllib.parse.urlencode(params)

    payload = _request(url)
    pois: list[Poi] = []
    for item in payload.get("pois") or []:
        loc = _split_location(str(item.get("location", "")))
        if loc is None:
            continue
        lat, lng = loc
        pois.append(
            Poi(
                name=str(item.get("name", "")).strip(),
                district=str(item.get("adname", "")).strip(),
                address=str(item.get("address", "") or "").strip(),
                lat=lat,
                lng=lng,
            )
        )
    return pois


_NOISE_SUFFIX = re.compile(r"(停车场.*|地下车库.*|出入口.*|停车库.*|大门.*|门.*)$")

# 深圳的大致地理范围（纬度 22.35~22.95，经度 113.65~114.75）。
# 实测中「东涌家园一期」曾匹配到长春、「山海名苑」匹配到山东海阳——
# 名称相似度不低，但坐标离深圳上千公里。没有这个围栏，通勤时间会算出天文数字。
SHENZHEN_BBOX = (22.35, 113.65, 22.95, 114.75)

# 高德的行政区划与大鹏新区这类功能区不完全一致，做个别名映射
DISTRICT_ALIASES: dict[str, set[str]] = {
    "大鹏新区": {"大鹏新区", "龙岗区"},
    "光明区": {"光明区", "宝安区"},
    "坪山区": {"坪山区", "龙岗区"},
}


def in_shenzhen(lat: float, lng: float) -> bool:
    lat_min, lng_min, lat_max, lng_max = SHENZHEN_BBOX
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def district_ok(poi_district: str, target_district: str) -> bool:
    """POI 所在区是否与我们记录的区一致。POI 没给区时不做判断。"""
    if not poi_district:
        return True
    if poi_district == target_district:
        return True
    return poi_district in DISTRICT_ALIASES.get(target_district, set())


SHENZHEN_DISTRICTS = (
    "南山区", "福田区", "罗湖区", "宝安区", "龙华区", "龙岗区",
    "盐田区", "光明区", "坪山区", "大鹏新区",
)

# 行政区前缀，出现在一些 POI 名称里（如「广东省深圳市南山区龙坤居」），比较前要剥掉
_ADMIN_PREFIX = re.compile(r"^(广东省)?(深圳市)?(" + "|".join(SHENZHEN_DISTRICTS) + r")?")

# 通用房源后缀。同一处房子在政府库里叫「天井湖住宅楼」，
# 在高德里可能叫「天井湖小区」，字面相似度只有 0.60，其实是同一个地方。
_GENERIC_SUFFIX = re.compile(
    r"(小区|住宅楼|住宅区|新村|统建楼|公寓|花园|大厦|广场|苑|村|楼)$"
)


def normalize_name(name: str) -> str:
    """去掉括号注释、噪点后缀、间隔号和行政区前缀。"""
    text = re.sub(r"[（(].*?[）)]", "", name)
    text = _NOISE_SUFFIX.sub("", text)
    text = text.replace("·", "").replace("・", "").replace(" ", "")
    text = _ADMIN_PREFIX.sub("", text)
    return text.strip()


def core_name(name: str) -> str:
    """再剥一层通用后缀，得到「核心名」。用于识别同地不同写法。"""
    return _GENERIC_SUFFIX.sub("", normalize_name(name)).strip()


def detect_district(name: str) -> str | None:
    """从 POI 名称里提取行政区（高德的 adname 有时为空）。"""
    for district in SHENZHEN_DISTRICTS:
        if district in name:
            return district
    return None


def score_match(target: str, candidate: str) -> float:
    """名称相似度，0~1。完全相同 = 1.0。"""
    a, b = normalize_name(target), normalize_name(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    # 核心名相同（「天井湖住宅楼」vs「天井湖小区」）视为同一处，但不满分
    ca, cb = core_name(target), core_name(candidate)
    if ca and cb and ca == cb and len(ca) >= 3:
        return 0.88

    if a in b or b in a:
        # 「XX花园」vs「XX花园二期」这种
        return 0.92
    return difflib.SequenceMatcher(None, a, b).ratio()


def pick_best(
    target: str,
    district: str,
    pois: list[Poi],
    *,
    min_score: float = 0.80,
) -> tuple[Poi | None, float]:
    """在候选里挑最可信的一个。

    三道闸门，顺序很重要：
    1. **地理围栏**：坐标不在深圳范围内的，直接丢掉（防跨省误匹配）
    2. **行政区一致**：POI 在别的区，直接丢掉（防「东涌家园」匹配「东荣家园」）
    3. **名称相似度**：前两关都过了才比名字
    """
    best: Poi | None = None
    best_score = 0.0
    for poi in pois:
        if not in_shenzhen(poi.lat, poi.lng):
            continue
        # adname 为空时，尝试从名称里认出行政区，避免把南山的小区配到龙岗
        poi_district = poi.district or detect_district(poi.name) or ""
        if not district_ok(poi_district, district):
            continue
        score = score_match(target, poi.name)
        if score > best_score:
            best, best_score = poi, score
    if best is None or best_score < min_score:
        return None, min(best_score, 1.0)
    return best, min(best_score, 1.0)
