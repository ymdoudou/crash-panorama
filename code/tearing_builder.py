#!/usr/bin/env python3
"""
tearing_builder.py — 持续性结构撕裂的日频度量

━━ 它测什么 ━━
科技行业与传统行业相对各自估值天花板的位置之差：

    d_i(t)  = ( m_i(t) − cap_i(t) ) / cap_i(t)      行业 i 相对天花板的偏离
    d_tech  = median{ d_i : i ∈ 科技行业 }        名单见 bw_classification.tech_industries
    d_trad  = median{ d_i : i ∈ 其余行业 }
    gap(t)  = d_tech(t) − d_trad(t)

gap > 0 表示科技行业整体比传统行业更贵（相对各自的天花板）。

━━ 为什么是这个层次 ━━
2026-08-28 的检验澄清了一件事：撕裂**不在 P 集合内部**。
按 Bit-Watt 框架，核心层与外围层同属科技，崩溃时二者同步下跌 ——
Zone 轴与 Gate 轴的滞后相关最大值都在 lag=0（0.703 / 0.773），
两侧全部接近 0，**没有「核心先裂、外围后跟」的传导时序**。

撕裂发生在**科技与传统之间**，需要在 110 个行业的层次上度量，而非 62 个 P 板块。
「核心与外围同步下跌」不是反例，恰恰证明 P 集合作为整体是对的研究对象。

━━ 实测 ━━
全部落在产出的 `measured` 块里，**每次运行重算**，本文档不复写具体数值。
2026-08-28 之前这六个 AUC 是手打常量，代价当天兑现：换科技行业名单后
论文会静默留着旧数，且原写的 0.7475 已无法从任何口径复现。
口径由 EVAL_H / EVAL_W0 / EVAL_W1 三个常量固定，数值跟着数据走。

━━ 与 T 的关系 ━━
    gap ↔ H (above/total)  远高于  gap ↔ T ≈ gap ↔ H_adj
H_adj 减掉了自身 60 日基线，**去基线把「持续性」那部分滤掉了** ——
gap↔H 与 gap↔H_adj 的落差正是这一步造成的，T 继承了 H_adj 的相关水平。
所以 T 刻画的是撕裂的**变化率**，gap 刻画的是撕裂的**水平**。二者互补，不是替代。

━━ 为什么不并入 Layer0 ━━
实测 ew_base + gap 等权可把前瞻 AUC 从 0.8789 抬到 0.8883（+0.0094），
但 gap 作为撕裂假说的**直接度量**独立呈现更有价值 —— 塞进合成分数会模糊它的含义。
并入还需重走 LOEO、重新冻结、重标全部阈值。故独立落盘，供论文与看板引用。

━━ 因果性 ━━
m 与 cap 均来自 t_causal.json，cap 经 cap_accessor 只取「生效日 ≤ 查询日」的季度。
gap 的扩展窗口秩 pGap 只用 <t 的历史，暖机 250 日，与 Layer1 的 pT/pS 同口径。

输出: stock_cache/tearing_daily.json
用法: python3 stock_cache/tearing_builder.py
管道位置: m_daily_pipeline Phase 2.7a0（T 因果基座之后、Crash Gate 之前）
"""

import bisect
import json
import statistics
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tech_lock import declared, tech_sha
# 阈值不写在代码里 —— 参数与实现分离，见 gate_params.py 的说明。
from gate_params import P

SCRIPT_DIR = Path(__file__).resolve().parent
BJT = ZoneInfo("Asia/Shanghai")

T_JSON = SCRIPT_DIR / "t_causal.json"
BW_JSON = SCRIPT_DIR / "indexes" / "bw_classification.json"
VOL_JSON = SCRIPT_DIR / "volume_concentration.json"
OUT_PATH = SCRIPT_DIR / "tearing_daily.json"

EPS_JSON = SCRIPT_DIR / "frozen" / "episodes_paper.json"
GATE_JSON = SCRIPT_DIR / "crash_gate_daily.json"

RANK_WARMUP = 250      # 与 crash_gate_builder 一致
# 前瞻判别力的评估口径。三个常量决定 measured 块里的全部 AUC，
# 改动即改动论文第 5 节的数字 —— 故与阈值同级，写在这里而不是埋在函数里。
EVAL_H = P("eval_h")           # 前瞻窗（交易日）
EVAL_W0, EVAL_W1 = P("eval_w0"), P("eval_w1")   # 与 paper_freeze 的冻结窗一致


def expanding_rank(vals, warmup=RANK_WARMUP):
    """pX(t) = #{历史值 < x(t)} / #历史，只用 <t 的信息。暖机内为 None。"""
    out, hist = [], []
    for v in vals:
        if v is None:
            out.append(None)
            continue
        out.append(bisect.bisect_left(sorted(hist), v) / len(hist)
                   if len(hist) >= warmup else None)
        hist.append(v)
    return out


