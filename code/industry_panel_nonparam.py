#!/usr/bin/env python3
"""
industry_panel_nonparam.py — §5.6 面板的非参数多重比较（Friedman + Wilcoxon）

━━ 为什么存在 ━━
FINR 审稿人 1（2026-10-23）要求：

  "Use the Friedman test to rank the overall performance of the competing
   models/indicators across the multiple severe episodes or industry panels.
   Use the Wilcoxon signed-rank test (with appropriate multiple comparison
   corrections, such as Bonferroni or Holm) ... Report the test statistics,
   p-values, and the average ranks of the models."

他的理由是对的：A 股数据非正态且波动剧烈，而原文的推断只有「每个量各自对
自身置换零分布」这一条，没有**量与量之间**的联合检验。

━━ 设计 ━━
    处理 k    tear · d · streak · dd   —— 与表 5.6 同一批候选量
    分块 n    行业。每个行业给出 4 个量在**该行业自己样本上**的 AUC
    度量      组内 AUC（秩和公式，与 tearing_builder._auc 同法）
    检验      Friedman（χ² 与 Iman-Davenport F 两种统计量）
              → 拒绝则 6 对 Wilcoxon 符号秩 + Holm 逐步校正

样本**不在本脚本重建**：由 industry_panel_study.py --dump-rows 导出，
两处共用同一份行。重建一遍必然漂，§5.6 与本节就会报不同的 n。

━━ 必须随结果一起说的话 ━━
Friedman 假定分块之间独立，而**行业之间并不独立**：论文自己写明
1030 个事件只落在 66 个互异周上，后者才是独立单位的上界。
所以这里的 p 值是**偏乐观**的：它把 100 个行业当成 100 个独立数据集。
这一点让结论的方向不对称 ——
  不显著  是**强**结论（连偏乐观的检验都没拒绝）
  显著    需谨慎（可能来自共同时间因子而非量之间的真实差异）
只用标准库：χ² 上尾用正则化不完全 Γ 的连分式，不引 scipy。
"""
import argparse
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
QTY = ("tear", "d", "streak", "dd")
MIN_POS = 5          # 组内阳性不足此数，AUC 噪声压倒信号，该行业不入分块
MIN_NEG = 20


def _auc(vals, labs):
    """秩和公式的 AUC，平均秩处理并列。与面板脚本同法。"""
    pairs = sorted(zip(vals, labs))
    n = len(pairs)
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        r = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[k] = r
        i = j + 1
    npos = sum(1 for _, l in pairs if l)
    nneg = n - npos
    if not npos or not nneg:
        return None
    s = sum(r for r, (_, l) in zip(ranks, pairs) if l)
    return (s - npos * (npos + 1) / 2.0) / (npos * nneg)


def _gammq(a, x):
    """正则化不完全 Γ 的上尾 Q(a,x)。x<a+1 用级数，否则用连分式。"""
    if x < 0 or a <= 0:
        raise ValueError
    if x == 0:
        return 1.0
    lg = math.lgamma(a)
    if x < a + 1.0:
        ap, s, d = a, 1.0 / a, 1.0 / a
        for _ in range(500):
            ap += 1.0
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-15:
                break
        return 1.0 - s * math.exp(-x + a * math.log(x) - lg)
    b, c, d = x + 1.0 - a, 1e300, 1.0 / (x + 1.0 - a)
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < 1e-300:
            d = 1e-300
        c = b + an / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - lg) * h


def chi2_sf(x, df):
    return _gammq(df / 2.0, x / 2.0)


def norm_sf(z):
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def rank_within(row):
    """块内按「越大越好」排名，1 为最好；并列取平均秩。"""
    order = sorted(range(len(row)), key=lambda i: -row[i])
    ranks = [0.0] * len(row)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and row[order[j + 1]] == row[order[i]]:
            j += 1
        r = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    return ranks


