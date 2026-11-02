"""给小区补经纬度（POI 检索）。

    python scripts/geocode_communities.py --limit 30          # 先跑 30 个看质量
    python scripts/geocode_communities.py --all               # 全量（约 1 小时）
    python scripts/geocode_communities.py --district 南山区    # 只补一个区

设计取舍
--------
- **只补住宅类**：商办公寓不参与推荐，没必要花配额。
- **限速**：默认每次请求间隔 0.6 秒。个人 Key 的 QPS 很低，
  跑快了会大量返回 CUQPS 限流，反而更慢。
- **可续跑**：已经拿到坐标的跳过；失败的记录 geo_status，可以重跑。
- **不猜**：名称相似度不够就标 low_match，不硬塞一个坐标。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rentwise import amap  # noqa: E402
from rentwise.config import DB_PATH  # noqa: E402
from rentwise.db import connect  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给小区补经纬度")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--all", action="store_true", help="不限数量，跑完为止")
    parser.add_argument("--district", default=None, help="只处理某个区")
    parser.add_argument("--delay", type=float, default=0.6, help="每次请求间隔秒数")
    parser.add_argument("--min-score", type=float, default=0.80)
    parser.add_argument("--include-commercial", action="store_true")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        sql = """
            SELECT id, district, street, name
              FROM communities
             WHERE lat IS NULL
               AND (geo_status IS NULL OR geo_status IN ('error', 'low_match'))
        """
        params: list[object] = []
        if not args.include_commercial:
            sql += " AND property_type = 'residential'"
        if args.district:
            sql += " AND district = ?"
            params.append(args.district)
        sql += " ORDER BY id"
        if not args.all:
            sql += f" LIMIT {int(args.limit)}"

        rows = conn.execute(sql, params).fetchall()
        if not rows:
            print("没有待处理的小区。")
            return 0

        print(f"待处理 {len(rows)} 个小区，请求间隔 {args.delay}s")
        print("=" * 70)

        stats = {"ok": 0, "low_match": 0, "not_found": 0, "error": 0}
        started = time.time()
        consecutive_network_failures = 0

        for index, row in enumerate(rows, 1):
            try:
                # 注意：region 传「南山区」会返回 0 条（高德把区名当城市级解析，深圳南山区命中失败），
                # 但传「罗湖区」「龙岗区」又能用。为了稳定，统一传深圳市 adcode，
                # 再用返回的 adname 做区一致性校验。
                pois = amap.search_poi(row["name"], region="440300")
                best, score = amap.pick_best(
                    row["name"], row["district"], pois, min_score=args.min_score
                )
            except amap.NetworkUnstable as exc:
                # 网络层面的问题：不计入数据错误，连续多次就整体中止，
                # 避免像上次那样一口气刷出 1400 条假失败。
                consecutive_network_failures += 1
                print(f"  [{index}/{len(rows)}] 网络不稳定（{consecutive_network_failures} 连败）：{exc}")
                if consecutive_network_failures >= 8:
                    print("\n连续 8 次网络失败，判定为环境故障，中止本次任务。")
                    print("已处理的进度都已提交，网络恢复后重跑即可续上。")
                    break
                time.sleep(5.0)
                continue
            except amap.AmapError as exc:
                # 真实的接口错误（参数错、Key 失效等），记下来但不中止整批
                stats["error"] += 1
                conn.execute(
                    "UPDATE communities SET geo_status='error', geo_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), row["id"]),
                )
                conn.commit()
                print(f"  [{index}/{len(rows)}] {row['name'][:18]:<20} 错误：{exc}")
                time.sleep(args.delay)
                continue

            consecutive_network_failures = 0

            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if best is None:
                status = "not_found" if not pois else "low_match"
                stats[status] += 1
                conn.execute(
                    "UPDATE communities SET geo_status=?, geo_score=?, geo_at=? WHERE id=?",
                    (status, round(score, 3), now, row["id"]),
                )
                hint = f"（最高匹配 {pois[0].name if pois else '无结果'} {score:.2f}）"
                print(f"  [{index}/{len(rows)}] {row['name'][:18]:<20} {status} {hint}")
            else:
                stats["ok"] += 1
                conn.execute(
                    """
                    UPDATE communities
                       SET lat=?, lng=?, geo_name=?, geo_status='ok', geo_score=?, geo_at=?
                     WHERE id=?
                    """,
                    (best.lat, best.lng, best.name, round(score, 3), now, row["id"]),
                )
                print(f"  [{index}/{len(rows)}] {row['name'][:18]:<20} ok  "
                      f"{best.name[:16]:<18}{best.lat:.5f},{best.lng:.5f}  score={score:.2f}")
            conn.commit()
            time.sleep(args.delay)

        elapsed = time.time() - started
        print("=" * 70)
        print(f"完成 {len(rows)} 个，用时 {elapsed:.0f}s")
        for key, value in stats.items():
            print(f"  {key:<10}{value}")
        if len(rows):
            print(f"  成功率    {stats['ok'] / len(rows) * 100:.1f}%")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
