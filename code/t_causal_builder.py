#!/usr/bin/env python3
"""
t_causal_builder.py — 全历史因果 T（研究数据根基）

与 t_daily_builder + t_structural_builder 的算法一致，但把四处 as-of-today
污染全部改成 as-of-t，并把覆盖从 250 天扩到 ohlcv_cache 的全长：

  ①  价格源   history/price（前复权, 250日滚动） → ohlcv_cache（未复权, ~805天）
  ②  历史市值 mcap_now × close比值             → shares(t) × close_raw(t)
                                                  shares(t) 由 share_factor 从 xdxr 还原
  ③  A group  今天的 PE_TTM 池追溯套全历史      → 逐日判定：ttm(t) > 0 即入组
              （利润口径 = 扣非归母 DEDUCT_PARENT_NETPROFIT，全 schema 100% 覆盖；
                NOTICE_DATE 缺失时按法定披露截止日推断，偏保守不前视）
  ④  cap      base × R(history[-1]) × anchor    → cap_accessor（按已生效季度, 无 anchor）

━━ 因果契约 ━━
任意日期 t 的 T(t) 只依赖 ≤ t 的信息：
  - close_raw(t) 是 t 当日的真实成交价，不因 t 之后的除权除息而改变
  - shares(t) = shares_now / Π(1+送转/10+配股/10)，只累乘 t 之后的事件
  - ttm(t) 只取 NOTICE_DATE ≤ t 的报告期
  - cap(t) 只取 regime 生效日 ≤ t 的季度
  - H 的 60 日基线 / S_base(t) 是 60 日回看

验收：假装今天是 T₀ 重算，再假装是 T₀+90 天重算，两次结果应逐日相同。

输出: stock_cache/t_causal.json
  daily[]            聚合 T 时序（T/H/H_base/H_adj/S/S_adj/above/total/med_d/
                     n_stocks/heat/cool/cap_shift）
  industries{}       逐行业 PE 时序 {行业: {cap, dates:{日期:{m,p25,p75,p90,n,cap,r}}}}
  semi / optic_comm / optoelec   半导体/光通信/光学光电子的子行业分组，结构同上
  weekly[]           周度聚合（取每周最后一个交易日的 daily 记录 + week 标签）
  weekly_industries{} 每周的逐行业偏离明细 {周末日期: [{ind,pe,cap,d,pos,trend,n,p25,p75,p90}]}

逐行业输出是为了让 t_structural_chart 那几个图表（T结构分析 / 广度偏离 /
行业估值统计：日频·周频 × 全行业·半导体·光通信·光学光电）也能走因果口径，
从而让 t_daily.json + t_structural.json 这条旧链退役、T 体系收敛为单一产物。

用法:
    python3 stock_cache/t_causal_builder.py
    python3 stock_cache/t_causal_builder.py --start 2023-07-04
"""

import argparse
import csv
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
BASE = SCRIPT_DIR.parent
os.chdir(BASE)
sys.path.insert(0, str(SCRIPT_DIR))

from cap_accessor import CapAccessor
from share_factor import ShareFactor

BJT = ZoneInfo("Asia/Shanghai")
OHLCV_DIR = BASE / "ohlcv_cache"
STATEMENTS_DIR = SCRIPT_DIR / "financial" / "statements"
MATRIX_PATH = SCRIPT_DIR / "m_pricing_matrix.json"
OUT_PATH = SCRIPT_DIR / "t_causal.json"
STOCK_VAL_PATH = SCRIPT_DIR / "stock_valuation_causal.json"

W = 60                      # H 自适应 / S_base 回看窗口 = 1 个财报周期
LOOKBACK = 20               # heat/cool 与 trend 的回望天数（行业偏离是在扩张还是收敛）
H_BASE = 0.5
MIN_INDUSTRY_STOCKS = 3
PE_MIN, PE_MAX = 0, 2000


# ── TTM（与 t_daily_builder 同算法，因果） ──────────────────────

def _infer_notice_date(rd):
    y, p = int(rd[:4]), rd[5:10]
    return {"03-31": f"{y}-04-30", "06-30": f"{y}-08-31",
            "09-30": f"{y}-10-31", "12-31": f"{y + 1}-04-30"}.get(p, f"{y}-12-31")


