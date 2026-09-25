#!/usr/bin/env python3
"""
industry_econ_significance.py — 面板口径的经济显著性：AUC 换成命中/误报/规避

━━ 为什么必须在面板上算，而不是市场级 ━━
市场级只有 5 个有效事件，命中率的分母是 5 —— 命中 4 个还是 3 个，差 20 个百分点，
而这个差完全在噪声里（见 5.8 的可分辨下限 0.231）。行业面板有 1030 个事件，
命中率第一次成为一个能读的数。

━━ 口径纪律 ━━
① 事件定义沿用中性口径（周收益 ≤ RET_THR，与所有对照量无机械关联），
   与 industry_panel_study.py 同一套 onsets_ret，不另立一份。
② 阈值取**池化分位**而非当日横截面分位。理由：tear 在当日横截面上等价于
   d_i（传统组中位是当日常数），按日取分位会把时序信号整个丢掉 —— 而
   regime shift 恰恰住在时序上。代价是警报率逐年不均，故按年分报。
③ 规避收益用**同一批样本**的前瞻 H 日复合收益，警报日与非警报日相减。
   不把"跌幅"与"命中率"混在一起讲 —— 前者是幅度，后者是频率，混用会让
   一个大事件顶替掉一堆小事件。
④ 一切与**基准率**比。base = 全样本阳性率；lift = precision / base。
   警报率 30% 的随机信号也会"命中"30% 的事件，不减去它就是自欺。

━━ 三个必须同时报的数 ━━
    precision   警报日里，真的在 H 日内跟来事件的比例
    recall      1030 个事件里，事前 H 日内至少响过一次警报的比例
    lift        precision / base —— 唯一能说明"比瞎猜强多少"的数
显著性由精确置换给出（保结构循环移位，与面板检验同一套零分布构造）。

用法: python3 stock_cache/industry_econ_significance.py
产出: stock_cache/industry_econ.json
依赖: .industry_dd_cache.json / .industry_ret_cache.json（industry_panel_study.py 生成）
"""
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from gate_params import P                                   # noqa: E402

FZ = SCRIPT_DIR / "frozen"
OUT = SCRIPT_DIR / "industry_econ.json"
W0, W1, H = P("eval_w0"), P("eval_w1"), P("eval_h")
RET_THR = -0.05
QS = (0.70, 0.80, 0.90)      # 池化分位 → 警报阈值
N_PERM = 120


def isoweek(d):
    y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
    return f"{y}-W{w:02d}"


