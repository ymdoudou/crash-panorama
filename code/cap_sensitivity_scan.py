#!/usr/bin/env python3
"""
cap_sensitivity_scan.py — cap 两个分位参数的敏感性扫描

━━ 为什么存在 ━━
FINR 审稿人 2（2026-11-18）指：「the criteria for industry classification and
the theoretical justification for selecting the median and 75th percentile are
insufficiently explained. Moreover, the indicator's performance appears highly
sensitive to alternative parameter choices.」

他说对了，而且 §6.3 当时只对比了 P50 与 P25 一个备选，并承认
「that choice decides whether T works at all」—— 那是承认，不是依据。
本脚本把两个旋钮都扫一遍，把承认换成可证的边界。

━━ 两个旋钮 ━━
    base_ref(i,Q) = P_qb{ implied_base }      生产取 qb = 0.50
    cap_i(Q)      = base_ref × R(sqq_P_qg)    生产取 qg = 0.75

━━ 为什么按比值扰动，而不是重算整条链 ━━
d = m/cap − 1，分子 m（行业 PE 中位）**不依赖分位选择**，只有 cap 依赖。故

    cap'(i,Q) = cap_生产(i,Q) × [base_q{qb}/base_q50] × [R_q{qg}/R_q75]
    d'(i,t)   = r(i,t) / 该比值 − 1          （r = m/cap，冻结副本里已存）

两个比值都取自 base_builder 自己的网格，于是生产格点上比值恒为 1、
d' 与冻结的 r−1 **逐项相等**（脚本启动时自证这一条）。
这样扰动纯粹来自分位选择，不混入「R 的权威源是 historical_cap_pe、
而 base_builder 自算的 R 是回落源」这个血脉差异。

生产的 cap 可由 base × R 精确重建：3380 项逐项核对，零偏差（2026-11-19 实测）。

用法:
    python3 stock_cache/cap_sensitivity_scan.py            # 只扫广度与 gap（秒级）
    python3 stock_cache/cap_sensitivity_scan.py --panel    # 另跑 §5.6 面板（每格点约 1 分钟）
"""
import argparse
import bisect
import json
import os
import shutil
import statistics as st
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
FZ = SCRIPT_DIR / "frozen"
OUT = SCRIPT_DIR / "cap_sensitivity.json"
# 扰动比值单独落盘。为什么要单独一份：
# 比值取自 base_ref_history.json，那是**日更**文件 —— 2026-11-19 实测，
# 封存 82 天后它已把 6 个季度、16 个行业的 cap 追溯改写，中位 2.54%、最大 18.34%
# （用 cap_accessor 对 68859 个 (行业,日) 复查，97.03% 一致）。
# 直接把日更文件发进论文数据集，就是发一份与论文印的数字对不上的数据。
# 故把**比值**冻结成独立数据集：包里只要有它加 industry_d.json 的 r，
# 就能复算全部格点，完全不必碰日更文件。
# 注意如实说明：比值算自**当前**盈利面板，不是封存时刻的 ——
# 封存时从未记录过其他分位（t_causal 只存 cap 的乘积，拆不出 base 与 R）。
# 基线格点仍然精确，因为那里比值恒为 1。
RATIOS = SCRIPT_DIR / "cap_scan_ratios.json"
FROZEN_RATIOS = FZ / "cap_scan_ratios_paper.json"

# 扫描网格。含生产取值 (50, 75) 以便自证。
BASE_Q = (25, 35, 40, 50, 60, 65, 75)
SQQ_Q = (50, 60, 65, 75, 85, 90)
# 面板那半逐格点要跑一分钟，故只取能定住结论的子集：
# 沿 base 轴单变、沿 sqq 轴单变，再加四个对角点。
PANEL_GRID = ((25, 75), (35, 75), (40, 75), (50, 75), (60, 75), (75, 75),
              (50, 50), (50, 90), (35, 50), (40, 60), (60, 85), (60, 90))
W0, W1 = "2024-01-01", "2026-08-26"
MIN_IND_PER_DAY = 20      # 当日有效行业不足此数则不计入广度，避免暖机期噪声


