#!/usr/bin/env python3
"""
industry_panel_study.py — 把前瞻检验从「市场级 5 个事件」搬到「行业面板」

━━ 为什么要做 ━━
2026-09-18 IRFA 拒稿，评语是「the central forecasting evidence is not credible
in the current design」。独立核算的结果（见 crash_panorama 5.8）：

    前瞻样本 390 个交易日、50 个阳性日 —— 但只来自 **5** 个有效 SEVERE 事件
    按事件计的 AUC 95% CI 含 0.5；差值的可分辨下限 0.231，而实测差值 0.0096

扩窗救不了：2020-04~2023-12 的 3.7 年里只有 2 个事件，且无一 SEVERE。
唯一能提高功效的方向是**换检验层次** —— 而撕裂本就是横截面估值分化，
在行业层检验比在市场聚合上检验更贴近假设本身。

━━ 设计 ━━
    单元    110 个行业 × 交易日（t_causal_paper 的行业，是 129 个分类行业的子集）
    事件    该行业自身的深度回撤「进入」。定义**原样移植市场级规则**：
            成员股相对各自 250 日最高收盘的回撤，取行业中位；
            周度 Δdd ≤ TIER_SEVERE(−5.0) 记为一次进入。
            同一量、同一阈值、同一回看，只换聚合单元 —— 不引入新的自由参数。
    预测量  该行业自身的 d = m/cap − 1（t_causal_paper 的 r − 1，因果）
            以及 streak = d>0 已连续交易日数（持续性）
    标签    未来 EVAL_H 个交易日内该行业有事件**开始**
    推断    **按日期整体循环移位**的置换检验。行业同涨同跌，逐单元置换会
            高估独立性；整体移位保留横截面相关结构，只切断时间对应关系。

━━ 事件定义必须与预测量无机械关联（2026-09-24 加）━━
原设计把事件定为「周度 Δdd ≤ −5pp」，而 dd 又是对照预测量之一。两者共用同一
底层量，存在**解析可证**的机械关联：

    dd(t) = close(t)/peak − 1
    Δdd   = [close(t+1) − close(t)]/peak = 收益率 × (1 + dd)

即「Δdd ≤ −5pp」换成收益率门槛是 −5% / (1 + dd)：
    dd = 0    （正在峰值）→ 需要 −5%   的收益
    dd = −60%              → 需要 −12.5% 的收益
**门槛随 dd 变化**，所以 dd 能预测事件，有一部分纯粹因为它决定了事件多难触发。

实测该污染的量级（2026-09-24）：换成固定收益率门槛后，
    dd   的事件 AUC  0.6125 → 0.5294，且**不再显著**（精确 p 0.185）
    tear 的事件 AUC  0.5530 → 0.5196，**仍然显著**（精确 p 0.0062）
dd 的原始 AUC 更高却不显著，是因为它自身时序自相关远强，零分布宽得多
（P95 0.5629 vs 0.5133）—— 移位后靠运气就能刷出那样的值。

故 --event-mode 默认为 return。dd 口径保留，供对照与复现旧结果。

━━ 零循环论证 ━━
回撤只用价格（split_adjusted，除权不除息），与估值完全无关。
这与市场级事件识别「零 T 依赖」是同一条纪律：
若事件本身由含预测量的模型推断，检验就不成立。

用法: python3 industry_panel_study.py [--rebuild]
产出: stock_cache/industry_panel.json
"""
import argparse
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from gate_params import P                                   # noqa: E402
from price_accessor import PriceAccessor                    # noqa: E402
from tearing_builder import _auc                            # noqa: E402

FZ = SCRIPT_DIR / "frozen"
# 产出文件名随 --event-mode 变。2026-09-25 事故：OUT 写死成 industry_panel.json，
# 于是「先跑 return 再跑 dd」时 dd 口径把中性口径的结果整个覆盖，
# 论文与发布包印出的 725 事件 / tear 0.5530 全是旧 Δdd 口径的数 ——
# 而 §5.6 整节存在的理由正是消除那个口径的机械偏袒。文件名必须自带口径。
def out_path(mode):
    return SCRIPT_DIR / ("industry_panel.json" if mode == "return"
                         else "industry_panel_dd.json")
CACHE = SCRIPT_DIR / ".industry_dd_cache.json"
RET_CACHE = SCRIPT_DIR / ".industry_ret_cache.json"

