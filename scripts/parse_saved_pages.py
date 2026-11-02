"""解析「自己保存的」链家/贝壳页面，提取在租房源。

为什么是这种形式
----------------
本脚本**不发起任何网络请求**。它只读用户自己用浏览器保存下来的页面文件
（.mhtml 单文件网页，或 .html）。这个区别很重要：

- 不是爬虫：没有绕过登录、没有批量请求、没有访问用户看不到的东西
- 数据是用户正常浏览时能看到的公开信息
- 保存下来的文件本身就是数据快照，可复现、可核对

支持的保存格式
--------------
1. Chrome/Edge「网页，单个文件」→ .mhtml（MHTML，内部是 MIME 多部件）
2. 「网页，仅 HTML」→ .html

用法
----
    python scripts/parse_saved_pages.py
    python scripts/parse_saved_pages.py --pages data/manual/pages --out data/manual/parsed_listings.csv
"""

from __future__ import annotations

import argparse
import csv
import email
import email.policy
import html as htmllib
import re
import statistics as st
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES_DIR = ROOT / "data" / "manual" / "pages"
OUT_CSV = ROOT / "data" / "manual" / "parsed_listings.csv"

# 只要这类房源：一房一厅。
# 面积区间放宽到 25~65㎡：深圳商品房的一房一厅普遍 45~55㎡，
# 只有城中村才严格按 35㎡ 标准户型。这一点直接影响租金换算，别写死 35。
ROOM_RE = re.compile(r"(\d)\s*室\s*(\d)\s*厅")
AREA_MIN, AREA_MAX = 25.0, 65.0
AREA_RE = re.compile(r"([\d.]+)\s*㎡")
PRICE_RE = re.compile(r"<em>\s*([\d,]+)\s*</em>\s*元/月")
COMMUNITY_RE = re.compile(r'<a\s+title="([^"]+)"\s+href="[^"]*zufang/c\d+')
DISTRICT_RE = re.compile(r'zufang/([a-z]+)/">([^<]+区)</a>-<a[^>]*>([^<]+)</a>')
TOTALPAGE_RE = re.compile(r'data-totalpage="(\d+)"')
ITEM_SPLIT = re.compile(r'<div class="content__list--item"[^>]*>')


@dataclass
class Listing:
    community: str
    district: str
    sub_area: str
    title: str
    area_sqm: float | None
    rooms: str
    rent: int | None
    source_url: str
    source_file: str

    @property
    def unit_rate(self) -> float | None:
        if self.area_sqm and self.rent:
            return round(self.rent / self.area_sqm, 2)
        return None


def decode_page(path: Path) -> tuple[str, str]:
    """返回 (html, content_location)。同时支持 mhtml 和 html。"""
    raw = path.read_bytes()
    if path.suffix.lower() in {".mhtml", ".mht"}:
        message = email.message_from_bytes(raw, policy=email.policy.default)
        for part in message.walk():
            if part.get_content_type() == "text/html":
                body = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                return body.decode(charset, "replace"), str(part.get("Content-Location") or "")
        return "", ""
    for encoding in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(encoding), ""
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), ""


def strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", htmllib.unescape(text)).strip()


def parse_page(path: Path) -> tuple[list[Listing], int]:
    html, location = decode_page(path)
    if not html:
        return [], 0

    total_pages = int(TOTALPAGE_RE.search(html).group(1)) if TOTALPAGE_RE.search(html) else 1
    # 房源块的真实标签带一堆 data-* 属性，所以不能写死 class 后面紧跟 '>'
    chunks = ITEM_SPLIT.split(html)[1:]

    listings: list[Listing] = []
    for chunk in chunks:
        head = chunk  # split 已保证不会跨到下一条房源

        price_match = PRICE_RE.search(head)
        if not price_match:
            continue
        rent = int(price_match.group(1).replace(",", ""))

        # 标题：<p class="...--title"> 里的第一个 <a> 文本
        title_match = re.search(
            r'<p[^>]*content__list--item--title[^>]*>\s*<a[^>]*>(.*?)</a>', head, re.S
        )
        title = strip_tags(title_match.group(1)) if title_match else ""

        community_match = COMMUNITY_RE.search(head)
        community = community_match.group(1) if community_match else ""

        des_match = re.search(r'class="content__list--item--des">(.*?)</p>', head, re.S)
        des = strip_tags(des_match.group(0)) if des_match else ""

        area_match = AREA_RE.search(des) or AREA_RE.search(title)
        area = float(area_match.group(1)) if area_match else None

        room_match = ROOM_RE.search(des) or ROOM_RE.search(title)
        rooms = f"{room_match.group(1)}室{room_match.group(2)}厅" if room_match else ""

        district, sub_area = "", ""
        # strip_tags 会把标签替换成空格，所以文本是「南山区 - 后海 - 京光海景花园」，
        # 连字符两侧带空格。用 \s*-\s* 容忍。
        district_match = re.search(r"([^\s\-]+?区)\s*-\s*([^\-\s/]+)\s*-", des)
        if district_match:
            district, sub_area = district_match.group(1), district_match.group(2)

        listings.append(
            Listing(
                community=community,
                district=district,
                sub_area=sub_area,
                title=title,
                area_sqm=area,
                rooms=rooms,
                rent=rent,
                source_url=location,
                source_file=path.name,
            )
        )

    return listings, total_pages