def _ratio_table(bp, rp):
    """逐 (行业, 季度) 的扰动比值。生产格点上两列都恰好是 1.0。"""
    out = {}
    for ind in bp:
        for q in bp[ind]:
            e, r = bp[ind][q], (rp.get(ind) or {}).get(q)
            if not r:
                continue
            b0, r0 = e.get("base_q50"), r.get("R_q75")
            if not b0 or not r0:
                continue
            out.setdefault(ind, {})[q] = {
                "b": {str(x): round(e[f"base_q{x:02d}"] / b0, 6) for x in BASE_Q
                      if e.get(f"base_q{x:02d}") is not None},
                "r": {str(x): round(r[f"R_q{x:02d}"] / r0, 6) for x in SQQ_Q
                      if r.get(f"R_q{x:02d}") is not None}}
    return out


def _load():
    h = json.loads((SCRIPT_DIR / "historical_cap.json").read_text())
    tc = json.loads((FZ / "t_causal_paper.json").read_text())["industries"]
    tech = set(json.loads((FZ / "bw_paper.json").read_text())
               ["tech_industries"]["list"])
    # 优先读冻结的比值表：发布包里跑时 base_ref_history 不存在，
    # 而冻结副本让结果与论文印的那一份完全一致。
    if FROZEN_RATIOS.exists():
        rt = json.loads(FROZEN_RATIOS.read_text())
        src = FROZEN_RATIOS.name
    else:
        b = json.loads((SCRIPT_DIR / "base_ref_history.json").read_text())
        rt = _ratio_table(b["industries_pe"], b.get("R_pe") or {})
        RATIOS.write_text(json.dumps(rt, ensure_ascii=False,
                                     separators=(",", ":")))
        src = "base_ref_history.json（已另存 " + RATIOS.name + "）"
    return rt, h["regime_dates"], tc, tech, src


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", action="store_true",
                    help="另跑 §5.6 面板检验（每格点约 1 分钟）")
    a = ap.parse_args()
    RT, rd, tc, TECH, src = _load()
    print("═══ cap 分位敏感性扫描 ═══")
    print(f"  扰动比值取自 {src}")

    # 逐行业的适用季度轴：只用该行业真有比值的季度，按生效日排序。
    # 并列生效日（年报与一季报同在 05-01）取**季度标签较大**者 ——
    # 与 cap_accessor 的 cells[max(avail)] 同规则。自己另写一套判据会错：
    # 2026-11-19 实测，取错并列项会把一致率从 97.03% 压到 61.13%。
    appl = {}
    for ind in tc:
        qs = sorted([q for q in (RT.get(ind) or {}) if q in rd],
                    key=lambda q: (rd[q], q))
        appl[ind] = (qs, [rd[q] for q in qs])

    def ratio(ind, q, qb, qg):
        c = (RT.get(ind) or {}).get(q)
        if not c:
            return None
        b1 = (c.get("b") or {}).get(str(qb))
        r1 = (c.get("r") or {}).get(str(qg))
        if b1 is None or r1 is None:
            return None
        return b1 * r1

    def d_series(qb, qg):
        out = {}
        for ind, v in tc.items():
            qs, eff = appl[ind]
            if not qs:
                continue
            ser = {}
            for dt, rec in (v.get("dates") or {}).items():
                if not (W0 <= dt <= W1):
                    continue
                r = rec.get("r")
                if r is None:
                    continue
                i = bisect.bisect_right(eff, dt) - 1
                if i < 0:
                    continue
                k = ratio(ind, qs[i], qb, qg)
                if k:
                    ser[dt] = r / k - 1.0
            if ser:
                out[ind] = ser
        return out

    # ── 自证：生产格点上 d' 必须与冻结的 r−1 逐项相等 ──
    base = d_series(50, 75)
    n_chk = bad = 0
    for ind, ser in base.items():
        for dt, v in ser.items():
            r = tc[ind]["dates"][dt].get("r")
            if r is None:
                continue
            n_chk += 1
            if abs((r - 1) - v) > 1e-9:
                bad += 1
    if bad or not n_chk:
        raise SystemExit(f"✗ 自证失败：生产格点 {n_chk} 项中 {bad} 项与 r−1 不等 "
                         "—— 扰动口径有误，扫描结果不可用")
    print(f"  自证通过：生产格点 {n_chk} 项 d' 与冻结 r−1 逐项相等")

    def breadth(D):
        days = sorted({dt for s in D.values() for dt in s})
        H, g = [], []
        for dt in days:
            vs = [(i, s[dt]) for i, s in D.items() if dt in s]
            if len(vs) < MIN_IND_PER_DAY:
                continue
            H.append(sum(1 for _, x in vs if x > 0) / len(vs))
            t = [x for i, x in vs if i in TECH]
            tr = [x for i, x in vs if i not in TECH]
            if t and tr:
                g.append((dt, st.median(t) - st.median(tr)))
        by = {}
        for dt, x in g:
            by.setdefault(dt[:4], []).append(x)
        return {"H_med": round(st.median(H), 4), "H_min": round(min(H), 4),
                "H_max": round(max(H), 4), "n_days": len(H),
                "gap_pos_pct": {y: round(100 * sum(1 for x in v if x > 0) / len(v), 1)
                                for y, v in sorted(by.items())}}

    rows = []
    for qb in BASE_Q:
        for qg in SQQ_Q:
            rows.append({"base_q": qb, "sqq_q": qg,
                         "production": (qb, qg) == (50, 75),
                         **breadth(d_series(qb, qg))})
    print(f"  广度与 gap：{len(rows)} 个格点")

    panel = []
    if a.panel:
        for qb, qg in PANEL_GRID:
            panel.append({"base_q": qb, "sqq_q": qg,
                          "production": (qb, qg) == (50, 75),
                          **_run_panel(d_series(qb, qg))})
            last = panel[-1]
            print(f"    P{qb} / P{qg}  tear {last.get('tear_auc')} "
                  f"p={last.get('tear_p')}  dd {last.get('dd_auc')} "
                  f"p={last.get('dd_p')}")

    payload = {"generated": __import__("datetime").datetime.now()
                            .strftime("%Y-%m-%d %H:%M"),
               "window": [W0, W1],
               "note": "cap 两个分位参数的敏感性扫描。按比值扰动，"
                       "生产格点自证 d' 与冻结 r−1 逐项相等。",
               "production": {"base_q": 50, "sqq_q": 75},
               "min_ind_per_day": MIN_IND_PER_DAY,
               "breadth": rows, "panel": panel}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"\n  → {OUT.name}")
    return 0