EVAL_H = P("eval_h")
W0, W1 = P("eval_w0"), P("eval_w1")
DD_LOOKBACK = 250          # 与 crash_episode_builder 同
TIER_SEVERE = -5.0         # 同上：峰值周 Δdd 分档线
MERGE_GAP = 3              # 相隔 ≤3 周的进入并为同一次
MIN_MEMBERS = 8            # 成员股少于此数的行业不进面板（中位数不稳）
RET_THR = -0.05            # 中性事件门槛：周收益 ≤ 此值。与 dd 无机械关联


def isoweek(d):
    y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
    return f"{y}-W{w:02d}"


def build_dd_panel(members):
    """{industry: {date: dd_median}} —— 只用价格，与估值无关。"""
    pa = PriceAccessor()
    per_ind = defaultdict(lambda: defaultdict(list))
    n_ok = n_skip = 0
    for ind, codes in members.items():
        for c in codes:
            try:
                ser = pa.split_adjusted(c)
            except Exception:
                ser = None
            if not ser or len(ser) < DD_LOOKBACK // 2:
                n_skip += 1
                continue
            n_ok += 1
            closes, dates = [], []
            for r in ser:
                cl = r.get("close")
                if cl and cl > 0:
                    closes.append(cl)
                    dates.append(r["date"])
            # 逐日相对过去 DD_LOOKBACK 日最高收盘的回撤
            for i in range(len(closes)):
                lo = max(0, i - DD_LOOKBACK + 1)
                pk = max(closes[lo:i + 1])
                if pk > 0:
                    per_ind[ind][dates[i]].append((closes[i] / pk - 1) * 100)
    out = {}
    for ind, byd in per_ind.items():
        out[ind] = {d: round(st.median(v), 3) for d, v in byd.items()
                    if len(v) >= MIN_MEMBERS}
    print(f"  成员股可用 {n_ok} / 跳过 {n_skip}；行业 {len(out)} 个有 dd 序列")
    return out


def build_ret_panel(members):
    """{industry: {date: 成员股日收益中位}} —— 与 dd 同源（split_adjusted）。"""
    pa = PriceAccessor()
    per = defaultdict(lambda: defaultdict(list))
    for ind, codes in members.items():
        for c in codes:
            try:
                ser = pa.split_adjusted(c)
            except Exception:
                ser = None
            if not ser or len(ser) < 60:
                continue
            prev = None
            for r in ser:
                cl = r.get("close")
                if cl and cl > 0:
                    if prev:
                        per[ind][r["date"]].append(cl / prev - 1)
                    prev = cl
    out = {i: {d: round(st.median(v), 6) for d, v in byd.items()
               if len(v) >= MIN_MEMBERS}
           for i, byd in per.items()}
    print(f"  收益面板：{len(out)} 个行业")
    return out


def onsets_ret(ret_by_date, thr):
    """中性事件：周度复合收益 ≤ thr。门槛对所有行业、所有时点相同，与 dd 无关。"""
    byw = defaultdict(list)
    for d, r in ret_by_date.items():
        byw[isoweek(d)].append((d, r))
    weeks = sorted(byw)
    ev, prev_w = [], None
    for i, w in enumerate(weeks):
        c = 1.0
        for _, r in sorted(byw[w]):
            c *= (1 + r)
        wr = c - 1
        if wr <= thr:
            if prev_w is not None and i - prev_w <= MERGE_GAP:
                prev_w = i
                continue
            ev.append({"week": w, "date": sorted(byw[w])[-1][0],
                       "wret": round(wr, 5)})
            prev_w = i
    return ev


def onsets(dd_by_date):
    """行业自身的深度回撤「进入」周 —— 移植市场级 Δdd ≤ −5 的规则。"""
    byw = defaultdict(list)
    for d, v in dd_by_date.items():
        byw[isoweek(d)].append((d, v))
    weeks = sorted(byw)
    wk_last = {w: sorted(byw[w])[-1] for w in weeks}      # 每周最后一个交易日
    ev, prev_w = [], None
    for i, w in enumerate(weeks):
        if i == 0:
            continue
        d_now, v_now = wk_last[w]
        _, v_prev = wk_last[weeks[i - 1]]
        if v_now - v_prev <= TIER_SEVERE:
            if prev_w is not None and i - prev_w <= MERGE_GAP:
                prev_w = i
                continue                                   # 并入上一次
            ev.append({"week": w, "date": d_now,
                       "delta_dd": round(v_now - v_prev, 2),
                       "dd": round(v_now, 2)})
            prev_w = i
    return ev


