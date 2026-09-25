#!/usr/bin/env python3
"""
industry_identification.py — 识别检验：制度边界 × 撕裂的前瞻判别力

━━ 要回答的问题 ━━
论文引 Hong2003（卖空受限下意见分歧累积后突然释放）与 Chen2019（A 股约束更强）
为撕裂提供机制动机。若该机制为真，有一个可证伪的推论：

    在套利**更受限**处，高估更难被纠正 ⇒ 撕裂的前瞻判别力应当**更强**。

本脚本用两个外生的制度边界切分样本来检验它：

    ① 卖空约束   成分股是否在融券标的名单内。不在名单者**无法做空**，
                 是制度给定的硬边界，不是内生选择。
                 度量：该行业「有融券余额的成分股占比」（抽样 12 个交易日取均值）
    ② 涨跌幅限制 主板 10% / 创业板(30x) 与科创板(688) 20%。
                 度量：该行业「20% 限幅成分股占比」

━━ 为什么必须在科技组内部切 ━━
科技与约束程度本身相关（可融券占比中位：科技 0.506 / 传统 0.445），
不分组会把「科技效应」读成「约束效应」。

━━ 2026-09-24 实测结论 ━━
两个维度给出方向一致的**反向**结果 —— 撕裂在套利更**通畅**的一侧判别力更高。
而回撤水平 dd 在两种切分下几乎不变，说明切分并非在加噪声。
一个平淡的替代解释（可融券行业只是估值测得更准）也被排除：受限一侧的行业
成员股中位数反而**更多**，噪声代理几乎相同。
故不主张撕裂源于套利受限。本脚本把这个否定结果做成可复现的产出。

用法: python3 industry_identification.py
产出: stock_cache/industry_ident.json
依赖: .industry_dd_cache.json / .industry_ret_cache.json（由 industry_panel_study.py 生成）
"""
import glob
import json
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from gate_params import P                                   # noqa: E402
from tearing_builder import _auc                            # noqa: E402

FZ = SCRIPT_DIR / "frozen"
OUT = SCRIPT_DIR / "industry_ident.json"
W0, W1, H = P("eval_w0"), P("eval_w1"), P("eval_h")
RET_THR = -0.05          # 与 industry_panel_study 的中性事件口径一致
N_SNAP = 12              # 两融名单抽样快照数


def isoweek(d):
    y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
    return f"{y}-W{w:02d}"