def _auc(pred, lab):
    """Mann-Whitney AUC；并列按 0.5 计。样本单侧为空返回 None。"""
    P = sorted(p for p, l in zip(pred, lab) if l)
    N = sorted(p for p, l in zip(pred, lab) if not l)
    if not P or not N:
        return None
    tot = 0.0
    for x in N:
        lo = bisect.bisect_left(P, x)
        hi = bisect.bisect_right(P, x)
        tot += lo + 0.5 * (hi - lo)
    return round(1 - tot / (len(P) * len(N)), 4)


def _ranks(v):
    """平均秩（并列取均值）—— Spearman 的秩变换。"""
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def _corr(a, b):
    """Spearman ρ。用秩而非原值 —— gap 与 H 的关系单调但非线性，
    Pearson 会低估；且 d_i 是重尾量，少数极端日会主导 Pearson。"""
    pair = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pair) < 30:
        return None
    xs = _ranks([p[0] for p in pair])
    ys = _ranks([p[1] for p in pair])
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** .5
    dy = sum((y - my) ** 2 for y in ys) ** .5
    return None if dx == 0 or dy == 0 else round(num / (dx * dy), 4)


def _resid(y, xs):
    """把 y 的秩对若干 x 的秩正交化，返回残差。

    用秩而不是原值：三个量的量纲与分布形状都不同（streak 是计数、gap 是比值、
    时间序号是等差），秩变换后正交化才是在比较单调关系，与 AUC 的口径一致。
    """
    def c(v):
        m = statistics.fmean(v)
        return [a - m for a in v]
    basis = []
    for x in xs:
        v = c(_ranks(x))
        for b in basis:
            k = sum(a * bb for a, bb in zip(v, b)) / sum(bb * bb for bb in b)
            v = [a - k * bb for a, bb in zip(v, b)]
        if sum(a * a for a in v) > 1e-9:
            basis.append(v)
    r = c(_ranks(y))
    for b in basis:
        k = sum(a * bb for a, bb in zip(r, b)) / sum(bb * bb for bb in b)
        r = [a - k * bb for a, bb in zip(r, b)]
    return r


def _persist(rows, sample, lab, starts):
    """「已经持续了多久」本身是否有前瞻信息 —— 对「持续 → 前瞻」的直接检验。

    正文 5.4 节把「持续」接到「前瞻」上，靠的是度量类型的论证：持续状态只能由
    水平量刻画，而水平量在图 7.1 上整齐地偏预报。那是一条经验规律，不是把
    「已经持续了多久」当作预测变量检验过。这里补这一步。

    三个对照缺一不可，少任何一个结论都会被高估：

      时间序号   2025 年的区制转换发生在窗口内部，此后 streak 近乎单调增长，
                 与「第几天」不易区分。不给出时间序号自身的 AUC，就无法判断
                 测到的是持续性，还是 SEVERE 事件在窗口后段更密集。
      剥离 gap   streak 与 gap 水平的秩相关 0.87。不剥离，报出来的多半是水平量
                 换了个写法，而水平量的判别力 5.3 节已经报过。
      留一事件   有效阳性窗只有 5 个（见 ep_pos）。事件级离散度是判断
                 「两个 AUC 的差距是否落在噪声内」的唯一依据。

    streak 无自由参数；share_N 的四个窗口全部报出，因为窗长是事后选的，
    只报最好的那个就是数据窥探。
    """
    SHARES = (20, 60, 120, 250)
    pos = [1 if (r["gap"] or 0) > 0 else 0 for r in rows]
    streak, sk = 0, []
    for v in pos:
        streak = streak + 1 if v else 0
        sk.append(streak)
    sh = {N: [statistics.fmean(pos[max(0, i - N + 1): i + 1])
              for i in range(len(rows))] for N in SHARES}

    gap = [r["gap"] for _, r in sample]
    st = [sk[i] for i, _ in sample]
    ti = [i for i, _ in sample]                       # 时间序号对照
    shw = {N: [sh[N][i] for i, _ in sample] for N in SHARES}

    ep_pos = [{"start": s, "n_pos": sum(1 for i, _ in sample
                                        if i < j <= i + EVAL_H)}
              for s, j in starts]

    def loeo(v):
        """轮流剔除某个事件的阳性日，看 AUC 在事件之间摆动多大。"""
        a = []
        for _, j in starts:
            keep = [k for k, (i, _) in enumerate(sample)
                    if not (i < j <= i + EVAL_H)]
            if len(set(lab[k] for k in keep)) < 2:
                continue
            a.append(_auc([v[k] for k in keep], [lab[k] for k in keep]))
        return {"min": min(a), "max": max(a), "range": round(max(a) - min(a), 4)}

    return {
        "note": "标签、样本与 AUC 口径完全沿用本函数上方的前瞻评估，未另立一套。"
                "streak = 截至当日 gap>0 已连续多少个交易日（因果，只用 t 及之前）；"
                "share_N = 过去 N 个交易日里 gap>0 的比例。",
        "n_eff_ep": sum(1 for e in ep_pos if e["n_pos"] > 0),
        "ep_pos": ep_pos,
        "auc": {"gap": _auc(gap, lab), "streak": _auc(st, lab),
                "tidx": _auc(ti, lab),
                **{f"share{N}": _auc(shw[N], lab) for N in SHARES}},
        "resid": {
            "streak_ex_gap": _auc(_resid(st, [gap]), lab),
            "streak_ex_tidx": _auc(_resid(st, [ti]), lab),
            "streak_ex_both": _auc(_resid(st, [gap, ti]), lab),
            "share60_ex_both": _auc(_resid(shw[60], [gap, ti]), lab),
        },
        "loeo": {"gap": loeo(gap), "streak": loeo(st), "share60": loeo(shw[60])},
        "corr": {"streak_tidx": _corr(st, ti), "streak_gap": _corr(st, gap),
                 "share60_gap": _corr(shw[60], gap)},
    }


