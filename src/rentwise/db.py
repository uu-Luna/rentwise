"""SQLite 存储层。

设计要点：

1. **两张原始表分开存。** 小区和城中村的价格口径完全不同（一个是单价，一个是分户型总额），
   强行塞进一张表会丢信息。统一放在 `rent_quotes` 里做视图层的合并。

2. **`rent_quotes` 是"预计算"的。** 它把每个房源 × 每种户型算成一行月租。
   4500 个房源 × 5 种户型 = 2 万多行，一次性算好，查询时不用再乘面积。
   这样界面里切换户型是纯查询，不会每次重算。

3. **坐标字段预留但先留空。** 官方数据没有坐标，Step 2 用高德补齐后再回填。
   留空而不是随便填个街道质心，是为了避免"假坐标"混进后续计算。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 住宅小区（商品房 / 统建楼 / 商务公寓）
CREATE TABLE IF NOT EXISTS communities (
    id        INTEGER PRIMARY KEY,
    district  TEXT NOT NULL,
    street    TEXT NOT NULL,
    name      TEXT NOT NULL,
    rate_high  REAL,           -- 高层单价 元/㎡/月
    rate_multi REAL,           -- 多层单价 元/㎡/月
    rate_used  REAL,           -- 实际采用的单价
    rate_basis TEXT,           -- 'high' | 'multi' | NULL
    property_type TEXT,        -- 'residential' | 'commercial'，按名称规则推断
    lat REAL, lng REAL,        -- Step 2 用高德 POI 检索补
    geo_name   TEXT,           -- 高德返回的 POI 名称（可能和我们的写法不同）
    geo_status TEXT,           -- ok | low_match | not_found | error
    geo_score  REAL,           -- 名称相似度
    geo_at     TEXT,           -- 补坐标的时间
    UNIQUE (district, street, name)
);
CREATE INDEX IF NOT EXISTS idx_comm_district ON communities(district);

-- 城中村私宅：直接给四档户型的月租总额
CREATE TABLE IF NOT EXISTS villages (
    id        INTEGER PRIMARY KEY,
    district  TEXT NOT NULL,
    street    TEXT NOT NULL,
    name      TEXT NOT NULL,
    address   TEXT,
    rent_single     REAL,      -- 单间 19㎡
    rent_one        REAL,      -- 一房一厅 35㎡
    rent_two        REAL,      -- 两房 60㎡
    rent_multi_room REAL,      -- 三房及以上 85㎡
    lat REAL, lng REAL,
    UNIQUE (district, street, name)
);
CREATE INDEX IF NOT EXISTS idx_vil_district ON villages(district);

-- 统一报价：房源 × 户型 → 月租（预计算）
CREATE TABLE IF NOT EXISTS rent_quotes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('community', 'village')),
    ref_id      INTEGER NOT NULL,
    district    TEXT NOT NULL,
    street      TEXT NOT NULL,
    name        TEXT NOT NULL,
    layout      TEXT NOT NULL,
    area_sqm    REAL NOT NULL,
    area_source TEXT,                  -- measured | policy | estimated
    monthly_rent REAL,                 -- 政府参考价直接换算的原值
    monthly_rent_adj REAL,             -- 乘市场修正系数后的值
    rate_per_sqm REAL,
    basis_note  TEXT,
    UNIQUE (kind, ref_id, layout)
);
CREATE INDEX IF NOT EXISTS idx_quote_layout ON rent_quotes(layout);
CREATE INDEX IF NOT EXISTS idx_quote_district ON rent_quotes(district, layout);
CREATE INDEX IF NOT EXISTS idx_quote_street ON rent_quotes(district, street);

-- 街道汇总：Step 2 用它做"粗筛"，避免对 4500 个点全部调高德
CREATE TABLE IF NOT EXISTS streets (
    district  TEXT NOT NULL,
    street    TEXT NOT NULL,
    n_community INTEGER NOT NULL DEFAULT 0,
    n_village   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (district, street)
);
"""


def connect(db_path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
