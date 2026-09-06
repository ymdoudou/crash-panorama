#!/usr/bin/env python3
"""
reproduce.py — 只用本仓库 data/ 里的冻结数据，重算论文里的主要数字

━━ 这个脚本回答什么 ━━
论文的核心主张是可复现。但「可复现」有两级：

  验证级   论文里的每一个数字，都能从已发布的数据重算出来。  ← 本脚本
  重建级   从原始行情重建全部中间量。                        ← 本仓库不支持

重建级需要约 145 MB 中间文件与 GB 级原始行情，且依赖第三方接口的历史可得性，
不适合随论文发布。验证级才是审稿与复核实际需要的那一级：
读者关心的不是「你能不能再跑一遍爬虫」，而是「你报的这个 0.7443 是不是真的」。

━━ 怎么用 ━━
    python3 reproduce.py            # 重算并与论文值逐项比对
    python3 reproduce.py --verbose  # 附带中间量

每一项都打印「论文值 / 重算值 / 差」。全部一致时退出码 0，有出入时非 0。
读者应当看到的是一整列 ✓；出现 ✗ 就说明论文与数据对不上，请报告给通讯作者。

━━ 不依赖任何第三方库 ━━
只用标准库。AUC 用秩和公式直接算，不引 scikit-learn ——
少一个依赖，读者少一道门槛，也少一处版本差异导致的数字漂移。
"""
import argparse
import json
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
TOL = 1e-4          # 论文里的判别力保留四位小数，容差取到同一量级


def load(name):
    return json.loads((DATA / name).read_text())


# ────────────────────────── 统计工具 ──────────────────────────
def auc(scores, labels):
    """ROC 曲线下面积，用 Mann-Whitney U 的秩和形式算，平局取平均秩。

    这与 sklearn.metrics.roc_auc_score 在数值上等价（两者都是
    「随机取一正一负，正样本得分更高的概率」，平局记 0.5），
    但不引入依赖。见 Hanley & McNeil (1982)。"""
    pairs = [(s, l) for s, l in zip(scores, labels) if s is not None]
    if not pairs:
        return None
    pos = sum(1 for _, l in pairs if l)
    neg = len(pairs) - pos
    if pos == 0 or neg == 0:
        return None
    pairs.sort(key=lambda x: x[0])
    ranks, i = [0.0] * len(pairs), 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        r = (i + j) / 2.0 + 1.0          # 平局段取平均秩
        for k in range(i, j + 1):
            ranks[k] = r
        i = j + 1
    rsum = sum(r for r, (_, l) in zip(ranks, pairs) if l)
    return (rsum - pos * (pos + 1) / 2.0) / (pos * neg)