def measure(rows, tech, trad):
    """前瞻判别力与相关性 —— 每次重算，不留手打常量。

    这一块曾是硬编码的六个 AUC。硬编码的代价在 2026-08-28 兑现：
    换科技行业名单后论文会静默留着旧数，且原值 0.7475 已无法从任何口径复现。
    现在口径写死在代码里，数值跟着数据走。

    标签  y(t) = 1  ⟺ 未来 EVAL_H 个交易日内有 SEVERE 事件**开始**
    样本  冻结窗内、且**不在任何事件区间内**的交易日
          —— 事件当中的日子度量的是「崩溃时是不是在崩溃」，那是检测不是预报
    """
    idx = {r["date"]: i for i, r in enumerate(rows)}
    eps = json.loads(EPS_JSON.read_text())
    eps = eps["episodes"] if isinstance(eps, dict) else eps
    inside = {r["date"] for r in rows
              for e in eps if e["start"] <= r["date"] <= e["end"]}
    starts = sorted((e["start"], idx[e["start"]]) for e in eps
                    if e["tier"] == "SEVERE" and e["start"] in idx)

    gate = {}
    if GATE_JSON.exists():
        gate = {r["date"]: r for r in json.loads(GATE_JSON.read_text())["daily"]}
    tcz = {r["date"]: r for r in json.loads(T_JSON.read_text())["daily"]}

    sample = [(i, r) for i, r in enumerate(rows)
              if EVAL_W0 <= r["date"] <= EVAL_W1 and r["date"] not in inside]
    lab = [any(i < j <= i + EVAL_H for _, j in starts) for i, _ in sample]

    def col(get):
        return [get(r) for _, r in sample]

    def pair(vals):
        z = [(v, l) for v, l in zip(vals, lab) if v is not None]
        return _auc([x[0] for x in z], [x[1] for x in z]) if z else None

    out = {
        "note": f"冻结窗 {EVAL_W0}~{EVAL_W1}，样本 {len(sample)} 个交易日"
                f"（阳性 {sum(lab)}）；标签 = 未来 {EVAL_H} 个交易日内 SEVERE 事件开始；"
                f"事件区间内的交易日已剔除。事件源 {EPS_JSON.name}，共 {len(eps)} 个。",
        "eval": {"h": EVAL_H, "window": [EVAL_W0, EVAL_W1],
                 "n": len(sample), "n_pos": sum(lab), "n_episodes": len(eps),
                 "episode_source": EPS_JSON.name},
        "auc_gap": pair(col(lambda r: r["gap"])),
        "auc_d_tech": pair(col(lambda r: r["d_tech"])),
        "auc_d_trad": pair(col(lambda r: r["d_trad"])),
        "auc_bw_turnover": pair(col(lambda r: r["bw_turnover_pct"])),
        "auc_p_turnover": pair(col(lambda r: r["p_turnover_pct"])),
        "auc_risk_ref": pair(col(lambda r: (gate.get(r["date"]) or {}).get("risk"))),
    }
    out["persistence"] = _persist(rows, sample, lab, starts)
    # 相关性也限定在冻结窗内 —— 全序列会把 2020~2023 的另一个 regime 混进来，
    # 与论文其余数字不同源。实测差别不小：H 全序列 +0.47 / 冻结窗 +0.55。
    win = [r for r in rows if EVAL_W0 <= r["date"] <= EVAL_W1]
    g = [r["gap"] for r in win]
    for k in ("H", "H_adj", "med_d", "T"):
        out["corr_gap_" + k] = _corr(
            g, [(tcz.get(r["date"]) or {}).get(k) for r in win])
    out["corr_window"] = [EVAL_W0, EVAL_W1]
    out["corr_n"] = len(win)
    out["n_tech"], out["n_trad"] = len(tech), len(trad)
    return out


