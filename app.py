"""RentWise · 深圳租房真实成本计算器（Streamlit 界面）。

产品逻辑一句话：
    输入公司地址 → 算出每个候选住处的「租金 + 交通费 (+ 时间成本)」→ 输出四套方案

设计上的几个决定：
1. **时间单价不预设**。但输入方式给人用的：可以填月薪（自动换算时薪），也可以直接填时薪。
   深圳人算工资是按月的，让他自己换算时薪是不友好的。
2. **通勤时间按"每月小时数"展示**，不是"单程 35 分钟"。后者没有冲击力。
3. **成本要拆开看**：租金、交通费、时间成本三段堆叠，一眼看出钱花在哪。
4. **地图**：候选撒在地图上，方位感比表格强得多。

跑法：streamlit run app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
import streamlit as st

from rentwise import commute as commute_mod
from rentwise import query
from rentwise.config import DB_PATH, LAYOUTS, MARKET_ADJUST
from rentwise.cost import PLANS, Option, build_plans, pick_tradeoff_pair, time_tradeoff
from rentwise.db import connect

st.set_page_config(page_title="RentWise · 深圳租房真实成本", page_icon="🏠", layout="wide")

MODES = {"地铁 / 公交": "transit", "驾车": "driving", "骑行 / 电动车": "biking"}
PLAN_TITLES = {p.code: p for p in PLANS}
DEFAULT_MAX_MINUTES = 45
WORKDAYS_STATUTORY = 21.75   # 中国法定月计薪天数
HOURS_PER_DAY = 8

CSS = """
<style>
  .block-container { padding-top: 2.2rem; max-width: 1280px; }
  #MainMenu, footer { visibility: hidden; }
  .rw-hero {
      background: linear-gradient(120deg, #0f172a 0%, #1e3a8a 55%, #0ea5e9 100%);
      border-radius: 20px; padding: 34px 38px; color: #fff; margin-bottom: 22px;
      box-shadow: 0 18px 40px -18px rgba(15, 23, 42, .55);
  }
  .rw-hero h1 { font-size: 2.05rem; margin: 0 0 6px 0; font-weight: 700; letter-spacing: .5px; }
  .rw-hero p { margin: 0; opacity: .88; font-size: 1rem; line-height: 1.6; }
  .rw-hero .tag {
      display: inline-block; margin-top: 14px; padding: 4px 12px; border-radius: 999px;
      background: rgba(255,255,255,.14); font-size: .82rem; letter-spacing: .4px;
  }
  .rw-card {
      border: 1px solid #e6eaf2; border-radius: 16px; padding: 18px 20px;
      background: #fff; box-shadow: 0 6px 18px -12px rgba(15,23,42,.28);
      height: 100%;
  }
  .rw-card.hl { border: 1.5px solid #2563eb; background: #f7faff; }
  .rw-rank {
      display: inline-flex; align-items: center; justify-content: center;
      width: 26px; height: 26px; border-radius: 9px; background: #2563eb; color: #fff;
      font-size: .82rem; font-weight: 700; margin-right: 8px;
  }
  .rw-rank.gray { background: #cbd5e1; }
  .rw-name { font-weight: 700; font-size: 1.06rem; color: #0f172a; }
  .rw-sub { color: #64748b; font-size: .82rem; margin: 2px 0 12px 34px; }
  .rw-num { font-size: 1.55rem; font-weight: 700; color: #0f172a; letter-spacing: -.5px; }
  .rw-num small { font-size: .8rem; font-weight: 500; color: #64748b; margin-left: 2px; }
  .rw-kv { color: #475569; font-size: .84rem; line-height: 1.75; }
  .rw-kv b { color: #0f172a; font-weight: 600; }
  .rw-badge {
      float: right; font-size: .72rem; padding: 2px 9px; border-radius: 999px;
      background: #dbeafe; color: #1d4ed8; font-weight: 600;
  }
  .rw-concl {
      border-left: 5px solid #2563eb; background: #f8fafc; border-radius: 12px;
      padding: 18px 22px; font-size: 1.02rem; line-height: 1.75; color: #0f172a;
  }
  div[data-testid="stMetricValue"] { font-size: 1.5rem; font-weight: 700; color: #0f172a; }
  div[data-testid="stMetricLabel"] { font-size: .8rem; color: #64748b; }
</style>
"""


@st.cache_resource(show_spinner=False)
def get_conn(path: str):
    return connect(path, check_same_thread=False)


def money(value: float | None) -> str:
    return "—" if value is None else f"¥{value:,.0f}"


def hero() -> None:
    st.markdown(
        """
        <div class="rw-hero">
          <h1>🏠 RentWise · 真实租房成本</h1>
          <p>房租 3,500 的房子，可能比房租 2,800 的更便宜。<br>
             输入公司地址，把租金、通勤时间、交通费一起算成一个月度数。</p>
          <span class="tag">深圳 · 政府参考价 × 高德实时通勤</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def compute(address: str, layout: str, radius: int, mode: str) -> dict:
    point = query.resolve_address(address)
    if point is None:
        return {"error": "地址解析失败。试试更具体的写法，例如「深圳湾科技生态园」「福田区车公庙」。"}

    conn = get_conn(str(DB_PATH))
    candidates = query.find_candidates(
        conn, point[0], point[1], layout, radius_m=radius, max_pages=5, limit=45
    )
    if not candidates:
        return {"error": "周边没有匹配到有租金数据的住宅。换个地址、加大搜索半径，或换个户型试试。"}

    candidates = commute_mod.apply_to_candidates(
        conn, point, candidates, primary_mode=mode, extra_limit=10
    )
    return {"point": point, "candidates": candidates}


def to_options(candidates, mode: str) -> list[Option]:
    options: list[Option] = []
    for candidate in candidates:
        c = candidate.commute
        if c is None:
            continue
        minutes = c.minutes(mode)
        if minutes is None:
            continue
        options.append(
            Option(
                kind=candidate.kind,
                district=candidate.district,
                name=candidate.name,
                layout=candidate.layout,
                rent=candidate.rent,
                minutes_one_way=minutes,
                transport_fee=c.monthly_fee(mode),
                mode=mode,
                extra={"distance_km": candidate.distance_km, "commute": c,
                       "lat": candidate.lat, "lng": candidate.lng},
            )
        )
    return options


# ------------------------------------------------------------------ 渲染部件
def render_cards(plan_code: str, rows) -> None:
    plan = PLAN_TITLES[plan_code]
    st.markdown(f"**{plan.code} · {plan.title}**　<span style='color:#64748b'>{plan.subtitle}</span>",
                unsafe_allow_html=True)

    for index, item in enumerate(rows):
        c = item.cost
        o = item.option
        badge = '<span class="rw-badge">首选</span>' if index == 0 else ""
        cls = "rw-card hl" if index == 0 else "rw-card"
        rank_cls = "" if index == 0 else " gray"
        kind = "城中村" if o.kind == "village" else "小区"

        extra_kv = ""
        if c.has_time_value:
            extra_kv = (
                f"时间成本 <b>{money(c.time_cost)}</b> · "
                f"总成本 <b>{money(c.real_cost)}</b><br>"
            )

        st.markdown(
            f"""
            <div class="{cls}" style="margin-bottom:12px">
              {badge}
              <div>
                <span class="rw-rank{rank_cls}">{index + 1}</span>
                <span class="rw-name">{o.name}</span>
              </div>
              <div class="rw-sub">{o.district} · {kind} · 直线距离 {o.extra.get('distance_km', 0):.1f} km</div>
              <div class="rw-num">{money(c.cash_cost)}<small>/月 现金成本</small></div>
              <div class="rw-kv" style="margin-top:10px">
                月租 <b>{money(c.rent)}</b> · 单程 <b>{o.minutes_one_way:.0f} 分钟</b>
                （每月 <b>{c.hours_per_month:.0f}</b> 小时在路上）<br>
                交通费 <b>{money(c.transport_fee)}</b> · {extra_kv}
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_cost_chart(rows, hourly_value: float | None) -> None:
    """成本拆解：租金 / 交通费 / 时间成本 堆叠条形图。"""
    records = []
    for item in rows:
        c = item.cost
        records.append({"小区": item.option.name, "成本项": "租金", "金额": c.rent})
        records.append({"小区": item.option.name, "成本项": "交通费", "金额": c.transport_fee})
        if c.has_time_value:
            records.append({"小区": item.option.name, "成本项": "时间成本", "金额": c.time_cost or 0})

    if not records:
        return

    frame = pd.DataFrame(records)
    import altair as alt

    order = [item.option.name for item in rows][::-1]
    chart = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=4, size=22)
        .encode(
            y=alt.Y("小区:N", sort=order, title=None),
            x=alt.X("金额:Q", title="元 / 月", stack="zero"),
            color=alt.Color(
                "成本项:N",
                scale=alt.Scale(
                    domain=["租金", "交通费", "时间成本"],
                    range=["#2563eb", "#38bdf8", "#f59e0b"],
                ),
                legend=alt.Legend(orient="top", title=None),
            ),
            tooltip=["小区", "成本项", alt.Tooltip("金额:Q", format=",.0f")],
        )
        .properties(height=max(180, 46 * len(rows)))
        .configure_view(stroke=None)
    )
    st.altair_chart(chart, width="stretch")


def render_map(options, point) -> None:
    try:
        import pydeck as pdk
    except ImportError:
        return

    rents = [o.rent for o in options]
    low, high = min(rents), max(rents)

    def color(rent: float):
        ratio = 0.0 if high == low else (rent - low) / (high - low)
        return [int(37 + ratio * 200), int(99 + (1 - ratio) * 120), int(235 - ratio * 170), 200]

    homes = pd.DataFrame(
        {
            "name": [o.name for o in options],
            "lat": [o.extra["lat"] for o in options],
            "lng": [o.extra["lng"] for o in options],
            "rent": rents,
            "color": [color(r) for r in rents],
        }
    )
    dest = pd.DataFrame({"lat": [point[0]], "lng": [point[1]], "name": ["公司" ]})

    layers = [
        pdk.Layer(
            "ScatterplotLayer",
            homes,
            get_position=["lng", "lat"],
            get_radius=220,
            get_fill_color="color",
            pickable=True,
            radius_min_pixels=6,
            radius_max_pixels=22,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            dest,
            get_position=["lng", "lat"],
            get_radius=380,
            get_fill_color=[239, 68, 68, 230],
            pickable=True,
            radius_min_pixels=10,
        ),
    ]
    view = pdk.ViewState(latitude=point[0], longitude=point[1], zoom=12.2, pitch=0)
    st.pydeck_chart(
        pdk.Deck(
            layers=layers,
            initial_view_state=view,
            map_style="light",
            tooltip={"text": "{name}\n月租 ¥{rent}"},
        )
    )
    st.caption("红点＝公司；蓝点＝候选住处，颜色越暖租金越高。点击可看名称与月租。")


# ------------------------------------------------------------------ 主流程
def main() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    hero()

    conn = get_conn(str(DB_PATH))

    # 空库保护：connect() 会自动建表，所以库里没数据时不会报错，
    # 只会显示「中位租金 ¥0 / 没有候选」，非常容易被误认为是程序坏了。
    n_quotes = conn.execute("SELECT COUNT(*) FROM rent_quotes").fetchone()[0]
    if n_quotes == 0:
        st.error(
            f"**数据库是空的**（{DB_PATH}）。\n\n"
            "先运行下面两条命令：\n\n"
            "```bash\n"
            "python scripts/fetch_reference_prices.py\n"
            "python scripts/build_db.py\n"
            "```\n\n"
            "如果你是在别处复制了一份项目，注意数据库不会跟着复制——"
            "`data/` 目录在 `.gitignore` 里。"
        )
        return

    layout_labels = [f"{lay.label}（{lay.area_sqm:.0f}㎡）" for lay in LAYOUTS]

    with st.sidebar:
        st.header("你的情况")
        address = st.text_input("公司 / 常去地址", "深圳湾科技生态园")
        layout_label = st.selectbox("户型", layout_labels, index=1)
        layout = LAYOUTS[layout_labels.index(layout_label)].code
        mode_label = st.radio("通勤方式", list(MODES))
        mode = MODES[mode_label]

        st.divider()
        stat_com = query.rent_stats(conn, layout, kind="community")
        stat_vil = query.rent_stats(conn, layout, kind="village")
        budget = st.number_input(
            "月租预算（0 = 不限）", 0, 50000, 0, step=100, key=f"budget_{layout}",
            help="不预设预算：预算是个人约束，没有「正确」的数值。下面给出市场参照。",
        )
        st.caption(
            f"全市中位租金（{layout_label}）："
            f"小区 ¥{stat_com['median'] or 0:,.0f} ｜ 城中村 ¥{stat_vil['median'] or 0:,.0f}"
        )
        max_minutes = st.slider("单程最长通勤（分钟）", 10, 120, DEFAULT_MAX_MINUTES, step=5)

        st.divider()
        st.markdown("**你的时间值多少钱？**")
        wage_mode = st.radio(
            "输入方式", ["不计算时间成本", "按月薪换算", "直接填时薪"],
            label_visibility="collapsed",
        )
        hourly_value: float | None = None
        if wage_mode == "按月薪换算":
            monthly = st.number_input("月薪（税前）", 0, 300000, 10000, step=500)
            hourly_value = round(monthly / WORKDAYS_STATUTORY / HOURS_PER_DAY, 1)
            st.caption(
                f"→ 时薪 ¥{hourly_value:,.1f}（月薪 ÷ {WORKDAYS_STATUTORY} 天 ÷ {HOURS_PER_DAY} 小时）"
            )
        elif wage_mode == "直接填时薪":
            hourly_value = st.number_input("时薪（元/小时）", 0, 2000, 60, step=10) or None

        st.divider()
        radius = st.select_slider("搜索半径", [2000, 3000, 4000, 5000, 6000], value=4000)
        exclude_village = st.checkbox("排除城中村", value=False)
        metro_only = st.checkbox("只看有真实公交/地铁方案", value=False)

        go = st.button("开始计算", type="primary", width="stretch")

    if go:
        with st.spinner("正在解析地址、搜索周边小区、计算通勤…（首次约 20~40 秒）"):
            st.session_state["result"] = compute(address, layout, radius, mode)

    result = st.session_state.get("result")
    if not result:
        st.info("在左侧填好地址，点「开始计算」。")
        return
    if "error" in result:
        st.error(result["error"])
        return

    options = to_options(result["candidates"], mode)
    if exclude_village:
        options = [o for o in options if o.kind != "village"]
    if metro_only:
        options = [
            o for o in options
            if o.extra.get("commute") is not None and not o.extra["commute"].transit_estimated
        ]
    options = [o for o in options if o.minutes_one_way <= max_minutes]

    if not options:
        st.warning("按当前条件筛选后没有候选了。放宽通勤时间或取消勾选试试。")
        return

    if budget > 0:
        within = [o for o in options if o.rent <= budget]
        if within:
            options = within
        else:
            st.info(
                f"预算 ¥{budget:,.0f} 在该区域没有匹配的{layout_label}，"
                f"最低也要 ¥{min(o.rent for o in options):,.0f}。已显示全部候选。"
            )

    district_median = query.rent_stats(conn, layout, options[0].district).get("median")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("候选数量", f"{len(options)} 个")
    m2.metric("区域租金中位", money(district_median))
    m3.metric("最低月租", money(min(o.rent for o in options)))
    m4.metric("最短通勤", f"{min(o.minutes_one_way for o in options):.0f} 分钟")

    st.write("")
    minutes_list = [o.minutes_one_way for o in options]
    spread = max(minutes_list) - min(minutes_list)
    if spread < 10:
        st.info(
            f"这一带候选的单程通勤都在 {min(minutes_list):.0f}~{max(minutes_list):.0f} 分钟之间，"
            "差别很小 —— 这种情况下**租金是主要变量**。"
            "想比较更远的地方，把搜索半径或通勤上限调大。"
        )
    else:
        top_rent, top_time = pick_tradeoff_pair(options)
        st.markdown(
            f'<div class="rw-concl">💡 {time_tradeoff(top_rent, top_time, hourly_value)}</div>',
            unsafe_allow_html=True,
        )

    st.write("")
    plans = build_plans(options, hourly_value, top_n=4)
    if not plans:
        st.warning("没有可展示的方案。")
        return

    first = plans.get("B") or next(iter(plans.values()))
    st.subheader("成本拆解")
    st.caption("同一批候选，钱花在哪一目了然。仅展示 B · 均衡型前几名。")
    render_cost_chart(first, hourly_value)

    with st.expander("在深圳地图上看这些候选", expanded=False):
        render_map(options, result["point"])

    st.subheader("四套方案")
    tabs = st.tabs([f"{PLAN_TITLES[c].code} · {PLAN_TITLES[c].title}" for c in plans])
    for tab, code in zip(tabs, plans):
        with tab:
            render_cards(code, plans[code])
            st.code("\n".join(item.option.name for item in plans[code]), language=None)

    st.divider()
    with st.expander("这些数字从哪来（很重要）"):
        st.markdown(
            f"""
- **租金**：深圳市住建局《房屋租赁参考价格》（2022 年度）+ 市场修正 ×{MARKET_ADJUST}。
  实测验证：住宅类挂牌价与政府参考价秩相关 **0.943**，水平差 4.5%。
- **通勤时间**：高德路径规划。驾车分早高峰（8:30）与平峰（14:00）；骑行按电动车折算 ×0.75（近似）。
- **交通费**：地铁用接口返回的真实票价；驾车按每公里 0.75 元估；骑行不计。
- **时间价值**：按月薪换算时用「月薪 ÷ 21.75 天 ÷ 8 小时」，这是法定计薪口径，不代表你真实的时间偏好。
- **户型面积**：一房一厅 43㎡ 来自实测；其他户型暂用政府标准面积。

**局限**：每月按 22 个工作日计；租金是参考价不是挂牌价；通勤不含等车与体力成本。
            """
        )


if __name__ == "__main__":
    main()
