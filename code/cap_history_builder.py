#!/usr/bin/env python3
"""
cap_history_builder.py — 研究链路的逐季度 cap（PE + PS，行业 + 子行业）

    cap(name, Q) = base_ref(name, Q) × R(name, Q)

两个因子的来源刻意不同：

  base   全部来自 base_ref_history.json（逐季度 P50，本次新建）
  R      PE 侧沿用 historical_cap_pe.json 的权威值 —— 它早已逐季度、已验证因果，
         且 T_v5 的判别力实验就是用它做的，换掉会引入与实验不一致的第二个变量。
         PS 侧此前没有任何 R 序列，取 base_builder 新算的 R_PS(sqq_gp_P75)。

━━ 与 historical_cap_pe.json 的关系 ━━
旧文件保留不动（生产链路与旧脚本仍在读），但它有两个问题使其不适合继续做研究地基：
  · base_pe 是单期冻结值（2026-08-19），2020 年的 cap 用 2026 年的 base
  · 只有 PE，PS 侧完全空缺 —— MKR 的 2724 只 PS 股因此只能借用生产系统的
    逐股 cap_multiple（今日快照，且被 clamp 成「今天的 ps×0.7」）

本文件是研究链路的唯一 cap 源，由 cap_accessor 读取，T 与 MKR 共用。

━━ 因果契约 ━━
cap(name, Q) 只依赖 ≤ D(Q) 的信息，D(Q) = regime 生效日（法定披露截止日次日）。
查询某日的 cap 时只取「生效日 ≤ 查询日」的最晚季度 —— 由 cap_accessor 保证。
不做 anchor 缩放，不做 R_floor 截断（T_v5 已把归一化移出 cap，见
views/t_structural_design.html 第 8 节）。

输出: stock_cache/historical_cap.json
用法: python3 stock_cache/cap_history_builder.py
"""

import json
import statistics
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
# 阈值不写在代码里 —— 参数与实现分离，见 gate_params.py 的说明。
from gate_params import P

SCRIPT_DIR = Path(__file__).resolve().parent
BJT = ZoneInfo("Asia/Shanghai")

BASE_PATH = SCRIPT_DIR / "base_ref_history.json"
LEGACY_PATH = SCRIPT_DIR / "historical_cap_pe.json"
OUT_PATH = SCRIPT_DIR / "historical_cap.json"


def merge(base_grp, r_auth, r_own, tag):
    """base × R → {name: {Q: {base, R, cap, r_src}}}。"""
    out, n_auth, n_own, n_nor = {}, 0, 0, 0
    for name, series in base_grp.items():
        byq = {}
        for q, cell in series.items():
            b = cell.get("base")
            if not b or b <= 0:
                continue
            r = (r_auth.get(name, {}) or {}).get(q)
            src = "authoritative"
            if r is None:
                r = (r_own.get(name, {}) or {}).get(q, {}).get("R")
                src = "recomputed"
            if r is None or r <= 0:
                n_nor += 1
                continue
            n_auth += src == "authoritative"
            n_own += src == "recomputed"
            byq[q] = {"base": b, "R": round(r, 3),
                      "cap": round(b * r, 3), "n": cell.get("n"), "r_src": src}
        if byq:
            out[name] = byq
    print(f"  {tag:<10} {len(out):>4} 项   R 权威 {n_auth} / 自算 {n_own} / 缺 R 丢弃 {n_nor}")
    return out