def _ttm_from(rd, by_rd, key="profit"):
    """TTM = 本期累计 + 上年年报 − 上年同期累计，单位亿元。key: profit | revenue。"""
    cur = by_rd.get(rd)
    if not cur or cur.get(key) is None:
        return None
    val = cur[key]
    if rd[5:10] == "12-31":
        return val / 1e8
    y = int(rd[:4])
    pa, pp = by_rd.get(f"{y - 1}-12-31"), by_rd.get(f"{y - 1}-{rd[5:10]}")
    if not pa or not pp or pa.get(key) is None or pp.get(key) is None:
        return None
    return (val + pa[key] - pp[key]) / 1e8


def _datestr(v):
    """安全取日期字符串。重采后的财报里 NOTICE_DATE 可能是 NaN(float)，
    而 `NaN or ""` 返回 NaN（NaN 是 truthy），直接切片会 TypeError。"""
    if v is None:
        return ""
    if isinstance(v, float):          # NaN 或误存的数值
        return ""
    return str(v)[:10]


def build_ttm_cache(codes, dates):
    """({code:{date:利润TTM}}, {code:{date:营收TTM}}, n) —— 每个日期只用 NOTICE_DATE ≤ 该日 的报告期。

    营收 TTM 与利润 TTM 同源同法。加它是为了让逐股估值序列同时给出 PE 与 PS：
    亏损股（利润 TTM ≤ 0）没有有意义的 PE，估值只能走 PS —— 而"该用 PE 还是 PS"
    本身就是逐日判定（t 时点盈利与否），不该由今天的一个静态标签决定。
    """
    cache, rev_cache, loaded = {}, {}, 0
    for code in codes:
        path = STATEMENTS_DIR / f"{code}_profit.json"
        if not path.exists():
            continue
        try:
            records = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(records, list):
            continue
        by_rd, entries = {}, []
        for r in records:
            rd = _datestr(r.get("REPORT_DATE"))
            if not rd:
                continue
            nd = _datestr(r.get("NOTICE_DATE")) or _infer_notice_date(rd)
            # 利润口径统一用扣非归母：两种 schema 下 100% 非空（PARENT_NETPROFIT 仅
            # 17% 的「富」记录有），且对 600519 与归母净利润比值 0.9999。
            # 扣非剔除非经常性损益，本就是更稳的估值分母。
            by_rd[rd] = {"profit": r.get("DEDUCT_PARENT_NETPROFIT")
                                   or r.get("PARENT_NETPROFIT")
                                   or r.get("NETPROFIT"),
                         "revenue": r.get("OPERATE_INCOME")
                                    or r.get("TOTAL_OPERATE_INCOME")}
            entries.append((nd, rd))
        if not entries:
            continue
        entries.sort()
        ct, cr, i, avail = {}, {}, 0, []
        for td in dates:                       # dates 升序 → 单调推进，避免 O(n²)
            while i < len(entries) and entries[i][0] <= td:
                avail.append(entries[i][1])
                i += 1
            if not avail:
                continue
            rd_use = max(avail)
            tp = _ttm_from(rd_use, by_rd, "profit")
            if tp is not None:
                ct[td] = tp
            tr = _ttm_from(rd_use, by_rd, "revenue")
            if tr is not None:
                cr[td] = tr
        if ct or cr:
            if ct:
                cache[code] = ct
            if cr:
                rev_cache[code] = cr
            loaded += 1
    return cache, rev_cache, loaded


def isoweek(d):
    y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
    return f"{y}-W{w:02d}"


def _pct(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo))


SUB_PARENTS = {"半导体": "semi", "光通信": "optic_comm", "光学光电子": "optoelec"}


# ── 主流程 ────────────────────────────────────────────────────