def main():
    print("═══ 持续性结构撕裂（科技 vs 传统）═══")
    T = json.loads(T_JSON.read_text())
    tech_list = set(json.loads(BW_JSON.read_text())["tech_industries"]["list"])
    ind = T["industries"]
    tech = [k for k in ind if k in tech_list]
    trad = [k for k in ind if k not in tech_list]
    dates = [r["date"] for r in T["daily"]]
    print(f"  行业 {len(ind)} 个：科技 {len(tech)} / 传统 {len(trad)}")
    print(f"  日期轴 {len(dates)} 天 {dates[0]} ~ {dates[-1]}")

    vol = {}
    if VOL_JSON.exists():
        vol = {r["date"]: r for r in json.loads(VOL_JSON.read_text())["daily"]}

    def med_d(names, d):
        v = []
        for nm in names:
            cell = ind[nm]["dates"].get(d)
            if not cell:
                continue
            m, cp = cell.get("m"), cell.get("cap")
            if m and cp and cp > 0:
                v.append((m - cp) / cp)
        return (statistics.median(v), len(v)) if v else (None, 0)

    rows = []
    for d in dates:
        dt, nt = med_d(tech, d)
        dr, nr = med_d(trad, d)
        g = None if (dt is None or dr is None) else round(dt - dr, 5)
        v = vol.get(d) or {}
        rows.append({
            "date": d,
            "d_tech": None if dt is None else round(dt, 5),
            "d_trad": None if dr is None else round(dr, 5),
            "gap": g,
            "n_tech": nt, "n_trad": nr,
            # 流动性虹吸 —— 撕裂的另一面，成交额口径
            "p_turnover_pct": v.get("p_pct"),
            "bw_turnover_pct": v.get("bw_pct"),
        })

    for key in ("gap", "d_tech", "d_trad"):
        pr = expanding_rank([r[key] for r in rows])
        for r, p in zip(rows, pr):
            r["p_" + key] = None if p is None else round(p, 4)

    ok = [r for r in rows if r["gap"] is not None]
    gv = [r["gap"] for r in ok]
    pos = sum(1 for x in gv if x > 0)
    last = rows[-1]

    payload = {
        "generated_at": datetime.now(BJT).strftime("%Y-%m-%d %H:%M"),
        # 这条序列建在哪一版科技/传统名单上。校验脚本比对它与 bw_classification
        # 的声明值，不符即 FAIL —— 防止名单改了而序列没重建。
        "tech_sha": tech_sha(),
        "tech_lock": (declared() or {}).get("version"),
        "contract": "gap(t) = median{d_i : 科技} − median{d_i : 传统}，"
                    "d_i = (m_i − cap_i)/cap_i；m 与 cap 均为因果口径。"
                    "pGap 为扩展窗口秩，只用 <t 的历史，暖机 250 日。",
        "scope": "撕裂在**科技与传统之间**（110 行业层次），不在 P 集合内部 —— "
                 "核心与外围同属科技，崩溃时同步下跌（滞后相关最大值在 lag=0）。",
        "params": {"rank_warmup": RANK_WARMUP,
                   "n_tech": len(tech), "n_trad": len(trad)},
        "measured": measure(rows, tech, trad),
        "summary": {
            "n_days": len(ok), "range": [ok[0]["date"], ok[-1]["date"]],
            "gap_median": round(statistics.median(gv), 5),
            "gap_positive_pct": round(pos / len(gv) * 100, 1),
            "gap_p10": round(sorted(gv)[len(gv) // 10], 5),
            "gap_p90": round(sorted(gv)[int(len(gv) * .9)], 5),
        },
        "daily": rows,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False))

    s = payload["summary"]
    print(f"  gap 可算 {s['n_days']} 天 {s['range'][0]} ~ {s['range'][1]}")
    print(f"  中位 {s['gap_median']:+.5f}   科技更贵的天数占比 {s['gap_positive_pct']}%")
    print(f"  P10 {s['gap_p10']:+.5f}   P90 {s['gap_p90']:+.5f}")
    print(f"\n  最新 {last['date']}: d_tech={last['d_tech']}  d_trad={last['d_trad']}  "
          f"gap={last['gap']}")
    if last.get("p_gap") is not None:
        print(f"    gap 处历史 P{last['p_gap']*100:.0f}"
              f"   P 成交额占比 {last.get('p_turnover_pct')}%")
    print(f"  → {OUT_PATH.name} ({OUT_PATH.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