def median(xs):
    v = sorted(x for x in xs if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


# ────────────────────────── 标签构造 ──────────────────────────
def labels_detect(dates, episodes, tier="SEVERE"):
    """检测标签：该交易日落在某个 tier 事件的区间内。"""
    spans = [(e["start"], e["end"]) for e in episodes if e["tier"] == tier]
    return [any(a <= d <= b for a, b in spans) for d in dates]


def labels_forecast(dates, episodes, h, tier="SEVERE"):
    """预报标签：未来 h 个交易日内有 tier 事件**开始**，且该日不在任何事件区间内。

    「不在任何事件区间内」这一条是关键：事中日既满足检测标签、
    又可能落进下一个事件的前瞻窗，两种标签混在一起会让预报的
    判别力被检测能力污染。论文 §7.1 就是靠这条把两者分开的。
    返回 (labels, mask)，mask 为 False 的日子整体排除出预报评估。"""
    idx = {d: i for i, d in enumerate(dates)}
    starts = sorted(idx[e["start"]] for e in episodes
                    if e["tier"] == tier and e["start"] in idx)
    inside = [any(a <= d <= b for a, b in
                  [(e["start"], e["end"]) for e in episodes]) for d in dates]
    lab, mask = [], []
    for i, _ in enumerate(dates):
        if inside[i]:
            lab.append(False); mask.append(False); continue
        lab.append(any(i < s <= i + h for s in starts))
        mask.append(True)
    return lab, mask


def ranks(v):
    """平均秩（并列取均值）。残差检验用秩而不是原值：streak 是计数、
    gap 是比值、时间序号是等差，量纲与分布形状都不同，秩变换后正交化
    才是在比较单调关系，与 AUC 的口径一致。"""
    o = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(o):
        j = i
        while j + 1 < len(o) and v[o[j + 1]] == v[o[i]]:
            j += 1
        a = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[o[k]] = a
        i = j + 1
    return r


def resid(y, xs):
    """把 y 的秩对若干 x 的秩正交化（Gram-Schmidt），返回残差。"""
    def ctr(v):
        m = sum(v) / len(v)
        return [a - m for a in v]
    basis = []
    for x in xs:
        v = ctr(ranks(x))
        for b in basis:
            k = sum(a * bb for a, bb in zip(v, b)) / sum(bb * bb for bb in b)
            v = [a - k * bb for a, bb in zip(v, b)]
        if sum(a * a for a in v) > 1e-9:
            basis.append(v)
    r = ctr(ranks(y))
    for b in basis:
        k = sum(a * bb for a, bb in zip(r, b)) / sum(bb * bb for bb in b)
        r = [a - k * bb for a, bb in zip(r, b)]
    return r


def masked_auc(scores, labels, mask):
    s = [x for x, m in zip(scores, mask) if m]
    l = [x for x, m in zip(labels, mask) if m]
    return auc(s, l)


# ────────────────────────── 比对与报告 ──────────────────────────
class Report:
    def __init__(self):
        self.rows, self.bad = [], 0

    def cmp(self, name, paper, mine, tol=TOL, note=""):
        if paper is None or mine is None or isinstance(paper, str) \
                or isinstance(mine, str):
            ok = paper == mine          # 序列/文本按相等比，不作差
            d = ""
        else:
            d = abs(paper - mine)
            ok = d <= tol
        if not ok:
            self.bad += 1
        self.rows.append((ok, name, paper, mine, d, note))

    def show(self, title):
        print(f"\n═══ {title} ═══")
        for ok, name, p, m, d, note in self.rows:
            fp = f"{p:.4f}" if isinstance(p, float) else str(p)
            fm = f"{m:.4f}" if isinstance(m, float) else str(m)
            fd = f"{d:.2e}" if isinstance(d, float) and d else ("" if ok else "—")
            print(f"  {'✓' if ok else '✗'} {name:34s} 论文 {fp:>10s}   "
                  f"重算 {fm:>10s}  {fd:>9s} {note}")
        self.rows = []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    man = load("manifest.json")
    tear = load("tearing_paper.json")
    epi = load("episodes_paper.json")
    gate = load("gate_paper.json")
    cal = load("calibration.json")
    par = load("gate_params.json")
    P = {k: v["value"] if isinstance(v, dict) else v
         for k, v in (par.get("params") or par).items() if not k.startswith("_")}

    w0, w1 = man["window"]
    h = P.get("eval_h", 10)
    eps = epi["episodes"]

    print("═══ 论文数字复算 ═══")
    print(f"  冻结窗口   {w0} ~ {w1}（{man['gate_days']} 个交易日）")
    print(f"  事件       {epi['n']} 个   " +
          " / ".join(f"{k} {v}" for k, v in epi["tiers"].items()))
    print(f"  预报视野   h = {h} 个交易日")
    print(f"  数据来源   data/ 下 {len(man['files'])} 个冻结文件，无外部依赖")

    R = Report()

    # ── 1. 撕裂 gap 的逐年统计 ──
    td_all = tear["daily"]
    years = sorted({r["date"][:4] for r in td_all})
    print(f"\n═══ 逐年 gap（全序列 {len(td_all)} 个交易日，含窗口外的跨区制对照）═══")
    print(f"  {'年份':6s} {'交易日':>6s} {'gap 中位':>11s} {'科技更贵占比':>12s}")
    for y in years:
        rows = [r for r in td_all if r["date"].startswith(y)]
        g = [r["gap"] for r in rows if r.get("gap") is not None]
        pos = sum(1 for x in g if x > 0) / len(g) * 100 if g else 0
        print(f"  {y:6s} {len(rows):6d} {median(g):11.4f} {pos:11.1f}%")

    # ── 2. 撕裂各量的前瞻判别力 ──
    # 冻结副本是**全序列快照**（2020 起），不是截到窗口的。
    # 评估窗口写在 measured.eval.window 里，必须显式取出来筛，
    # 否则会把窗口外的 900 多天也算进去（实测 n 从 390 变成 1296）。
    EW0, EW1 = tear["measured"]["eval"]["window"]
    td = [r for r in td_all if EW0 <= r["date"] <= EW1]
    dates = [r["date"] for r in td]
    lab_f, mask = labels_forecast(dates, eps, h)
    ms = tear["measured"]
    for key, fld in (("auc_gap", "gap"), ("auc_d_tech", "d_tech"),
                     ("auc_d_trad", "d_trad"),
                     ("auc_bw_turnover", "bw_turnover_pct"),
                     ("auc_p_turnover", "p_turnover_pct")):
        mine = masked_auc([r.get(fld) for r in td], lab_f, mask)
        R.cmp(f"{fld} 预报 AUC", ms.get(key), mine)
    R.cmp("预报评估样本数", ms["eval"]["n"], sum(mask), tol=0)
    R.cmp("预报阳性日数", ms["eval"]["n_pos"], sum(1 for a, b in zip(lab_f, mask) if a and b),
          tol=0)
    R.show("第五部分：结构撕裂的判别力")

    # ── 2b. 5.5 节：持续性本身是否携带前瞻信息 ──
    # streak 必须在**全序列**上累计再切窗：从窗口首日重新起算会把
    # 2024-01-01 之前已经持续的那一段抹掉，窗口开头几十天全部失真。
    pz = ms.get("persistence")
    if pz:
        print("\n═══ 第五部分 5.5：持续性检验 ═══")
        pos_all, sk_all, run = [], [], 0
        for r in td_all:
            v = 1 if (r.get("gap") or 0) > 0 else 0
            pos_all.append(v)
            run = run + 1 if v else 0
            sk_all.append(run)
        base = {d: i for i, d in enumerate(r["date"] for r in td_all)}
        sk = [sk_all[base[d]] for d in dates]
        sh = {N: [sum(pos_all[max(0, base[d] - N + 1): base[d] + 1]) /
                  len(pos_all[max(0, base[d] - N + 1): base[d] + 1])
                  for d in dates] for N in (20, 60, 120, 250)}
        ti = [base[d] for d in dates]
        R.cmp("streak 前瞻 AUC", pz["auc"]["streak"], masked_auc(sk, lab_f, mask))
        R.cmp("时间序号 AUC（对照）", pz["auc"]["tidx"], masked_auc(ti, lab_f, mask))
        for N in (20, 60, 120, 250):
            R.cmp(f"share{N} 前瞻 AUC", pz["auc"][f"share{N}"],
                  masked_auc(sh[N], lab_f, mask))

        # 残差只在参与评估的日子上算，与 builder 的 sample 口径一致
        keep = [i for i, m in enumerate(mask) if m]
        g_k = [td[i].get("gap") for i in keep]
        sk_k, ti_k = [sk[i] for i in keep], [ti[i] for i in keep]
        s6_k = [sh[60][i] for i in keep]
        lab_k = [lab_f[i] for i in keep]
        R.cmp("streak 剥离 gap", pz["resid"]["streak_ex_gap"],
              auc(resid(sk_k, [g_k]), lab_k))
        R.cmp("streak 剥离时间序号", pz["resid"]["streak_ex_tidx"],
              auc(resid(sk_k, [ti_k]), lab_k))
        R.cmp("streak 剥离两者", pz["resid"]["streak_ex_both"],
              auc(resid(sk_k, [g_k, ti_k]), lab_k))
        R.cmp("share60 剥离两者", pz["resid"]["share60_ex_both"],
              auc(resid(s6_k, [g_k, ti_k]), lab_k))

        # 留一事件：极差是判断「AUC 差距是不是噪声」的唯一依据
        idxd = {d: i for i, d in enumerate(dates)}
        starts = sorted(idxd[e["start"]] for e in eps
                        if e["tier"] == "SEVERE" and e["start"] in idxd)
        eff = sum(1 for j in starts
                  if any(mask[i] and i < j <= i + h for i in range(len(dates))))
        R.cmp("有效事件起点数", pz["n_eff_ep"], eff, tol=0)
        for nm, vals in (("gap", [r.get("gap") for r in td]),
                         ("streak", sk), ("share60", sh[60])):
            got = []
            for j in starts:
                mk = [m and not (i < j <= i + h) for i, m in enumerate(mask)]
                if len(set(l for l, m in zip(lab_f, mk) if m)) < 2:
                    continue
                got.append(masked_auc(vals, lab_f, mk))
            R.cmp(f"{nm} 留一极差", pz["loeo"][nm]["range"],
                  round(max(got) - min(got), 4))
        R.show("第五部分 5.5：持续性检验")

    # ── 3. 三层判定量的检测 vs 预报 ──
    gd = gate["daily"]
    gdates = [r["date"] for r in gd]
    ld = labels_detect(gdates, eps)
    lf, gmask = labels_forecast(gdates, eps, h)
    print("\n═══ 第七部分：同一批量，两种标签 ═══")
    print(f"  {'量':16s} {'检测 AUC':>10s} {'预报 AUC':>10s}   方向")
    for fld in ("risk", "T", "pT", "pS", "price_stress", "margin_stress", "SI"):
        a_d = auc([r.get(fld) for r in gd], ld)
        a_f = masked_auc([r.get(fld) for r in gd], lf, gmask)
        if a_d is None or a_f is None:
            continue
        arrow = "检测强" if a_d > a_f else ("预报强" if a_f > a_d else "持平")
        print(f"  {fld:16s} {a_d:10.4f} {a_f:10.4f}   {arrow}")

    # ── 3b. Layer0 前瞻层 ──
    # 摘要引的前瞻 AUC 就出自这里；不核这一节，读者就核不了摘要里的那个数。
    ew = [r for r in load("ew_paper.json")["daily"] if EW0 <= r["date"] <= EW1]
    edates = [r["date"] for r in ew]
    elf, emask = labels_forecast(edates, eps, h)
    eld = labels_detect(edates, eps)
    # 「低值代表危险」的量要取反，否则 AUC 会落在 0.5 以下、读起来像反向指标。
    # 论文的 §7 判别力表用同一约定；两处不一致就没法逐项比对。
    INVERT = {"p_price"}          # T 也属此类，但它在 gate_paper 那一节
    print("\n═══ 第七部分：Layer0 前瞻层 ═══")
    print(f"  {'量':16s} {'检测 AUC':>10s} {'预报 AUC':>10s}   覆盖   取向")
    L0 = {}
    for fld in ("ew_base", "ew_full", "p_price", "p_cover", "pT_fwd", "pS"):
        n = sum(1 for r in ew if r.get(fld) is not None)
        if not n:
            continue
        sgn = -1 if fld in INVERT else 1
        vals = [None if r.get(fld) is None else sgn * r[fld] for r in ew]
        a_d, a_f = auc(vals, eld), masked_auc(vals, elf, emask)
        L0[fld] = (a_d, a_f)
        print(f"  {fld:16s} {'—' if a_d is None else format(a_d, '10.4f')} "
              f"{'—' if a_f is None else format(a_f, '10.4f')}   {n:4d} 日   "
              f"{'低值危险，已取反' if sgn < 0 else ''}")
    # 摘要与 §7 引的就是这四个数，必须断言，不能只打印
    for fld, pd_, pf in (("ew_base", 0.4355, 0.8729), ("ew_full", 0.5693, 0.8751),
                         ("p_price", 0.4075, 0.7999), ("p_cover", 0.7051, 0.7748)):
        if fld in L0:
            R.cmp(f"{fld} 检测 AUC", pd_, L0[fld][0])
            R.cmp(f"{fld} 预报 AUC", pf, L0[fld][1])
    R.show("第七部分：Layer0 前瞻层")

    # ── 4. Layer1 阈值的留一事件交叉验证 ──
    # 三处细节必须与论文的标定脚本一致，任何一处不同都会得到别的阈值：
    #   ① 召回判在**峰前 H 日窗口** [pk-H, pk] 内，不是整个事件区间。
    #      标定的是「预警是否在峰值之前响过」，不是「事件期间响没响过」。
    #   ② 选阈是最大召回之后取**报警日最少**者，不是取最严的阈值。
    #      同样召回下，报警越少越有用；取最严会挑到恰好卡住的那一个。
    #   ③ 比较是严格大于 S > thr。
    sev = [e for e in eps if e["tier"] == "SEVERE"]
    H = cal["lead_window"]
    thr_grid = [round(0.40 + 0.01 * i, 2) for i in range(51)]
    gidx = {d: i for i, d in enumerate(gdates)}
    risk = {r["date"]: r.get("risk") for r in gd}

    def peak_i(e):
        return gidx.get(e["pk"])

    reach = [e for e in sev if peak_i(e) is not None]

    def evaluate(thr, ids):
        """(命中事件数, 报警日数)。命中 = 峰前 H 日窗口内有任一日 risk > thr。"""
        on = {d for d in gdates if risk.get(d) is not None and risk[d] > thr}
        rec = 0
        for e in reach:
            if e["id"] not in ids:
                continue
            i = peak_i(e)
            if set(gdates[max(0, i - H):i + 1]) & on:
                rec += 1
        return rec, len(on)

    folds = []
    for e in reach:                      # 留一：每次剔掉一个事件，在其余事件上选阈
        others = [x["id"] for x in reach if x["id"] != e["id"]]
        best = None
        for t in thr_grid:
            r, n = evaluate(t, others)
            if best is None or (-r, n) < best[0]:      # 先最大召回，再最少报警
                best = ((-r, n), t, r, n)
        _, t, r, n = best
        folds.append({"ep": e["id"], "thr": t, "train_rec": r,
                      "train_n": len(others), "train_rate": n / len(gdates) * 100})

    picks = [f["thr"] for f in folds]
    mean = sum(picks) / len(picks)
    med = median(picks)
    sigma = (sum((x - mean) ** 2 for x in picks) / len(picks)) ** 0.5

    if args.verbose:
        print("\n  逐折明细（留出事件 / 训练集选出的阈值 / 训练召回 / 训练报警率）")
        for f in folds:
            print(f"    #{f['ep']:<3d} thr={f['thr']:.2f}  "
                  f"召回 {f['train_rec']}/{f['train_n']}  "
                  f"报警率 {f['train_rate']:.1f}%")

    c1 = cal["calib"]["layer1"]
    R.cmp("Layer1 LOEO 阈值中位", c1["loeo_med"], med, tol=1e-9)
    R.cmp("Layer1 折间离散度", c1["sigma"], sigma, tol=1e-4,
          note="σ<0.05 判为「由数据决定」")
    R.cmp("Layer1 阈值下沿", c1["loeo_range"][0], min(picks), tol=1e-9)
    R.cmp("Layer1 阈值上沿", c1["loeo_range"][1], max(picks), tol=1e-9)
    R.cmp("逐折阈值序列", str([f["thr"] for f in c1["folds"]]),
          str(picks), tol=0)
    R.cmp("Layer1 生产常数", cal["production_constants"]["layer1"],
          P.get("risk_alarm"), tol=0, note="gate_params.json 与标定结果对账")
    R.show("第八部分：阈值标定")

    # ── 5. 逐事件检出与定级 ──
    print("\n═══ 逐事件：Layer1 检出与 Layer2 定级 ═══")
    thr1, thr2 = P.get("risk_alarm", 0.65), cal["production_constants"]["layer2"]
    print(f"  阈值 risk ≥ {thr1}（检出）  SI ≥ {thr2}（critical）")
    print(f"  {'#':>3s} {'分级':9s} {'区间':24s} {'maxRisk':>8s} {'maxSI':>7s} "
          f"{'检出':>5s} {'定级':>5s}")
    n_hit = n_crit = n_false = 0
    for e in eps:
        rows = [r for r in gd if e["start"] <= r["date"] <= e["end"]]
        mr = max((r.get("risk") or 0) for r in rows) if rows else 0
        msi = max((r.get("SI") or 0) for r in rows) if rows else 0
        hit, crit = mr >= thr1, msi >= thr2
        if e["tier"] == "SEVERE":
            n_hit += hit; n_crit += crit
        elif crit:
            n_false += 1
        print(f"  {e['id']:3d} {e['tier']:9s} {e['start']}~{e['end']}  "
              f"{mr:8.4f} {msi:7.4f} {'✓' if hit else '·':>5s} "
              f"{'critical' if crit else '·':>5s}")
    R.cmp("SEVERE 检出数", len(sev), n_hit, tol=0)
    R.cmp("SEVERE 判 critical", len(sev), n_crit, tol=0)
    R.cmp("非 SEVERE 误判 critical", 0, n_false, tol=0)
    R.show("第八部分：逐事件表现")

    print()
    if R.bad:
        print(f"✗ {R.bad} 项与论文不符 —— 请把本输出发给通讯作者")
        return 1
    print("✓ 全部一致：论文中的数字可由本仓库的冻结数据重算得到")
    return 0


if __name__ == "__main__":
    sys.exit(main())