def collect_trading_dates(start):
    """以大盘权重股的并集作为交易日历。"""
    ds = set()
    for code in ("600519", "601318", "000001", "601012", "300750", "600036"):
        fp = OHLCV_DIR / f"{code}.csv"
        if not fp.exists():
            continue
        with open(fp) as f:
            rd = csv.DictReader(f)
            if not rd.fieldnames or "date" not in rd.fieldnames:
                continue
            for r in rd:
                d = r.get("date", "")
                if d >= start and (r.get("close") or "").strip():
                    ds.add(d)
    return sorted(ds)


def _write_stock_valuation(stock_val, dates):
    """逐股估值序列 → stock_valuation_causal.json（列式，共享日期轴）。

    ━━ 为什么单独一个文件 ━━
    这是 T 体系"四件套"（T / MKR / BI / short_tool）共享的地基。T 只需要行业中位数，
    算完逐股 PE 就丢了；MKR 的个股层因此只能自己反推：

        v_ttm(t) = close(t) / (close_now / v_now)      v_now 取自今日估值矩阵

    分母锁死在今天 —— 新财报一发 EPS_now 变，整段历史 v_ttm 按同一比例平移。行业层
    早已因果，个股层却一直不是。把 t_causal 本来就算出的逐股 PE/PS 落盘，MKR 个股层
    改读它，四件套才真正共用同一个地基。

    ━━ 格式 ━━
    列式 + 共享日期轴，逐股只存自己的有效区间：

        {"dates": [...], "stocks": {"600519": {"i0": 12, "pe": [...], "ps": [...]}}}

    i0 是该股首个有值日在 dates 中的下标；pe/ps 等长，缺失位为 null。
    亏损日 pe 为 null（负 PE 对"离 cap 多远"无意义），ps 仍有值。
    PE 未做 [PE_MIN, PE_MAX] 截断 —— 那是 T 聚合时的取舍，不是数据本身的性质。
    """
    idx = {d: i for i, d in enumerate(dates)}
    stocks = {}
    n_pe = n_ps = 0
    for code, series in stock_val.items():
        ks = sorted(series)
        i0, i1 = idx[ks[0]], idx[ks[-1]]
        pe = [None] * (i1 - i0 + 1)
        ps = [None] * (i1 - i0 + 1)
        for d, (a, b) in series.items():
            j = idx[d] - i0
            pe[j], ps[j] = a, b
        stocks[code] = {"i0": i0, "pe": pe, "ps": ps}
        n_pe += sum(1 for x in pe if x is not None)
        n_ps += sum(1 for x in ps if x is not None)

    payload = {
        "generated_at": datetime.now(BJT).strftime("%Y-%m-%d %H:%M"),
        "contract": "causal: PE(t)=mcap(t)/利润TTM(≤t), PS(t)=mcap(t)/营收TTM(≤t); "
                    "mcap(t)=shares(t)×close_raw(t), shares 按 xdxr 还原; "
                    "报告期按 NOTICE_DATE ≤ t 取，无前视",
        "note": "pe 为 null 表示该日利润 TTM ≤ 0（亏损），估值应走 ps。"
                "该用 PE 还是 PS 是逐日判定，不是个股的静态属性。",
        "n_stocks": len(stocks), "n_dates": len(dates),
        "n_pe_points": n_pe, "n_ps_points": n_ps,
        "dates": dates,
        "stocks": stocks,
    }
    STOCK_VAL_PATH.write_text(json.dumps(payload, ensure_ascii=False,
                                         separators=(",", ":")))
    print(f"  逐股估值: {STOCK_VAL_PATH.name} "
          f"({STOCK_VAL_PATH.stat().st_size / 1048576:.1f} MB) "
          f"{len(stocks)} 只  PE {n_pe / 1e6:.2f}M 点 / PS {n_ps / 1e6:.2f}M 点")