def quantile(v, q):
    s = sorted(v)
    i = q * (len(s) - 1)
    lo, hi = int(i), min(int(i) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def build_rows():
    tc = json.loads((FZ / "t_causal_paper.json").read_text())["industries"]
    tech = set(json.loads((FZ / "bw_paper.json").read_text())
               ["tech_industries"]["list"])
    dd = json.loads((SCRIPT_DIR / ".industry_dd_cache.json").read_text())
    rt = json.loads((SCRIPT_DIR / ".industry_ret_cache.json").read_text())

    axis = sorted({d for v in dd.values() for d in v if W0 <= d <= W1})
    pos = {d: i for i, d in enumerate(axis)}
    trad_med = {}
    for d in axis:
        v = [(tc[i]["dates"].get(d) or {}).get("r") for i in tc if i not in tech]
        v = [x - 1 for x in v if x is not None]
        if v:
            trad_med[d] = st.median(v)

    rows, events = [], []
    for ind in dd:
        byw = defaultdict(list)
        for d, r in rt.get(ind, {}).items():
            if W0 <= d <= W1:
                byw[isoweek(d)].append((d, r))
        ws = sorted(byw)
        ev, last = [], None
        for k, w in enumerate(ws):
            c = 1.0
            for _, r in sorted(byw[w]):
                c *= (1 + r)
            if c - 1 <= RET_THR:
                if last is not None and k - last <= 3:
                    last = k
                    continue
                d0 = sorted(byw[w])[-1][0]
                if d0 in pos:
                    ev.append(d0)
                last = k
        ei = sorted(pos[d] for d in ev)
        events += [(ind, j) for j in ei]
        inside = set()
        for j in ei:
            inside.update(range(j - 4, j + 1))
        # 前瞻 H 日复合收益（因果：只用 t 之后的实际收益，不含 t 当日）
        dser = {pos[d]: r for d, r in rt.get(ind, {}).items() if d in pos}
        for i, d in enumerate(axis):
            r = (tc.get(ind, {}).get("dates", {}).get(d) or {}).get("r")
            tm, cur = trad_med.get(d), dd[ind].get(d)
            if r is None or tm is None or cur is None or i in inside:
                continue
            fwd, ok = 1.0, 0
            for k in range(i + 1, i + 1 + H):
                if k in dser:
                    fwd *= (1 + dser[k])
                    ok += 1
            rows.append({"i": i, "ind": ind, "tech": ind in tech,
                         "tear": r - 1 - tm, "dd": cur,
                         "y": any(i < j <= i + H for j in ei),
                         "fwd": (fwd - 1) if ok >= H // 2 else None,
                         "year": d[:4]})
    return rows, events, axis


def perm_labels(R, n):
    """保结构循环移位：同一交易日的标签整体搬移，保留自相关与截面结构。"""
    lab = [x["y"] for x in R]
    by = defaultdict(list)
    for k, x in enumerate(R):
        by[x["i"]].append(k)
    ia = sorted(by)
    step = max(1, len(ia) // n)
    out = []
    for s0 in range(1, len(ia), step):
        o = [False] * len(R)
        for a_, i_ in enumerate(ia):
            src = ia[(a_ + s0) % len(ia)]
            sr, dr = by[src], by[i_]
            for t_, k in enumerate(dr):
                o[k] = lab[sr[t_ % len(sr)]]
        out.append(o)
    return out


def score(R, events, key, q):
    """给定量与分位，算出全部口径。"""
    vals = [x[key] for x in R]
    thr = quantile(vals, q)
    alarm = [x[key] >= thr for x in R]
    n = len(R)
    base = sum(x["y"] for x in R) / n
    na = sum(alarm)
    if not na:
        return None
    prec = sum(1 for x, a in zip(R, alarm) if a and x["y"]) / na
    # 事件级召回：该事件所在行业，事前 H 日内是否响过警报
    on = defaultdict(set)
    for x, a in zip(R, alarm):
        if a:
            on[x["ind"]].add(x["i"])
    inds = {x["ind"] for x in R}
    ev = [(ind, j) for ind, j in events if ind in inds]
    hit = sum(1 for ind, j in ev
              if any(k in on[ind] for k in range(j - H, j)))
    fa = [x["fwd"] for x, a in zip(R, alarm) if a and x["fwd"] is not None]
    fn = [x["fwd"] for x, a in zip(R, alarm) if not a and x["fwd"] is not None]
    lift = prec / base if base else None

    # 置换零分布：lift 的显著性
    nl = []
    for pl in perm_labels(R, N_PERM):
        b = sum(pl) / n
        p_ = sum(1 for a, yy in zip(alarm, pl) if a and yy) / na
        if b:
            nl.append(p_ / b)
    p = (sum(1 for x in nl if x >= lift) + 1) / (len(nl) + 1) if nl else None
    return {
        "q": q, "thr": round(thr, 4), "n": n, "n_alarm": na,
        "alarm_rate": round(na / n * 100, 1),
        "base_pct": round(base * 100, 2), "precision_pct": round(prec * 100, 2),
        "lift": round(lift, 3), "lift_p": round(p, 4) if p is not None else None,
        "lift_null_p95": round(quantile(nl, 0.95), 3) if nl else None,
        "n_events": len(ev), "n_hit": hit,
        "recall_pct": round(hit / len(ev) * 100, 1) if ev else None,
        "fwd_alarm_pct": round(st.mean(fa) * 100, 3) if fa else None,
        "fwd_quiet_pct": round(st.mean(fn) * 100, 3) if fn else None,
        "avoided_pp": round((st.mean(fn) - st.mean(fa)) * 100, 3)
        if fa and fn else None,
    }


def main():
    print("═══ 面板口径的经济显著性 ═══")
    rows, events, axis = build_rows()
    print(f"  样本 {len(rows)}（行业 × 交易日），事件 {len(events)}，"
          f"基准阳性率 {sum(x['y'] for x in rows) / len(rows) * 100:.2f}%")
    groups = {"全部": rows, "科技": [x for x in rows if x["tech"]],
              "传统": [x for x in rows if not x["tech"]]}
    result = {}
    for gname, R in groups.items():
        print(f"\n  ── {gname}（{len(R)} 样本）──")
        print(f"  {'量':<6}{'分位':>6}{'警报率':>8}{'精确率':>8}{'基准':>7}"
              f"{'lift':>7}{'p':>8}{'召回':>7}{'规避(pp)':>10}")
        g = {}
        for key in ("tear", "dd"):
            g[key] = []
            for q in QS:
                s = score(R, events, key, q)
                if not s:
                    continue
                g[key].append(s)
                star = "*" if (s["lift_p"] or 1) <= 0.05 else " "
                print(f"  {key:<6}{q:>6.2f}{s['alarm_rate']:>7.1f}%"
                      f"{s['precision_pct']:>7.2f}%{s['base_pct']:>6.2f}%"
                      f"{s['lift']:>7.3f}{(s['lift_p'] or 0):>7.4f}{star}"
                      f"{(s['recall_pct'] or 0):>6.1f}%"
                      f"{(s['avoided_pp'] if s['avoided_pp'] is not None else 0):>10.3f}")
        result[gname] = g

    # ── 条件检验：撕裂在回撤水平之上还加不加信息 ──
    # 上面的并排比较只说明「谁单独更强」。真正决定 tear 该不该留在论文里的，
    # 是它在 dd 之上的**增量**。两个方向都算，免得只报有利的那个。
    def conditional(R, first, second, q):
        base_all = sum(x["y"] for x in R) / len(R)
        thr1 = quantile([x[first] for x in R], q)
        sel = [x for x in R if x[first] >= thr1]
        if len(sel) < 200:
            return None
        p1 = sum(x["y"] for x in sel) / len(sel)
        med = st.median([x[second] for x in sel])
        hi = [x for x in sel if x[second] >= med]
        lo = [x for x in sel if x[second] < med]
        if not hi or not lo:
            return None
        ph, pl = sum(x["y"] for x in hi) / len(hi), sum(x["y"] for x in lo) / len(lo)
        # 增量的显著性：在 sel 子样本上置换 second 的高低划分
        nl = []
        for pm in perm_labels(sel, 60):
            a_ = sum(1 for x, yy in zip(sel, pm) if x[second] >= med and yy)
            b_ = sum(1 for x in sel if x[second] >= med)
            c_ = sum(1 for x, yy in zip(sel, pm) if x[second] < med and yy)
            d_ = len(sel) - b_
            if b_ and d_:
                nl.append(a_ / b_ - c_ / d_)
        dlt = ph - pl
        return {
            "gate": first, "add": second, "q": q,
            "n_gate": len(sel), "prec_gate_pct": round(p1 * 100, 2),
            "prec_hi_pct": round(ph * 100, 2), "prec_lo_pct": round(pl * 100, 2),
            "delta_pp": round(dlt * 100, 3),
            "lift_gate": round(p1 / base_all, 3),
            "lift_hi": round(ph / base_all, 3),
            "p": round((sum(1 for x in nl if x >= dlt) + 1) / (len(nl) + 1), 4)
            if nl else None,
            "fwd_hi_pct": round(st.mean([x["fwd"] for x in hi
                                         if x["fwd"] is not None]) * 100, 3),
            "fwd_lo_pct": round(st.mean([x["fwd"] for x in lo
                                         if x["fwd"] is not None]) * 100, 3),
        }

    cond = {}
    print("\n  ── 条件检验：在一个量筛过之后，另一个还加不加信息 ──")
    print(f"  {'分组':<6}{'门槛量':>8}{'增量量':>8}{'门槛精确率':>12}"
          f"{'增量高':>9}{'增量低':>9}{'Δ(pp)':>9}{'p':>9}")
    for gname, R in groups.items():
        c = []
        for first, second in (("dd", "tear"), ("tear", "dd")):
            r = conditional(R, first, second, 0.90)
            if not r:
                continue
            c.append(r)
            star = "*" if (r["p"] or 1) <= 0.05 else " "
            print(f"  {gname:<6}{first:>8}{second:>8}"
                  f"{r['prec_gate_pct']:>11.2f}%{r['prec_hi_pct']:>8.2f}%"
                  f"{r['prec_lo_pct']:>8.2f}%{r['delta_pp']:>9.3f}"
                  f"{(r['p'] or 0):>8.4f}{star}")
        cond[gname] = c

    # 警报率逐年分布 —— 池化阈值的代价必须摆出来
    yearly = {}
    for q in QS:
        thr = quantile([x["tear"] for x in rows], q)
        byy = defaultdict(lambda: [0, 0])
        for x in rows:
            byy[x["year"]][1] += 1
            if x["tear"] >= thr:
                byy[x["year"]][0] += 1
        yearly[f"{q:.2f}"] = {y: round(a / b * 100, 1)
                              for y, (a, b) in sorted(byy.items()) if b}
    print("\n  ── 警报率逐年（池化阈值的代价）──")
    for q, m in yearly.items():
        print(f"    分位 {q}: " + "  ".join(f"{y} {v}%" for y, v in m.items()))

    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "window": [W0, W1], "h": H, "ret_thr": RET_THR, "quantiles": list(QS),
        "n_sample": len(rows), "n_events": len(events),
        "base_pct": round(sum(x["y"] for x in rows) / len(rows) * 100, 2),
        "groups": result, "conditional": cond, "alarm_by_year": yearly,
        "script": "stock_cache/industry_econ_significance.py",
        "note": "lift = precision / base。警报率 q 的随机信号 lift ≡ 1，"
                "故 lift 才是「比瞎猜强多少」；precision 单独看会被基准率骗。"
                "规避(pp) = 非警报日的前瞻 H 日均收益 − 警报日的，正值表示避开警报日有利。",
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"\n  → {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