def wilcoxon(a, b):
    """配对符号秩。返回 (W, z, p)，正态近似带并列校正；零差值剔除。"""
    d = [x - y for x, y in zip(a, b) if x != y]
    n = len(d)
    if n < 6:
        return None, None, None
    mags = sorted((abs(v), i) for i, v in enumerate(d))
    ranks = [0.0] * n
    i = 0
    ties = []
    while i < n:
        j = i
        while j + 1 < n and mags[j + 1][0] == mags[i][0]:
            j += 1
        r = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[mags[k][1]] = r
        ties.append(j - i + 1)
        i = j + 1
    wp = sum(r for r, v in zip(ranks, d) if v > 0)
    wm = sum(r for r, v in zip(ranks, d) if v < 0)
    w = min(wp, wm)
    mu = n * (n + 1) / 4.0
    tie_adj = sum(t ** 3 - t for t in ties) / 48.0
    var = n * (n + 1) * (2 * n + 1) / 24.0 - tie_adj
    if var <= 0:
        return w, None, None
    z = (abs(w - mu) - 0.5) / math.sqrt(var)
    return w, z, 2.0 * norm_sf(z)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", default=str(SCRIPT_DIR / "industry_panel_rows.json"),
                    help="industry_panel_study.py --dump-rows 的产物")
    ap.add_argument("--out", default=str(SCRIPT_DIR / "industry_nonparam.json"))
    a = ap.parse_args()
    f = Path(a.rows)
    if not f.exists():
        sys.exit(f"缺 {f.name} —— 先跑\n"
                 f"  python3 stock_cache/industry_panel_study.py --dump-rows {f}")
    doc = json.loads(f.read_text())
    rows = doc["rows"]
    print("═══ §5.6 面板的非参数多重比较 ═══")
    print(f"  样本取自 {f.name}：{len(rows)} 行，"
          f"事件 {doc['design'].get('n_events')} 个")

    by_ind = defaultdict(list)
    for r in rows:
        by_ind[r["ind"]].append(r)

    # ── 分块：逐行业算 4 个量的组内 AUC ──
    blocks, dropped = [], []
    for ind, rs in sorted(by_ind.items()):
        npos = sum(1 for r in rs if r["y"])
        nneg = len(rs) - npos
        if npos < MIN_POS or nneg < MIN_NEG:
            dropped.append((ind, npos, nneg))
            continue
        labs = [r["y"] for r in rs]
        aucs = []
        for q in QTY:
            v = [r.get(q) for r in rs]
            if any(x is None for x in v):
                aucs = None
                break
            aucs.append(_auc(v, labs))
        if aucs is None or any(x is None for x in aucs):
            dropped.append((ind, npos, nneg))
            continue
        blocks.append({"ind": ind, "n": len(rs), "n_pos": npos,
                       "auc": dict(zip(QTY, [round(x, 4) for x in aucs])),
                       "tech": rs[0]["tech"]})
    n, k = len(blocks), len(QTY)
    print(f"  分块 {n} 个行业（阳性 ≥ {MIN_POS} 且阴性 ≥ {MIN_NEG}），"
          f"剔除 {len(dropped)} 个")
    if n < 10:
        sys.exit("✗ 分块太少，Friedman 无意义")

    # ── Friedman ──
    R = [rank_within([b["auc"][q] for q in QTY]) for b in blocks]
    avg = [sum(r[i] for r in R) / n for i in range(k)]
    ss = sum((x - (k + 1) / 2.0) ** 2 for x in avg)
    chi = 12.0 * n / (k * (k + 1)) * ss
    p_chi = chi2_sf(chi, k - 1)
    # Iman-Davenport：小 n 时比 χ² 保守，更可信
    denom = n * (k - 1) - chi
    F = ((n - 1) * chi / denom) if denom > 0 else float("inf")
    print(f"\n  平均秩（1 最好）：" +
          "　".join(f"{q} {v:.3f}" for q, v in zip(QTY, avg)))
    print(f"  Friedman χ² = {chi:.4f}  df = {k - 1}  p = {p_chi:.4f}"
          f"{'  ✓ 拒绝等效' if p_chi <= 0.05 else '  ✗ 不拒绝等效'}")
    print(f"  Iman-Davenport F = {F:.4f}  df = ({k - 1}, {(k - 1) * (n - 1)})")

    # ── Wilcoxon 两两 + Holm ──
    pairs = []
    for i in range(k):
        for j in range(i + 1, k):
            A = [b["auc"][QTY[i]] for b in blocks]
            B = [b["auc"][QTY[j]] for b in blocks]
            w, z, p = wilcoxon(A, B)
            pairs.append({"a": QTY[i], "b": QTY[j], "W": w,
                          "z": round(z, 4) if z is not None else None,
                          "p_raw": round(p, 6) if p is not None else None,
                          "median_diff": round(st.median(
                              [x - y for x, y in zip(A, B)]), 4)})
    live = [q for q in pairs if q["p_raw"] is not None]
    for rank, q in enumerate(sorted(live, key=lambda x: x["p_raw"])):
        q["p_holm"] = round(min(1.0, max(
            [(len(live) - r) * s["p_raw"]
             for r, s in enumerate(sorted(live, key=lambda x: x["p_raw"]))
             if s["p_raw"] <= q["p_raw"]] or [q["p_raw"]])), 6)
    print("\n  两两 Wilcoxon 符号秩（Holm 逐步校正，族内 "
          f"{len(live)} 个假设）：")
    print(f"    {'对比':<18}{'W':>9}{'z':>8}{'p':>9}{'p(Holm)':>10}"
          f"{'AUC 中位差':>12}  显著")
    for q in sorted(live, key=lambda x: x["p_raw"]):
        sig = "✓" if q.get("p_holm", 1) <= 0.05 else "✗"
        print(f"    {q['a'] + ' vs ' + q['b']:<18}{q['W']:>9.1f}"
              f"{q['z']:>8.3f}{q['p_raw']:>9.4f}{q['p_holm']:>10.4f}"
              f"{q['median_diff']:>+12.4f}  {sig}")

    payload = {
        "generated": __import__("datetime").datetime.now()
                     .strftime("%Y-%m-%d %H:%M"),
        "source": f.name, "design": doc["design"],
        "note": "处理=4 个候选量，分块=行业，度量=组内 AUC。"
                "行业之间并不独立（1030 事件只在 66 个互异周上），"
                "故 p 值偏乐观：不显著是强结论，显著需谨慎。",
        "min_pos": MIN_POS, "min_neg": MIN_NEG,
        "n_blocks": n, "k": k, "n_dropped": len(dropped),
        "quantities": list(QTY),
        "avg_rank": {q: round(v, 4) for q, v in zip(QTY, avg)},
        "friedman": {"chi2": round(chi, 4), "df": k - 1,
                     "p": round(p_chi, 6),
                     "reject_at_05": bool(p_chi <= 0.05),
                     "iman_davenport_F": round(F, 4),
                     "F_df": [k - 1, (k - 1) * (n - 1)]},
        "wilcoxon": pairs,
        "blocks": blocks,
    }
    Path(a.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"\n  → {Path(a.out).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