def main():
    global EVAL_H, TIER_SEVERE        # 必须在任何使用之前声明
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="忽略 dd 缓存，重算")
    ap.add_argument("--h", type=int, default=EVAL_H,
                    help=f"前瞻窗（交易日），默认 {EVAL_H} 与论文一致")
    ap.add_argument("--event-mode", choices=("return", "dd"), default="return",
                    help="事件定义。return=周收益≤门槛（与 dd 无机械关联，默认）；"
                         "dd=周度 Δdd≤分档线（旧口径，供对照）")
    ap.add_argument("--ret-thr", type=float, default=RET_THR,
                    help=f"return 模式的周收益门槛，默认 {RET_THR}")
    ap.add_argument("--tier", type=float, default=TIER_SEVERE,
                    help=f"事件分档线 Δdd，默认 {TIER_SEVERE}（SEVERE）；"
                         f"-3.0 为 MODERATE，事件更多")
    a = ap.parse_args()
    EVAL_H, TIER_SEVERE = a.h, a.tier
    print(f"  参数 h={EVAL_H}  分档线 Δdd≤{TIER_SEVERE}")

    print("═══ 行业面板前瞻检验 ═══")
    tc = json.loads((FZ / "t_causal_paper.json").read_text())["industries"]
    cls = json.loads((SCRIPT_DIR / "industry_classification.json").read_text())
    members = defaultdict(list)
    for c, v in cls.items():
        i = v.get("industry")
        if i in tc:
            members[i].append(c)
    print(f"  行业 {len(members)} 个（t_causal ∩ 分类），成员股 "
          f"{sum(len(v) for v in members.values())} 只")

    if CACHE.exists() and not a.rebuild:
        dd = json.loads(CACHE.read_text())
        print(f"  dd 面板取自缓存（{len(dd)} 个行业）；--rebuild 可重算")
    else:
        dd = build_dd_panel(members)
        CACHE.write_text(json.dumps(dd, ensure_ascii=False, separators=(",", ":")))

    # ── 事件 ──
    if a.event_mode == "return":
        if RET_CACHE.exists() and not a.rebuild:
            rp = json.loads(RET_CACHE.read_text())
            print(f"  收益面板取自缓存（{len(rp)} 个行业）")
        else:
            rp = build_ret_panel(members)
            RET_CACHE.write_text(json.dumps(rp, ensure_ascii=False,
                                            separators=(",", ":")))
        ev = {ind: onsets_ret(rp.get(ind, {}), a.ret_thr) for ind in dd}
        print(f"  事件定义：周收益 ≤ {a.ret_thr:.0%}（中性，与 dd 无机械关联）")
    else:
        ev = {ind: onsets(v) for ind, v in dd.items()}
        print(f"  事件定义：周度 Δdd ≤ {TIER_SEVERE}（旧口径，与 dd 共用底层量）")
    ev = {k: [e for e in v if W0 <= e["date"] <= W1] for k, v in ev.items()}
    n_ev = sum(len(v) for v in ev.values())
    wk_hist = defaultdict(int)
    for v in ev.values():
        for e in v:
            wk_hist[e["week"]] += 1
    print(f"\n  窗内行业级事件 {n_ev} 个，涉及 "
          f"{sum(1 for v in ev.values() if v)}/{len(ev)} 个行业")
    print(f"  分布在 {len(wk_hist)} 个不同周上 —— 这才是有效独立单位的上界")
    top = sorted(wk_hist.items(), key=lambda x: -x[1])[:5]
    print(f"  最集中的周: {top}")

    # ── 预测量：d = r − 1，streak，以及**逐行业的撕裂 tear** ──
    # tear_i(t) = d_i(t) − median{d_j(t) : j ∈ 传统组}
    # 这是论文 gap 的解聚版：论文的 gap = median(科技 d) − median(传统 d)，
    # 在**行业**中位上算（见 tearing_builder.med_d）。所以行业是撕裂的原生单元，
    # 把 gap 按行业拆开，检验的就是论文自己那个构造，而不是它的近亲。
    tech_list = set(json.loads((FZ / "bw_paper.json").read_text())
                    ["tech_industries"]["list"])
    axis = sorted({d for v in dd.values() for d in v if W0 <= d <= W1})
    pos = {d: i for i, d in enumerate(axis)}
    trad_med = {}
    for dt_ in axis:
        vs = []
        for ind_, cell_ in tc.items():
            if ind_ in tech_list:
                continue
            r_ = (cell_.get("dates", {}).get(dt_) or {}).get("r")
            if r_ is not None:
                vs.append(r_ - 1)
        if vs:
            trad_med[dt_] = st.median(vs)
    print(f"  科技 {len(tech_list & set(tc))} / 传统 {len(set(tc) - tech_list)} 行业；"
          f"传统组中位 d 覆盖 {len(trad_med)}/{len(axis)} 日")
    rows, skipped = [], 0
    for ind in sorted(dd):
        dts = tc.get(ind, {}).get("dates", {})
        ev_idx = sorted(pos[e["date"]] for e in ev.get(ind, []) if e["date"] in pos)
        if not ev_idx:
            skipped += 1
        inside = set()
        for j in ev_idx:                       # 事件当周整周剔除（检测≠预报）
            inside.update(range(j - 4, j + 1))
        streak = 0
        for i, d in enumerate(axis):
            cell = dts.get(d) or {}
            r = cell.get("r")
            dv = None if r is None else r - 1
            streak = streak + 1 if (dv is not None and dv > 0) else 0
            if dv is None or i in inside:
                continue
            tm = trad_med.get(d)
            rows.append({"ind": ind, "i": i, "date": d, "d": round(dv, 5),
                         "tech": ind in tech_list,
                         # 撕裂：该行业相对传统组中位的估值位置 —— 论文 gap 的解聚
                         "tear": None if tm is None else round(dv - tm, 5),
                         "streak": streak,
                         # 朴素价格对照：该行业自身的当前回撤。零估值依赖，
                         # 是「不需要 cap / T / 财报就能算」的那一类量。
                         "dd": dd[ind].get(d),
                         "y": any(i < j <= i + EVAL_H for j in ev_idx)})
    n, npos = len(rows), sum(1 for r in rows if r["y"])
    print(f"\n  面板样本 {n} 个（行业×交易日），阳性 {npos}"
          f"　| 无事件而未贡献阳性的行业 {skipped}")

    # ── 评估：三个子组 × 全部候选量，同一套置换零分布 ──
    # 子组必做：论文的故事是关于科技的，若增量只在合并样本上成立而在科技组内
    # 立不住，那是必须写进论文的限制，不是可以略过的细节。
    def rank(v):
        o = sorted(range(len(v)), key=lambda i_: v[i_])
        rk = [0] * len(v)
        for j_, i_ in enumerate(o):
            rk[i_] = j_
        return rk

    def resid_on(y_, x_):
        ry, rx = rank(y_), rank(x_)
        mx, my = st.mean(rx), st.mean(ry)
        sxx = sum((q - mx) ** 2 for q in rx)
        b = sum((q - mx) * (w - my) for q, w in zip(rx, ry)) / sxx if sxx else 0
        return [w - (my + b * (q - mx)) for q, w in zip(rx, ry)]

    full = [r for r in rows if r["dd"] is not None and r["tear"] is not None]
    groups = (("全部", full),
              ("科技", [r for r in full if r["tech"]]),
              ("传统", [r for r in full if not r["tech"]]))
    # 各组真正贡献样本的行业数。分类名单上的数与面板上的数不是一回事：
    # 名单有 110 个行业，窗内有数据的只有 100 个，正文说「N 个行业 × 交易日」
    # 用的必须是后者。
    n_used = {g: len({r['ind'] for r in R}) for g, R in groups}
    res = {}
    for gname, R in groups:
        if len(R) < 500:
            continue
        lab_g = [r["y"] for r in R]
        by_i = defaultdict(list)
        for k, r in enumerate(R):
            by_i[r["i"]].append(k)
        ia = sorted(by_i)

        def shifted(sh, _R=R, _lab=lab_g, _by=by_i, _ia=ia):
            out = [False] * len(_R)
            for a_, i_ in enumerate(_ia):
                src = _ia[(a_ + sh) % len(_ia)]
                s_rows, d_rows = _by[src], _by[i_]
                for t_, k in enumerate(d_rows):
                    out[k] = _lab[s_rows[t_ % len(s_rows)]]
            return out

        step = max(1, len(ia) // 150)
        perms = [shifted(sh) for sh in range(1, len(ia), step)]
        V = {"tear": [r["tear"] for r in R], "d": [r["d"] for r in R],
             "streak": [r["streak"] for r in R], "dd": [r["dd"] for r in R]}
        V["tear⊥dd"] = resid_on(V["tear"], V["dd"])
        V["d⊥dd"] = resid_on(V["d"], V["dd"])
        V["streak⊥dd"] = resid_on(V["streak"], V["dd"])
        V["dd⊥tear"] = resid_on(V["dd"], V["tear"])
        g = {"n": len(R), "n_pos": sum(lab_g), "n_days": len(ia), "metrics": {}}
        print(f"\n  ═══ {gname}  n={len(R)}  阳性 {sum(lab_g)} ═══")
        for key, v in V.items():
            obs = _auc(v, lab_g)
            null = [_auc(v, pm) for pm in perms]
            pv = (sum(1 for x in null if x >= obs) + 1) / (len(null) + 1)
            g["metrics"][key] = {"auc": round(obs, 4), "p": round(pv, 4),
                                 "null_p95": round(sorted(null)[int(len(null) * .95)], 4)}
            print(f"    {key:<11} AUC={obs:.4f}  P95={sorted(null)[int(len(null)*.95)]:.4f}"
                  f"  p={pv:.4f}  {'✓' if pv <= 0.05 else '✗'}")
        # 差值只在全部样本上做 —— 子组的功效不足以分辨
        if gname == "全部":
            g["pairs"] = {}
            print("    ── 差值 ──")
            for a_, b_ in (("tear", "dd"), ("tear", "d"), ("d", "dd"),
                           ("d", "streak")):
                dobs = _auc(V[a_], lab_g) - _auc(V[b_], lab_g)
                dn = [_auc(V[a_], pm) - _auc(V[b_], pm) for pm in perms]
                pv = (sum(1 for x in dn if abs(x) >= abs(dobs)) + 1) / (len(dn) + 1)
                fl = sorted(abs(x) for x in dn)[int(len(dn) * .95)]
                g["pairs"][f"{a_}-{b_}"] = {"delta": round(dobs, 4),
                                            "p": round(pv, 4), "floor": round(fl, 4)}
                print(f"      Δ({a_} − {b_}) = {dobs:+.4f}  下限 {fl:.4f}  "
                      f"p={pv:.4f}  {'✓' if pv <= 0.05 else '✗'}")
        res[gname] = g

    # ── 按 dd 分层：撕裂在「尚未下跌」的区间还有没有用 ──
    # 池化的 AUC 会把条件结构抹平。dd 与 tear 度量的不是同一件事
    # （前者是价格在自身近期区间中的位置，后者是估值相对同期传统组的位置），
    # 所以"谁更高"这个问法本身就不成立；该问的是**各自在哪里起作用**。
    strata = []
    byd = sorted(full, key=lambda r: r["dd"])
    m_ = len(byd)
    for k in range(10):
        lay = byd[k * m_ // 10:(k + 1) * m_ // 10]
        lb = [r["y"] for r in lay]
        if not lb or all(lb) or not any(lb):
            continue
        strata.append({
            "decile": k + 1,
            "dd_lo": round(lay[0]["dd"], 1), "dd_hi": round(lay[-1]["dd"], 1),
            "n": len(lay), "pos_pct": round(sum(lb) / len(lb) * 100, 1),
            "tear": round(_auc([r["tear"] for r in lay], lb), 4),
            "dd": round(_auc([r["dd"] for r in lay], lb), 4),
        })
    if strata:
        near = strata[-1]
        deep = [x for x in strata if x["decile"] <= 4]
        print(f"\n  按 dd 分层：最接近峰值一层 tear AUC {near['tear']}"
              f"（dd {near['dd_lo']}% ~ {near['dd_hi']}%，阳性率 {near['pos_pct']}%）；"
              f"最深回撤四层均值 {st.mean([x['tear'] for x in deep]):.4f}")

    payload = {
        "strata": strata,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "design": {"unit": "industry×trading-day", "window": [W0, W1],
                   "h": EVAL_H, "within_day_identical": True, "dd_lookback": DD_LOOKBACK,
                   "event_mode": a.event_mode, "ret_thr": a.ret_thr,
                   "tier_severe": TIER_SEVERE, "merge_gap_weeks": MERGE_GAP,
                   "min_members": MIN_MEMBERS},
        # len(dd) 含窗内无数据的空序列（2026-09-25 实测 110 键里 10 个为空）。
        # 正文说「N 个行业 × 交易日」时该用真正贡献样本的那个数，否则把
        # 分类口径的行业数当成了面板规模。两个都报，正文引 n_industries_used。
        "n_industries": len(dd),
        "n_industries_used": sum(1 for v in dd.values() if v),
        "n_used": n_used,
        "n_events": n_ev,
        "n_event_weeks": len(wk_hist),
        "event_weeks_top": top,
        "n_sample": n, "n_pos": npos,
        "result": res,
    }
    out = out_path(a.event_mode)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"\n  → {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
