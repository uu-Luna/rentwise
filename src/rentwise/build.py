"""把两份 CSV 建成结构化数据库，并预计算「房源 × 户型 → 月租」。

设计要点：

1. **原值和校准值都存。** `monthly_rent` 是政府参考价直接换算的原始值，
   `monthly_rent_adj` 是乘了市场修正系数（1.045）之后的值。
   两个都留着，界面上才能做「官方原值 ⟷ 校准后」的开关，而不是二选一藏一个。

2. **物业类型必须分开。** 实测 276 条真实挂牌房源后发现：
   住宅类的「挂牌单价 / 政府单价」秩相关 0.943，商办公寓只有 0.252。
   混在一起会互相污染（整体掉到 0.772）。所以按名称规则打标签，
   排名默认只用住宅类。

3. **面积来源要标注。** 一房一厅用实测中位 42.9㎡，其他户型暂用政府标准面积，
   字段 `area_source` 区分 measured / policy / estimated，界面上要显示出来。

幂等：重复运行会先清空再重建，结果一致。
"""

from __future__ import annotations

import csv
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    BASELINE_YEAR,
    CITY,
    COMMERCIAL_PATTERN,
    COMMUNITIES_CSV,
    INDUSTRIAL_PATTERN,
    LAYOUTS,
    MARKET_ADJUST,
    MARKET_ADJUST_NOTE,
    RATE_MAX,
    RATE_MIN,
    SOURCE_NAME,
    SOURCE_URL,
    VILLAGES_CSV,
)
from .db import connect

UNKNOWN_DISTRICT = "未知"
_COMMERCIAL_RE = re.compile(COMMERCIAL_PATTERN)
_INDUSTRIAL_RE = re.compile(INDUSTRIAL_PATTERN)

PROPERTY_LABELS = {
    "residential": "住宅",
    "commercial": "商办公寓",
    "industrial": "产业/厂房",
}


def classify_property(name: str) -> str:
    """按名称规则推断物业类型：residential / commercial / industrial。

    这是**规则推断，不是政府标注**。依据是实测结果：
    名字里带「公寓/大厦/广场/中心」的房源，挂牌单价系统性低于政府参考价，
    且排序与政府参考价几乎无关（Spearman 0.252），说明它们的定价逻辑不同。

    另外政府「小区」列表里混进了产业园区宿舍和厂房（「恒丰工业城」「布心工业区」），
    这些不是住宅，也必须排除，否则会被当成超低价房源推荐出来。

    规则会有误判，所以字段名用的是 property_type 而不是 is_residential。
    """
    if _INDUSTRIAL_RE.search(name):
        return "industrial"
    if _COMMERCIAL_RE.search(name):
        return "commercial"
    return "residential"


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number > 0 else None