def _run_panel(D):
    """把扰动后的 d 喂给真正的 industry_panel_study.py，不写复刻。"""
    with tempfile.TemporaryDirectory() as td:
        w = Path(td)
        (w / "frozen").mkdir()
        for f in ("bw_paper.json", "gate_params.json"):
            if (FZ / f).exists():
                shutil.copy2(FZ / f, w / "frozen" / f)
        (w / "frozen" / "industry_d_paper.json").write_text(
            json.dumps(D, ensure_ascii=False, separators=(",", ":")))
        for f in os.listdir(SCRIPT_DIR):
            if f.endswith(".py"):
                shutil.copy2(SCRIPT_DIR / f, w / f)
        for f in (".industry_dd_cache.json", ".industry_ret_cache.json",
                  "industry_classification.json"):
            shutil.copy2(SCRIPT_DIR / f, w / f)
        subprocess.run([sys.executable, "industry_panel_study.py"], cwd=w,
                       capture_output=True, text=True)
        f = w / "industry_panel.json"
        if not f.exists():
            return {"error": "面板脚本未产出结果"}
        g = ((json.loads(f.read_text()).get("result") or {}).get("全部") or {})
        m = g.get("metrics") or {}
        out = {"n": g.get("n"), "n_pos": g.get("n_pos")}
        for k in ("tear", "dd"):
            e = m.get(k) or {}
            out[f"{k}_auc"] = e.get("auc")
            out[f"{k}_p95"] = e.get("null_p95")
            out[f"{k}_p"] = e.get("p")
            out[f"{k}_sig"] = (e.get("p") is not None and e["p"] <= 0.05)
        return out


if __name__ == "__main__":
    sys.exit(main())
