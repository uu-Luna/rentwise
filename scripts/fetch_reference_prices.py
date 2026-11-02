"""Step 0：拉取深圳市住建局「房屋租赁参考价格」公开数据。

数据来源
--------
深圳市住房和建设局 · 深圳市房屋租赁参考价格查询平台
https://zjj.sz.gov.cn/fwzljgcx/

两份数据：
1. 住宅小区（含商品房 / 统建楼 / 商务公寓）：4319 条，单价为 元/㎡·月
   字段：区、街道、小区名、高层租金(highRent)、多层租金(multiRent)
2. 城中村私宅：2043 条，直接给出四档户型的**月租总额**
   字段：区、街道、地址、村名、单间/一房一厅/两房/三房及以上 月租

合规说明（重要）
----------------
- 这是政府公开的查询平台，数据本身面向公众免费查询，来源合法。
- 但本脚本调用的是平台前端的内部接口，不是官方发布的开放 API。
  因此：**低频调用（各 1 次）、结果本地缓存、不转售、不做全站高频轮询**。
- 生成的 CSV 只用于个人研究，不随仓库公开原始全量数据；如果要发 GitHub，
  公开的是**代码和聚合结果**，原始数据让使用者自己跑这个脚本获取。
- 数据年份需自行确认（平台未标注年度，公开报道为 2022 年度口径）。
"""

from __future__ import annotations

import csv
import json
import re
import time
import urllib.request
import http.cookiejar
from pathlib import Path

BASE = "https://zjj.sz.gov.cn"
LIST_PAGE = "/fwzljgcx/vue/list?page=1"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"


class Client:
    """带 cookie 与 CSRF 的会话。"""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.csrf_header = "X-CSRF-TOKEN"
        self.csrf_token = ""
        self._bootstrap()

    def _bootstrap(self) -> None:
        req = urllib.request.Request(BASE + LIST_PAGE, headers={"User-Agent": UA})
        with self.opener.open(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "ignore")
        self.csrf_header = self._meta(html, "_csrf_header") or "X-CSRF-TOKEN"
        self.csrf_token = self._meta(html, "_csrf") or ""

    @staticmethod
    def _meta(html: str, name: str) -> str | None:
        for pattern in (
            r'<meta[^>]*name=["\']%s["\'][^>]*content=["\']([^"\']*)["\']',
            r'<meta[^>]*content=["\']([^"\']*)["\'][^>]*name=["\']%s["\']',
        ):
            match = re.search(pattern % re.escape(name), html, re.I)
            if match:
                return match.group(1)
        return None

    def post(self, path: str, payload: dict) -> dict:
        headers = {
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": BASE + LIST_PAGE,
            "X-Requested-With": "XMLHttpRequest",
            self.csrf_header: self.csrf_token,
        }
        body = json.dumps(payload).encode()
        req = urllib.request.Request(BASE + path, data=body, headers=headers, method="POST")
        with self.opener.open(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))


COMMUNITY_FIELDS = [
    ("communityId", "community_id"),
    ("districtname", "区"),
    ("zoneName", "街道"),
    ("communityName", "小区名"),
    ("highRent", "高层单价_元每平每月"),
    ("multiRent", "多层单价_元每平每月"),
]

VILLAGE_FIELDS = [
    ("villageNo", "village_no"),
    ("region", "区"),
    ("street", "街道"),
    ("address", "地址"),
    ("villageName", "村名"),
    ("singleRoomPrice", "单间19平_月租"),
    ("oneRoomPrice", "一房一厅35平_月租"),
    ("twoRoomPrice", "两房60平_月租"),
    ("multiRoomPrice", "三房及以上85平_月租"),
]


def fetch_communities(client: Client) -> list[dict]:
    data = client.post(
        "/fwzljgcx/pgsj/rentRefer/communityPage",
        {"area": "", "page": 1, "rows": 5000, "searchText": "", "street": ""},
    )["data"]
    total, rows = data["total"], data["list"]
    if len(rows) < total:
        raise RuntimeError(f"小区数据未取全：{len(rows)}/{total}，需要分页")
    return rows


def fetch_villages(client: Client) -> list[dict]:
    data = client.post(
        "/fwzljgcx/pgsj/rentRefer/czcPage",
        {"area": "", "page": 1, "rows": 5000, "searchText": "", "street": ""},
    )["data"]
    total, rows = data["total"], data["list"]
    if len(rows) < total:
        raise RuntimeError(f"城中村数据未取全：{len(rows)}/{total}，需要分页")
    return rows


def write_csv(rows: list[dict], fields: list[tuple[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([label for _, label in fields])
        for row in rows:
            writer.writerow([row.get(key, "") for key, _ in fields])


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = Client()
    print("CSRF:", client.csrf_header, "token 长度", len(client.csrf_token))

    print("\n拉取住宅小区…")
    communities = fetch_communities(client)
    time.sleep(1.0)
    print(f"  拿到 {len(communities)} 条")

    print("拉取城中村私宅…")
    villages = fetch_villages(client)
    time.sleep(1.0)
    print(f"  拿到 {len(villages)} 条")

    write_csv(communities, COMMUNITY_FIELDS, OUT_DIR / "communities.csv")
    write_csv(villages, VILLAGE_FIELDS, OUT_DIR / "villages.csv")

    # ---- 统计摘要
    from collections import Counter

    print("\n" + "=" * 56)
    print("小区：按区")
    for name, n in Counter(r.get("districtname", "?") for r in communities).most_common():
        print(f"  {name:<6} {n}")

    with_price = [
        r for r in communities
        if isinstance(r.get("highRent") or r.get("multiRent"), (int, float))
    ]
    print(f"\n小区：可算出单价的 {len(with_price)} / {len(communities)}")
    high = [r["highRent"] for r in communities if isinstance(r.get("highRent"), (int, float))]
    multi = [r["multiRent"] for r in communities if isinstance(r.get("multiRent"), (int, float))]
    print(f"  高层单价 {min(high)} ~ {max(high)}（{len(high)} 条）")
    print(f"  多层单价 {min(multi)} ~ {max(multi)}（{len(multi)} 条）")

    one = [r["oneRoomPrice"] for r in villages if isinstance(r.get("oneRoomPrice"), (int, float))]
    print("\n城中村：一房一厅 35㎡ 月租")
    print(f"  {min(one):.0f} ~ {max(one):.0f} 元，中位 {sorted(one)[len(one)//2]:.0f} 元，共 {len(one)} 条")
    cheapest = sorted(
        (r for r in villages if isinstance(r.get("oneRoomPrice"), (int, float))),
        key=lambda r: r["oneRoomPrice"],
    )[:5]
    print("  最便宜 5 个村：")
    for r in cheapest:
        print(f"    {r['region']}{r['street']} {r['villageName']}  {r['oneRoomPrice']:.0f} 元/月")

    print(f"\n已写入：{OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