def main():
    print("═══ 逐季度 cap 构建器（研究口径）═══")
    if not BASE_PATH.exists():
        raise SystemExit(f"缺 {BASE_PATH.name} —— 先跑 python3 stock_cache/base_builder.py")
    bd = json.loads(BASE_PATH.read_text())
    print(f"  base 源: {BASE_PATH.name} (gen {bd['generated_at']}) "
          f"{len(bd['quarters'])} 期 {bd['quarters'][0]} ~ {bd['quarters'][-1]}")

    # PE 的权威 R：{ind: {Q: R}}
    auth_ind, auth_sub = {}, {}
    if LEGACY_PATH.exists():
        lg = json.loads(LEGACY_PATH.read_text())
        for k, dst in (("industries", auth_ind), ("semi_subs", auth_sub)):
            for name, e in lg.get(k, {}).items():
                dst[name] = {h["quarter"]: h["R"] for h in e.get("history", [])
                             if h.get("R")}
        print(f"  权威 R 源: {LEGACY_PATH.name} (gen {lg.get('generated_at')}) "
              f"行业 {len(auth_ind)} / 子行业 {len(auth_sub)}")
    else:
        print(f"  ⚠ 无 {LEGACY_PATH.name}，PE 的 R 全部改用 base_builder 自算值")

    pe = merge(bd["industries_pe"], auth_ind, bd.get("R_pe", {}), "行业 PE")
    ps = merge(bd["industries_ps"], {}, bd.get("R_ps", {}), "行业 PS")
    sub_pe = merge(bd["sub_industries_pe"], auth_sub, bd.get("R_sub_pe", {}), "子行业 PE")
    sub_ps = merge(bd["sub_industries_ps"], {}, bd.get("R_sub_ps", {}), "子行业 PS")

    quarters = sorted({q for grp in (pe, ps, sub_pe, sub_ps)
                       for s in grp.values() for q in s})

    # ── 覆盖率闸门 ──────────────────────────────────────────────
    # 财报披露有一个多月的窗口（中报截止 08-31，年报截止 04-30），
    # 而 regime 生效日就压在窗口末尾。若此时采集尚未追平，
    # 新季度的 base 会由「早披露的那部分公司」算出 —— 那是有偏子集，
    # 不是市场。2026-09-01 实测：2026Q2 只有 1245 只样本（上季 3312，37.6%），
    # cap 中位骤降 10.4%，个别行业 −71% / +359%，
    # 顺着 cap → d_i → H / med_d / cap_shift 一路把 T、gap、risk、Layer0 全带偏。
    #
    # 故：样本量不足上一季 COVER_MIN 的季度**不发 regime 生效日**。
    # cap_accessor 取「生效日 ≤ 查询日的最晚季度」，自然回落到上一季。
    # 等采集追平后重建，闸门自行放行 —— 不需要人工干预，也不改写历史。
    COVER_MIN = P("cover_min")

    def q_samples(q):
        return sum((v[q] or {}).get("n") or 0 for v in pe.values() if q in v)

    regime = dict(bd["regime_dates"])
    withheld = {}
    for i, q in enumerate(quarters):
        if i == 0 or q not in regime:
            continue
        prev = quarters[i - 1]
        cur_n, prev_n = q_samples(q), q_samples(prev)
        if prev_n <= 0:
            continue
        ratio = cur_n / prev_n
        if ratio < COVER_MIN:
            withheld[q] = {"eff_withheld": regime.pop(q), "n": cur_n,
                           "prev": prev, "prev_n": prev_n,
                           "ratio": round(ratio, 3),
                           "why": f"样本量 {cur_n} 仅为上一季 {prev_n} 的 "
                                  f"{ratio:.1%}，低于 {COVER_MIN:.0%} —— "
                                  f"财报采集未追平，base 会由早披露的有偏子集算出"}
    if withheld:
        print("")
        print(f"  ⚠ 覆盖率闸门拦下 {len(withheld)} 个季度（cap 回落到上一季）:")
        for q, w in withheld.items():
            print(f"      {q}  样本 {w['n']}/{w['prev_n']} = {w['ratio']:.1%}"
                  f"  原生效日 {w['eff_withheld']}")
    else:
        print("")
        print(f"  覆盖率闸门: 全部季度样本量 ≥ 上一季的 {COVER_MIN:.0%}")
    payload = {
        "generated_at": datetime.now(BJT).strftime("%Y-%m-%d %H:%M"),
        "contract": "causal: cap(name,Q) = base_ref(Q) × R(Q)，只依赖 ≤D(Q) 的信息。"
                    "无 anchor 缩放，无 R_floor 截断。",
        "sources": {
            "base": f"{BASE_PATH.name} (P50 逐季)",
            "R_pe": f"{LEGACY_PATH.name} 权威值，缺失时回落 base_builder 自算",
            "R_ps": f"{BASE_PATH.name} 的 R_PS(sqq_gp_P75)，此前无既有来源",
        },
        "scope": "研究链路（T/MKR/BI/short_tool）唯一 cap 源。"
                 "生产系统 m_pricing_matrix 的 cap 是人工标定常量表，与本文件无关。",
        "quarters": quarters,
        "regime_dates": regime,
        "coverage": {q: q_samples(q) for q in quarters},
        "regime_withheld": withheld,
        "cover_min": COVER_MIN,
        "pe": pe, "ps": ps, "sub_pe": sub_pe, "sub_ps": sub_ps,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False))

    print(f"\n  输出: {OUT_PATH.name} ({OUT_PATH.stat().st_size / 1024:.0f} KB)")
    print(f"  季度: {len(quarters)} 期 ({quarters[0]} ~ {quarters[-1]})")

    latest = quarters[-1]
    print(f"\n  最新季 {latest}:")
    print(f"    {'行业':<12}{'PE base':>10}{'R':>7}{'cap_pe':>9}"
          f"{'PS base':>10}{'R':>7}{'cap_ps':>9}")
    for name in ("半导体", "银行", "医疗服务", "食品饮料", "光伏设备", "汽车零部件"):
        a = pe.get(name, {}).get(latest, {})
        b = ps.get(name, {}).get(latest, {})
        print(f"    {name:<12}{a.get('base', 0):>10.1f}{a.get('R', 0):>7.3f}"
              f"{a.get('cap', 0):>9.1f}"
              f"{b.get('base', 0):>10.2f}{b.get('R', 0):>7.3f}{b.get('cap', 0):>9.2f}")

    # 与旧 cap 的水位对照 —— 这个差值就是 R_floor 此前在补的口径差
    if LEGACY_PATH.exists():
        rows = []
        for name, byq in pe.items():
            old = next((h for h in lg["industries"].get(name, {}).get("history", [])
                        if h["quarter"] == latest), None)
            cur = byq.get(latest)
            if old and cur and old.get("cap_pe_raw"):
                rows.append(cur["cap"] / old["cap_pe_raw"])
        if rows:
            rows.sort()
            n = len(rows)
            print(f"\n  新 cap / 旧 cap_pe_raw ({n} 行业): 中位 {rows[n // 2]:.2f}  "
                  f"P10 {rows[n // 10]:.2f}  P90 {rows[int(n * .9)]:.2f}")
            print("  → 对照: R_floor 中位 1.75（旧设计正是靠它补这个口径差）")


if __name__ == "__main__":
    main()