def _read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def build(db_path: str | Path, *, verbose: bool = True) -> dict:
    if not COMMUNITIES_CSV.exists() or not VILLAGES_CSV.exists():
        raise FileNotFoundError(
            f"缺少原始数据：{COMMUNITIES_CSV} / {VILLAGES_CSV}\n"
            "先运行 scripts/fetch_reference_prices.py"
        )

    communities = _read(COMMUNITIES_CSV)
    villages = _read(VILLAGES_CSV)

    conn = connect(db_path)
    try:
        for table in ("rent_quotes", "communities", "villages", "streets", "meta"):
            conn.execute(f"DELETE FROM {table}")

        stats = {
            "communities_in": len(communities),
            "communities_ok": 0,
            "communities_priced": 0,
            "villages_in": len(villages),
            "villages_ok": 0,
            "villages_priced": 0,
            "quotes": 0,
            "rate_out_of_range": 0,
            "skipped_duplicate": 0,
            "residential": 0,
            "commercial": 0,
        }

        # ---------------- 小区
        for row in communities:
            district = (row.get("区") or "").strip() or UNKNOWN_DISTRICT
            street = (row.get("街道") or "").strip() or UNKNOWN_DISTRICT
            name = (row.get("小区名") or "").strip()
            if not name:
                continue

            high = _num(row.get("高层单价_元每平每月"))
            multi = _num(row.get("多层单价_元每平每月"))

            for value in (high, multi):
                if value is not None and not (RATE_MIN <= value <= RATE_MAX):
                    stats["rate_out_of_range"] += 1

            if high is not None:
                rate, basis = high, "high"
            elif multi is not None:
                rate, basis = multi, "multi"
            else:
                rate, basis = None, None

            property_type = classify_property(name)

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO communities
                    (district, street, name, rate_high, rate_multi, rate_used,
                     rate_basis, property_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (district, street, name, high, multi, rate, basis, property_type),
            )
            if cur.rowcount == 0:
                stats["skipped_duplicate"] += 1
                continue
            stats["communities_ok"] += 1
            stats[property_type] = stats.get(property_type, 0) + 1
            cid = int(cur.lastrowid)

            if rate is None:
                continue
            stats["communities_priced"] += 1

            for layout in LAYOUTS:
                rent = round(rate * layout.area_sqm, 2)
                conn.execute(
                    """
                    INSERT INTO rent_quotes
                        (kind, ref_id, district, street, name, layout,
                         area_sqm, area_source, monthly_rent, monthly_rent_adj,
                         rate_per_sqm, basis_note)
                    VALUES ('community', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid, district, street, name, layout.code,
                        layout.area_sqm, layout.area_source,
                        rent, round(rent * MARKET_ADJUST, 2), rate,
                        f"{'高层' if basis == 'high' else '多层'} {rate:.0f} 元/㎡/月 "
                        f"× {layout.area_sqm:.0f}㎡（{property_type}）",
                    ),
                )
                stats["quotes"] += 1

        # ---------------- 城中村
        for row in villages:
            district = (row.get("区") or "").strip() or UNKNOWN_DISTRICT
            street = (row.get("街道") or "").strip() or UNKNOWN_DISTRICT
            name = (row.get("村名") or "").strip()
            if not name:
                continue

            rents = {
                "rent_single": _num(row.get("单间19平_月租")),
                "rent_one": _num(row.get("一房一厅35平_月租")),
                "rent_two": _num(row.get("两房60平_月租")),
                "rent_multi_room": _num(row.get("三房及以上85平_月租")),
            }

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO villages
                    (district, street, name, address,
                     rent_single, rent_one, rent_two, rent_multi_room)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    district, street, name, (row.get("地址") or "").strip() or None,
                    rents["rent_single"], rents["rent_one"],
                    rents["rent_two"], rents["rent_multi_room"],
                ),
            )
            if cur.rowcount == 0:
                stats["skipped_duplicate"] += 1
                continue
            stats["villages_ok"] += 1
            vid = int(cur.lastrowid)

            priced = False
            for layout in LAYOUTS:
                if layout.village_field is None:
                    continue
                rent = rents.get(layout.village_field)
                if rent is None:
                    continue
                priced = True
                conn.execute(
                    """
                    INSERT INTO rent_quotes
                        (kind, ref_id, district, street, name, layout,
                         area_sqm, area_source, monthly_rent, monthly_rent_adj,
                         rate_per_sqm, basis_note)
                    VALUES ('village', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        vid, district, street, name, layout.code,
                        layout.area_sqm, layout.area_source,
                        round(rent, 2), round(rent * MARKET_ADJUST, 2),
                        f"城中村标准户型 {layout.area_sqm:.0f}㎡ 参考月租",
                    ),
                )
                stats["quotes"] += 1
            if priced:
                stats["villages_priced"] += 1

        # ---------------- 街道汇总
        conn.execute(
            """
            INSERT INTO streets (district, street, n_community, n_village)
            SELECT c.district, c.street,
                   SUM(CASE WHEN c.kind = 'community' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN c.kind = 'village' THEN 1 ELSE 0 END)
              FROM (SELECT district, street, 'community' AS kind FROM communities
                    UNION ALL
                    SELECT district, street, 'village' AS kind FROM villages) c
             GROUP BY c.district, c.street
            """
        )

        # ---------------- meta
        meta = {
            "city": CITY,
            "source_name": SOURCE_NAME,
            "source_url": SOURCE_URL,
            "baseline_year": str(BASELINE_YEAR),
            "market_adjust": str(MARKET_ADJUST),
            "market_adjust_note": MARKET_ADJUST_NOTE,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "n_communities": str(stats["communities_ok"]),
            "n_villages": str(stats["villages_ok"]),
            "n_quotes": str(stats["quotes"]),
            "n_streets": str(conn.execute("SELECT COUNT(*) FROM streets").fetchone()[0]),
        }
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", list(meta.items()))
        conn.commit()

        stats["streets"] = int(meta["n_streets"])
        if verbose:
            _print_build_report(stats)
        return stats
    finally:
        conn.close()