def short_constraint(members):
    """{industry: {cov, short}} —— cov=在名单占比，short=有融券余额占比。"""
    files = sorted(glob.glob(str(SCRIPT_DIR / "margin_cache" / "margin_202[456]-*.json")))
    if not files:
        return {}
    step = max(1, len(files) // N_SNAP)
    cov, sh = defaultdict(list), defaultdict(list)
    for f in files[::step][:N_SNAP]:
        d = json.loads(Path(f).read_text())
        on = set(d)
        for ind, codes in members.items():
            cov[ind].append(sum(1 for c in codes if c in on) / len(codes))
            sh[ind].append(sum(1 for c in codes if c in on
                               and (d[c].get("融券余额") or 0) > 0) / len(codes))
    return {i: {"cov": round(st.mean(cov[i]), 4), "short": round(st.mean(sh[i]), 4)}
            for i in cov}


def measurement_quality(tc, members, split_hi, split_lo):
    """排除「受限一侧只是测得更差」这个平淡解释。"""
    def stat(names):
        n_, jit, spr = [], [], []
        for ind in names:
            cells = [c for d, c in tc.get(ind, {}).get("dates", {}).items()
                     if W0 <= d <= W1 and c.get("r") and c.get("n")]
            if len(cells) < 200:
                continue
            rs = [c["r"] - 1 for c in cells]
            n_.append(st.median([c["n"] for c in cells]))
            jit.append(st.median([abs(rs[k] - rs[k - 1]) for k in range(1, len(rs))]))
            sp = [(c["p75"] - c["p25"]) / c["m"] for c in cells
                  if c.get("p25") and c.get("p75") and c.get("m")]
            if sp:
                spr.append(st.median(sp))
        return {"n_ind": len(n_),
                "members": round(st.median(n_), 1) if n_ else None,
                "jitter": round(st.median(jit), 5) if jit else None,
                "spread": round(st.median(spr), 3) if spr else None}
    return {"loose": stat(split_hi), "tight": stat(split_lo)}


def main():
    print("═══ 识别检验：制度边界 × 撕裂判别力 ═══")
    tc = json.loads((FZ / "t_causal_paper.json").read_text())["industries"]
    tech = set(json.loads((FZ / "bw_paper.json").read_text())
               ["tech_industries"]["list"])
    cls = json.loads((SCRIPT_DIR / "industry_classification.json").read_text())
    dd = json.loads((SCRIPT_DIR / ".industry_dd_cache.json").read_text())
    rt = json.loads((SCRIPT_DIR / ".industry_ret_cache.json").read_text())

    members = defaultdict(list)
    for c, v in cls.items():
        if v.get("industry") in tc:
            members[v["industry"]].append(c)
    sc = short_constraint(members)
    wide = {i: (sum(1 for c in cs if c.startswith(("688", "30"))) / len(cs))
            for i, cs in members.items()}
    print(f"  两融约束：{len(sc)} 个行业；限幅：{len(wide)} 个行业")

    axis = sorted({d for v in dd.values() for d in v if W0 <= d <= W1})
    pos = {d: i for i, d in enumerate(axis)}
    trad_med = {}
    for d in axis:
        v = [(tc[i]["dates"].get(d) or {}).get("r") for i in tc if i not in tech]
        v = [x - 1 for x in v if x is not None]
        if v:
            trad_med[d] = st.median(v)

    rows = []
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
        inside = set()
        for j in ei:
            inside.update(range(j - 4, j + 1))
        for i, d in enumerate(axis):
            r = (tc.get(ind, {}).get("dates", {}).get(d) or {}).get("r")
            tm, cur = trad_med.get(d), dd[ind].get(d)
            if r is None or tm is None or cur is None or i in inside:
                continue
            rows.append({"i": i, "ind": ind, "tear": r - 1 - tm, "dd": cur,
                         "y": any(i < j <= i + H for j in ei)})
    TE = [r for r in rows if r["ind"] in tech]
    print(f"  科技面板样本 {len(TE)}（全体 {len(rows)}）")

    def score(R):
        lab = [x["y"] for x in R]
        by = defaultdict(list)
        for k, x in enumerate(R):
            by[x["i"]].append(k)
        ia = sorted(by)

        def shifted(s0):
            out = [False] * len(R)
            for a_, i_ in enumerate(ia):
                src = ia[(a_ + s0) % len(ia)]
                sr, dr = by[src], by[i_]
                for t_, k in enumerate(dr):
                    out[k] = lab[sr[t_ % len(sr)]]
            return out
        perms = [shifted(s0) for s0 in range(1, len(ia), max(1, len(ia) // 120))]
        o = {}
        for key in ("tear", "dd"):
            v = [x[key] for x in R]
            a = _auc(v, lab)
            nl = [_auc(v, pm) for pm in perms]
            o[key] = {"auc": round(a, 4),
                      "p": round((sum(1 for x in nl if x >= a) + 1) / (len(nl) + 1), 4)}
        return o

    result = {}
    for tag, getter, label in (
            ("short", lambda r: (sc.get(r["ind"]) or {}).get("short"), "可融券成分占比"),
            ("wide", lambda r: wide.get(r["ind"]), "20% 限幅成分占比")):
        vals = [getter(r) for r in TE if getter(r) is not None]
        med = st.median(vals)
        hi = [r for r in TE if (getter(r) or 0) >= med]   # 约束松
        lo = [r for r in TE if (getter(r) or 0) < med]    # 约束紧
        a, b = score(hi), score(lo)
        names_hi = sorted({r["ind"] for r in hi})
        names_lo = sorted({r["ind"] for r in lo})
        result[tag] = {
            "split": label, "median": round(med, 4),
            "n_loose": len(hi), "n_tight": len(lo),
            "loose": a, "tight": b,
            "d_tear": round(b["tear"]["auc"] - a["tear"]["auc"], 4),
            "d_dd": round(b["dd"]["auc"] - a["dd"]["auc"], 4),
            "quality": measurement_quality(tc, members, names_hi, names_lo),
        }
        print(f"\n  ── {label}（中位 {med:.3f}）──")
        print(f"    约束松  tear {a['tear']['auc']:.4f} (p={a['tear']['p']:.4f})"
              f"   dd {a['dd']['auc']:.4f} (p={a['dd']['p']:.4f})")
        print(f"    约束紧  tear {b['tear']['auc']:.4f} (p={b['tear']['p']:.4f})"
              f"   dd {b['dd']['auc']:.4f} (p={b['dd']['p']:.4f})")
        print(f"    Δtear(紧−松) {result[tag]['d_tear']:+.4f}   "
              f"Δdd {result[tag]['d_dd']:+.4f}   （机制为真应 Δtear > 0）")

    both_neg = all(v["d_tear"] < 0 for v in result.values())
    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "window": [W0, W1], "h": H, "ret_thr": RET_THR,
        "n_tech_sample": len(TE), "dims": result,
        "mechanism_rejected": both_neg,
        "note": "机制假设：套利更受限处撕裂应更强（Δtear > 0）。"
                "两个维度均为负即方向一致地否定该假设。",
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"\n  机制假设{'被否定（两维度一致反向）' if both_neg else '未被否定'}")
    print(f"  → {OUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