def is_one_bedroom(item: Listing) -> tuple[bool, str]:
    """一房一厅判定。返回 (是否符合, 原因)。"""
    if item.rooms != "1室1厅":
        return False, f"户型为 {item.rooms or '未知'}"
    if item.area_sqm is None:
        return False, "面积缺失"
    if not (AREA_MIN <= item.area_sqm <= AREA_MAX):
        return False, f"面积 {item.area_sqm}㎡ 超出 {AREA_MIN:.0f}~{AREA_MAX:.0f} 范围"
    return True, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解析已保存的租房页面")
    parser.add_argument("--pages", default=str(PAGES_DIR))
    parser.add_argument("--out", default=str(OUT_CSV))
    parser.add_argument("--all-rooms", action="store_true", help="不过滤户型，全部输出")
    args = parser.parse_args(argv)

    pages_dir = Path(args.pages)
    files = sorted(
        [p for p in pages_dir.glob("*") if p.suffix.lower() in {".mhtml", ".mht", ".html", ".htm"}]
    )
    if not files:
        print(f"没有找到页面文件：{pages_dir}")
        print("把浏览器保存的 .mhtml 放进这个目录再跑。")
        return 1

    all_rows: list[dict] = []
    print("=" * 68)
    print(f"解析 {len(files)} 个页面文件")
    print("=" * 68)

    for path in files:
        listings, total_pages = parse_page(path)
        kept, rejected = [], 0
        for item in listings:
            ok, _reason = (True, "") if args.all_rooms else is_one_bedroom(item)
            if ok:
                kept.append(item)
            else:
                rejected += 1

        print(f"\n{path.name[:60]}")
        print(f"  房源条数 {len(listings)}｜符合一房一厅 {len(kept)}｜其他户型/面积被排除 {rejected}"
              f"｜该小区共 {total_pages} 页")

        if kept:
            rents = [x.rent for x in kept if x.rent]
            rates = [x.unit_rate for x in kept if x.unit_rate]
            for x in kept:
                print(f"    {x.community or '(未识别)'}  {x.area_sqm:>5.1f}㎡  "
                      f"{x.rent:>6,} 元/月  单价 {x.unit_rate:>6.1f} 元/㎡")
            print(f"    → 中位月租 {st.median(rents):,.0f} 元｜中位单价 {st.median(rates):.1f} 元/㎡")

        for x in kept:
            all_rows.append(
                {
                    "小区": x.community,
                    "区": x.district,
                    "片区": x.sub_area,
                    "户型": x.rooms,
                    "面积㎡": x.area_sqm,
                    "月租": x.rent,
                    "单价_元每平每月": x.unit_rate,
                    "来源文件": x.source_file,
                    "来源URL": x.source_url,
                }
            )

    if not all_rows:
        print("\n没有解析出任何符合条件的一房一厅房源。")
        print("可能是：页面保存不完整、筛选条件不是一室、或该小区没有一房一厅在租。")
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(all_rows[0]))
        writer.writeheader()
        writer.writerows(all_rows)

    print("\n" + "=" * 68)
    print(f"合计 {len(all_rows)} 条房源，已写入 {out}")

    by_community: dict[str, list[dict]] = {}
    for row in all_rows:
        by_community.setdefault(row["小区"], []).append(row)

    print(f"\n按小区汇总（{len(by_community)} 个小区）")
    print(f"  {'小区':<24}{'条数':>5}{'中位月租':>10}{'中位单价':>10}")
    for name, rows in sorted(by_community.items()):
        rents = [r["月租"] for r in rows if r["月租"]]
        rates = [r["单价_元每平每月"] for r in rows if r["单价_元每平每月"]]
        print(f"  {name or '(未识别)':<24}{len(rows):>5}{st.median(rents):>10,.0f}{st.median(rates):>10.1f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