def _print_build_report(stats: dict) -> None:
    print("=" * 62)
    print("建库完成")
    print("=" * 62)
    print(f"  小区      原始 {stats['communities_in']} → 入库 {stats['communities_ok']}"
          f"（有可算单价 {stats['communities_priced']}）")
    print(f"            住宅 {stats.get('residential', 0)}｜商办公寓 {stats.get('commercial', 0)}")
    print(f"  城中村    原始 {stats['villages_in']} → 入库 {stats['villages_ok']}"
          f"（有报价 {stats['villages_priced']}）")
    print(f"  统一报价  {stats['quotes']} 行（含原值与校准值）")
    print(f"  街道      {stats['streets']} 个（区+街道）")
    print(f"  市场修正  原值 × {MARKET_ADJUST}")


def report(db_path: str | Path) -> None:
    """数据体检：这一步的输出是要给人看的。"""
    conn = connect(db_path)
    try:
        print("\n" + "=" * 62)
        print("数据体检")
        print("=" * 62)

        for label, sql in (
            ("小区合计", "SELECT COUNT(*) FROM communities"),
            ("  住宅", "SELECT COUNT(*) FROM communities WHERE property_type='residential'"),
            ("  商办公寓", "SELECT COUNT(*) FROM communities WHERE property_type='commercial'"),
            ("城中村", "SELECT COUNT(*) FROM villages"),
            ("报价行", "SELECT COUNT(*) FROM rent_quotes"),
            ("街道", "SELECT COUNT(*) FROM streets"),
        ):
            print(f"  {label:<12}{conn.execute(sql).fetchone()[0]:>6}")

        print("\n各户型报价覆盖（仅住宅小区 + 城中村）")
        print(f"  {'户型':<10}{'面积':>6}{'来源':>10}{'小区':>7}{'城中村':>8}{'中位月租(校准)':>14}")
        for layout in LAYOUTS:
            n_com = conn.execute(
                """
                SELECT COUNT(*) FROM rent_quotes q
                  JOIN communities c ON c.id = q.ref_id
                 WHERE q.kind = 'community' AND q.layout = ?
                   AND c.property_type = 'residential'
                """,
                (layout.code,),
            ).fetchone()[0]
            n_vil = conn.execute(
                "SELECT COUNT(*) FROM rent_quotes WHERE kind='village' AND layout=?",
                (layout.code,),
            ).fetchone()[0]
            med = conn.execute(
                "SELECT monthly_rent_adj FROM rent_quotes WHERE layout=? "
                "ORDER BY monthly_rent_adj LIMIT 1 OFFSET "
                "(SELECT COUNT(*)/2 FROM rent_quotes WHERE layout=?)",
                (layout.code, layout.code),
            ).fetchone()
            med_text = f"{med[0]:,.0f}" if med else "—"
            print(f"  {layout.label:<10}{layout.area_sqm:>5.0f}㎡{layout.area_source:>10}"
                  f"{n_com:>7}{n_vil:>8}{med_text:>14}")

        print("\n一房一厅（校准后，住宅类）：按区中位")
        rows = conn.execute(
            """
            SELECT district, AVG(monthly_rent_adj) AS avg
              FROM rent_quotes q
             WHERE layout='one_bed' AND kind='community'
               AND ref_id IN (SELECT id FROM communities WHERE property_type='residential')
             GROUP BY district ORDER BY avg DESC
            """
        ).fetchall()
        for r in rows:
            print(f"  {r['district']:<8}{r['avg']:>9,.0f} 元")

        print("\n复式只存在于小区（预期城中村 = 0）")
        n = conn.execute(
            "SELECT COUNT(*) FROM rent_quotes WHERE layout='duplex' AND kind='village'"
        ).fetchone()[0]
        print(f"  城中村复式报价行 = {n}  {'✅' if n == 0 else '❌'}")
    finally:
        conn.close()