def build(start, valuation_only=False):
    t0 = datetime.now()
    print("═══ 全历史因果 T 构建器 ═══")

    matrix = json.loads(MATRIX_PATH.read_text())
    ind_of = {s["code"]: s.get("industry") for s in matrix["stocks"] if s.get("industry")}
    # 半导体/光通信/光学光电子 三个母行业下的子行业归属
    sub_of = {s["code"]: (SUB_PARENTS[s["industry"]], s["sub_industry"])
              for s in matrix["stocks"]
              if s.get("industry") in SUB_PARENTS and s.get("sub_industry")}
    print(f"  M universe: {len(matrix['stocks'])} 只，其中有行业标签 {len(ind_of)} 只")

    dates = collect_trading_dates(start)
    print(f"  交易日: {len(dates)} 天 ({dates[0]} ~ {dates[-1]})")

    ttm, rev, n_ttm = build_ttm_cache(list(ind_of), dates)
    print(f"  TTM 覆盖: {n_ttm} 只（利润 {len(ttm)} / 营收 {len(rev)}）")

    sf = ShareFactor()

    # ── 逐股历史 PE → 按 (日期, 行业) 汇总 ──
    pe_by = defaultdict(lambda: defaultdict(list))
    sub_pe_by = defaultdict(lambda: defaultdict(list))     # date -> (parent_key, sub) -> [pe]
    stock_val = {}                     # code -> {date: (pe, ps)}  逐股估值序列，另行落盘
    n_stock = n_skip_share = 0
    for code, ind in ind_of.items():
        ct = ttm.get(code)
        cr = rev.get(code) or {}
        if not ct and not cr:
            continue
        shares_now = sf.shares_now.get(code)
        if not shares_now or shares_now <= 0:
            n_skip_share += 1
            continue
        fp = OHLCV_DIR / f"{code}.csv"
        if not fp.exists():
            continue
        with open(fp) as f:
            rd = csv.DictReader(f)
            if not rd.fieldnames or "date" not in rd.fieldnames:
                continue
            rows = [(r["date"], r.get("close")) for r in rd if r.get("date", "") >= start]
        if not rows:
            continue
        evs = sf.event_dates(code)
        used = False
        series = {}
        for d, close_s in rows:
            tp, tr = ct.get(d), cr.get(d)
            if tp is None and tr is None:
                continue
            try:
                close = float(close_s)
            except (TypeError, ValueError):
                continue
            if close <= 0:
                continue
            f_adj = 1.0                        # ② shares(t) = shares_now / F(t→now)
            for ed, mult in evs:
                if ed > d:
                    f_adj *= mult
            mcap_yi = shares_now / f_adj * close / 1e8

            # 逐股序列：PE 与 PS 都留，由消费端按 t 时点是否盈利自行选。
            # 亏损日 PE 记 None —— 负 PE 对"离 cap 多远"这个度量没有意义。
            pe_v = round(mcap_yi / tp, 3) if (tp is not None and tp > 0) else None
            ps_v = round(mcap_yi / tr, 3) if (tr is not None and tr > 0) else None
            if pe_v is not None or ps_v is not None:
                series[d] = (pe_v, ps_v)

            if tp is None or tp <= 0:          # ③ A group 逐日判定：t 时点盈利才入 T 的分子
                continue
            pe = mcap_yi / tp
            if PE_MIN < pe < PE_MAX:
                pe_by[d][ind].append(pe)
                sub = sub_of.get(code)
                if sub:
                    sub_pe_by[d][sub].append(pe)
                used = True
        if series:
            stock_val[code] = series
        if used:
            n_stock += 1
    print(f"  参与演算: {n_stock} 只（缺当前股本跳过 {n_skip_share} 只）")

    # ── 行业中位数 ──
    ind_m = {}
    # cap / r 留空，等主循环取到当日适用季度后回填 —— 必须与 H 判定同口径，
    # 否则明细算出的 above 与 daily 记录对不上（实测 2026-05-15 差 99 vs 66）
    ind_detail = defaultdict(dict)       # 行业 -> 日期 -> {m,p25,p75,p90,n,cap,r}
    for d, per_ind in pe_by.items():
        row = {}
        for ind, vals in per_ind.items():
            if len(vals) < MIN_INDUSTRY_STOCKS:
                continue
            med = round(statistics.median(vals), 2)
            row[ind] = (med, len(vals))
            ind_detail[ind][d] = {
                "m": med, "p25": _pct(vals, 25), "p75": _pct(vals, 75),
                "p90": _pct(vals, 90), "n": len(vals),
                "cap": None, "r": None,
            }
        if row:
            ind_m[d] = row

    sub_detail = {k: defaultdict(dict) for k in SUB_PARENTS.values()}
    for d, per_sub in sub_pe_by.items():
        for (pkey, sub), vals in per_sub.items():
            if len(vals) < MIN_INDUSTRY_STOCKS:
                continue
            sub_detail[pkey][sub][d] = {
                "m": round(statistics.median(vals), 2),
                "p25": _pct(vals, 25), "p75": _pct(vals, 75),
                "p90": _pct(vals, 90), "n": len(vals),
                "cap": None, "r": None,
            }
    # 逐股估值序列不依赖 cap —— 先落盘，解开 t_causal → base → cap → t_causal 的环。
    # base_builder 只需要这个文件，跑完它再产出 cap，t_causal 第二段才用得上 cap。
    _write_stock_valuation(stock_val, dates)
    if valuation_only:
        print("  --valuation-only：逐股估值已落盘，跳过 T 计算")
        return None

    ca = CapAccessor()
    print(f"  cap_accessor: {len(ca.names())} 行业 (gen {ca.generated_at})")

    dates = [d for d in dates if d in ind_m]
    print(f"  有效日期: {len(dates)} 天，行业数中位 "
          f"{sorted(len(ind_m[d]) for d in dates)[len(dates) // 2]}")

    # ── 逐日 T（T_v5） ──
    # 2026-08-26 由 T_v4 升级为 T_v5：删除 R_floor 搜索，把 60 天归一化移到 H 上。
    #   T_v4:  求 λ(t) 使 mean₆₀(H[cap·λ]) ≈ 0.5，再取 T ∝ H[cap·λ(t)](t)
    #          λ 出现在 H 的定义里、H 又用来定 λ —— 循环；且 λ 对 99~100% 的行业
    #          都在生效，把 cap 压成 base_i × 常数，逐季的 R_i(Q) 从未起过作用。
    #   T_v5:  H_adj(t) = H(t) − mean(H[t-W..t-1]) + 0.5
    #          等价目标（把 H 的 60 天基线钉在 0.5），但是加性、显式、无自指，
    #          且 cap 保持纯数据驱动 —— 与 MKR 共用同一个值。
    # 详见 views/t_structural_design.html 第 8 节。
    date_idx = {d: i for i, d in enumerate(dates)}
    out, S_hist = [], []

    def _H_raw(d, caps):
        a = t = 0
        for ind, (m, _) in ind_m[d].items():
            c = caps.get(ind)
            if c and c > 0:
                t += 1
                if m > c:
                    a += 1
        return (a / t) if t else H_BASE

    # ── H 的 60 天基线必须与当日同 cap ──
    # 2026-08-26 修正。此前基线取历史 H 的均值，而其中每一天用的是**它自己那天的**
    # cap。于是 H_adj = H − H_base + 0.5 在拿「新 cap 下的今天」比「旧 cap 下的
    # 过去」—— regime 生效日 cap 一跳，差值全部记到 H_adj 上，再经 60 天才消化，
    # 等于每年凭空造 4 次为期两月的假降温。
    #
    # 实测（修正前）：生效日 |ΔT| 中位 0.258 / 均值 0.375 / 最大 1.216，普通日
    # 只有 0.039 / 0.052 —— 6.6 倍；最大 8 次跳变里 6 次是生效日，且全部向下
    # （财报抬高 cap → d 骤降）。2024-11-01 单日 T 从 1.877 掉到 0.661。
    #
    # 改为用**当日 cap** 回算过去 60 天的 H：水位变化在两者之间对消，H_adj 只
    # 反映 m 的真实变化。仍然因果 —— cap(t) 与 m_j(j<t) 在 t 时刻都已知。
    #
    # 效果：生效日后 60 天的持续偏移从 +0.0266 降到 −0.0035（消失），
    # H_adj 标准差 −20%，日均波动仅 −2%（削掉的是伪波动不是信号）。
    # 生效日当天仍有一次性跳变 —— 那是估值基准的换季重定价，是事实，不抹平。
    #
    # 实现：cap 只在季度边界变，故按「适用季度」预算 H 矩阵（约 24 个季度），
    # 而非逐日重算 60 天。
    print("  预算 H 矩阵（按适用季度）...")
    q_of = {d: ca.applicable_quarter(d) for d in dates}
    _quarters = sorted({q for q in q_of.values() if q})
    Hq = {}
    for _q in _quarters:
        _d0 = next(d for d in dates if q_of[d] == _q)
        _cq = ca.caps(_d0)
        Hq[_q] = [_H_raw(d, _cq) for d in dates]
    print(f"    {len(_quarters)} 个季度 × {len(dates)} 日")

    for i in range(W, len(dates)):
        d = dates[i]
        caps = ca.caps(d)
        sub_caps_d = ca.sub_caps(d)
        for ind, cp in caps.items():                 # 回填行业明细的 cap/r
            cell = ind_detail.get(ind, {}).get(d)
            if cell and cp and cp > 0:
                cell["cap"] = round(cp, 2)
                cell["r"] = round(cell["m"] / cp, 3)
        for pkey in SUB_PARENTS.values():            # 回填子行业明细
            for sub, series in sub_detail[pkey].items():
                cell = series.get(d)
                cp = sub_caps_d.get(sub)
                if cell and cp and cp > 0:
                    cell["cap"] = round(cp, 2)
                    cell["r"] = round(cell["m"] / cp, 3)
        prev_d = dates[i - LOOKBACK]
        prev_m = ind_m.get(prev_d, {})
        dv, above, heat, cool = [], 0, 0, 0
        for ind, (m, _) in ind_m[d].items():
            c = caps.get(ind)
            if not c or c <= 0:
                continue
            x = (m - c) / c
            dv.append(x)
            if x > 0:
                above += 1
            pm = prev_m.get(ind)
            if pm:                                   # 偏离在扩张(away)还是回归(regress)
                if abs(x) > abs((pm[0] - c) / c):
                    heat += 1
                else:
                    cool += 1
        total = len(dv)
        if total < 20:
            continue
        H = above / total
        # 基线用**当日 cap** 回算 t 之前 60 天 —— 同口径相比，且不含今日（无自指）
        _hq = Hq.get(q_of[d])
        H_base = statistics.mean(_hq[i - W:i]) if _hq else H_BASE
        H_adj = H - (H_base - H_BASE)
        sd = statistics.stdev(dv)
        S = (statistics.mean(dv) - statistics.median(dv)) / sd if sd > 0 else 0
        # 基线只用 t 之前 —— 与 H_base 对称。2026-08-26 之前实现是先 append 再取
        # median，S_base 含今日，与设计口径 median(S[t-W..t-1]) 不符；H 侧没有这个
        # 问题。两侧口径必须一致：我们是拿今日去比过去，今日不该进基线。
        S_base = statistics.median(S_hist[-W:]) if S_hist else S
        S_hist.append(S)
        S_adj = S - S_base
        out.append({
            "date": d, "T": round(max(0.0, (H_adj / H_BASE) * (1 - max(S_adj, 0))), 3),
            "H": round(H, 3), "H_base": round(H_base, 3), "H_adj": round(H_adj, 3),
            "S": round(S, 3), "S_base": round(S_base, 3), "S_adj": round(S_adj, 3),
            "above": above, "below": total - above, "total": total,
            "heat": heat, "cool": cool,
            # 估值基准迁移 —— 与 T 并列的 Layer1 第二维，见 cap_accessor.cap_shift
            "cap_shift": (round(_cs, 5) if (_cs := ca.cap_shift(d)) is not None else None),
            "med_d": round(statistics.median(dv), 4),
            "n_stocks": sum(n for _, n in ind_m[d].values()),
        })

    # ── 周度聚合：取每周最后一个交易日 ──
    by_week = {}
    for r in out:
        by_week.setdefault(isoweek(r["date"]), []).append(r)
    weekly = []
    for wk in sorted(by_week):
        last = max(by_week[wk], key=lambda x: x["date"])
        weekly.append({**last, "week": wk})

    weekly_industries = {}
    for w in weekly:
        d = w["date"]
        rows = []
        pi = date_idx.get(d, 0) - LOOKBACK
        prev_wd = dates[pi] if pi >= 0 else None
        for ind, series in ind_detail.items():
            cell = series.get(d)
            if not cell or not cell.get("cap"):
                continue
            dv = cell["m"] / cell["cap"] - 1
            trend = ""
            pcell = series.get(prev_wd) if prev_wd else None
            if pcell and pcell.get("m"):             # 用当日 cap 比两个时点的偏离幅度
                trend = "away" if abs(dv) > abs(pcell["m"] / cell["cap"] - 1) else "regress"
            rows.append({"ind": ind, "pe": cell["m"], "cap": cell["cap"],
                         "d": round(dv, 4), "pos": "chase" if dv > 0 else "abandon",
                         "trend": trend,
                         "p25": cell["p25"], "p75": cell["p75"], "p90": cell["p90"],
                         "n": cell["n"]})
        rows.sort(key=lambda x: -x["d"])
        weekly_industries[d] = rows

    ts = [r["T"] for r in out]
    payload = {
        "generated_at": datetime.now(BJT).strftime("%Y-%m-%d %H:%M"),
        "contract": "causal: T(t) 只依赖 ≤t 的信息；价格未复权、股本按 xdxr 还原、"
                    "A group 逐日判定、cap 按已生效季度",
        "params": {"W": W, "H_base": H_BASE, "lookback": LOOKBACK,
                   "min_industry_stocks": MIN_INDUSTRY_STOCKS,
                   "pe_range": [PE_MIN, PE_MAX]},
        "summary": {
            "n_days": len(out), "date_range": [out[0]["date"], out[-1]["date"]],
            "T_median": round(statistics.median(ts), 3),
            "T_mean": round(statistics.mean(ts), 3),
            "T_min": min(ts), "T_max": max(ts),
            "T_below_1_pct": round(sum(1 for t in ts if t < 1.0) / len(ts) * 100, 1),
            "H_range": [min(r["H"] for r in out), max(r["H"] for r in out)],
            "H_adj_range": [round(min(r["H_adj"] for r in out), 3),
                            round(max(r["H_adj"] for r in out), 3)],
        },
        "daily": out,
        "weekly": weekly,
        "weekly_industries": weekly_industries,
        "industries": {ind: {"cap": (list(v.values())[-1] or {}).get("cap"), "dates": dict(v)}
                       for ind, v in ind_detail.items()},
        "semi": {sub: {"dates": dict(v)} for sub, v in sub_detail["semi"].items()},
        "optic_comm": {sub: {"dates": dict(v)} for sub, v in sub_detail["optic_comm"].items()},
        "optoelec": {sub: {"dates": dict(v)} for sub, v in sub_detail["optoelec"].items()},
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False))
    s = payload["summary"]
    print(f"\n  输出: {OUT_PATH.name} ({OUT_PATH.stat().st_size / 1024:.0f} KB)")
    print(f"  T: {s['n_days']} 天 {s['date_range'][0]} ~ {s['date_range'][1]}")
    print(f"     中位 {s['T_median']}  区间 [{s['T_min']}, {s['T_max']}]  T<1 占 {s['T_below_1_pct']}%")
    print(f"     H {s['H_range']}   H_adj {s['H_adj_range']}")
    print(f"  周度: {len(payload['weekly'])} 周   weekly_industries {len(payload['weekly_industries'])} 周")
    print(f"  逐行业明细: {len(payload['industries'])} 行业   "
          f"子行业: semi {len(payload['semi'])} / optic_comm {len(payload['optic_comm'])} "
          f"/ optoelec {len(payload['optoelec'])}")
    print(f"  用时 {(datetime.now() - t0).total_seconds():.1f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--valuation-only", action="store_true",
                    help="只产出 stock_valuation_causal.json 后退出。"
                         "用于流水线里 base_builder / cap_history_builder 之前的引导步骤")
    ap.add_argument("--start", default="2020-01-17",
                    help="ohlcv 全量起点。默认即全历史 —— 不要改小，会截断数据集")
    _a = ap.parse_args()
    build(_a.start, _a.valuation_only)
