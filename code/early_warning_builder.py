#!/usr/bin/env python3
"""
early_warning_builder.py — Layer0 前瞻状态量（不是门控）

━━ 为什么是「状态量」而不是第三个 Gate ━━
Layer1 对 SEVERE 召回 6/6，但提前量中位仅 3 交易日，2/6 是峰日才响。
缺的是预报能力。2026-08-26 做了完整的前瞻判别力实验（见 crash_panorama 第 5.6 节），
结论分两半：

  好消息 —— 前瞻信号确实存在，而且现行 Layer1 恰好把它抵消掉了。
    前瞻 h=10 的 AUC（统一按「原值高→预测崩溃」，事中日已剔除）：
        pS = rank(cap_shift)      0.757        T ↑                 0.724
        杀空 short_cover ↑         0.757        做空 short_open ↑     0.739
        price_stress ↓            0.816        多杀多 long_unwind ↑   0.689
        risk = 0.45·pT + 0.55·pS  0.578   ← 比它自己的两个分量都差
    原因是机械的：pT = 1 − rank(T)，按「T 低 = 危险」构造 —— 这对**检测**正确
    （崩溃当中 T 确实低），但**事前是反的**：崩溃前 T 高。两半互相抵消。

  坏消息 —— 它只能覆盖一半的 SEVERE。
    LOEO（留一事件）在四分量版本上是**通过**的：σ=0.0373，阈值只在 0.66~0.76 之间动。
    （两分量版本 σ=0.0857 不通过 —— 加入 pT_fwd 恰好治好了阈值不稳，见下节。）
    但中位阈值 0.66 的全集表现是：

        SEVERE 召回 4/6    报警率 29.7%    提前量 [7, 10, 10, 10]    合计 37 日
        对照 Layer1        6/6            26.8%         [0,0,1,3,8,10]        22 日

    所以 Layer0 **不是**更好的 Layer1，它是**另一件事**：Layer1 全召回但两个零提前，
    Layer0 只中 4 个但每个都提前 ≥7 个交易日。二者互补，不可互相替代。

故本文件落一个连续量 + 一个 **WATCH 观察位**（不是 ALARM）。
判读：WATCH = 处在崩溃更容易发生的估值/价格状态，且这个状态历史上有 4/6 的
SEVERE 跟在后面 —— 不是「要崩了」，是「该看着」。检测仍然是 Layer1 的职责。

━━ 为什么预报层用 rank(T)，而 Layer1 用 1−rank(T) ━━
这不是两个量，是**同一个量的两个相反用法**：

    Layer1（检测）  pT     = 1 − rank(T)     低 T = 危险   崩溃当中 T 确实低
    Layer0（预报）  pT_fwd =     rank(T)     高 T = 危险   崩溃之前 T 高

实现上 pT_fwd 直接取 `1 − pT`，不重新排序 —— crash_gate_builder 的 expanding_rank
在 lower_is_risk=True 时返回的正是 `1 − bisect_left(hist,T)/len(hist)`，两者严格互补。
这样两层用的永远是同一次排序的结果，不存在口径漂开的可能。

实测（因果口径，SEVERE-only，前瞻 h=10，事中日剔除）：加入 pT_fwd 使
ew_base 0.8012 → 0.8087，ew_full 0.8965 → 0.9029。增量不大，但 rank(T) 与
其余分量近乎正交（↔pS −0.004、↔p_cover +0.040、↔1−p_price +0.266），
是真的新信息而非冗余。

之所以敢加第 4 维：**Layer0 不落阈值**。此前担心的过拟合是针对阈值的
（6 个 SEVERE 撑不起一个过 LOEO 的阈值），而一个不设阈值的连续量没有这个负担。

━━ 定义（全部因果）━━
    surge_cover(t) = 融券偿还额(t) / mean(融券偿还额[t−60 .. t−1])     P 集合合计
                     融券偿还额 = 融券偿还量 × 收盘价
    p_cover(t)     = 扩展窗口秩(surge_cover)，暖机 120
    p_price(t)     = 扩展窗口秩(price_stress)，暖机 250
    pS(t)          = 扩展窗口秩(cap_shift)，来自 crash_gate_builder，暖机 250

    pT_fwd(t)      = 1 − pT(t)                                        = rank(T)

    ew_base = ( pT_fwd + pS + (1 − p_price) ) / 3               全历史
    ew_full = ( pT_fwd + p_cover + pS + (1 − p_price) ) / 4     2024-03-29 起

两个都落盘，**不自动切换** —— 自动切换会在 2024-03-29 留下一道人看不见的接缝。
ew_full 覆盖起点由 margin_cache 的起点(2023-07-04)减 60 日 surge 窗、加 120 暖机决定；
EP#3(2024-02-02 量化雪崩)结构性地落在覆盖之外，不是没检出，是数据不存在。

输出: stock_cache/early_warning_daily.json
用法: python3 stock_cache/early_warning_builder.py
管道位置: m_daily_pipeline Phase 2.7a2（crash_gate_builder 之后，看板数据之前）
"""

import bisect
import glob
import json
import statistics
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
# 阈值不写在代码里 —— 参数与实现分离，见 gate_params.py 的说明。
from gate_params import P

SCRIPT_DIR = Path(__file__).resolve().parent
BJT = ZoneInfo("Asia/Shanghai")

GATE_JSON = SCRIPT_DIR / "crash_gate_daily.json"
P_SECTOR = SCRIPT_DIR / "indexes" / "p_sector.json"
MARGIN_DIR = SCRIPT_DIR / "margin_cache"
OUT_PATH = SCRIPT_DIR / "early_warning_daily.json"

# WATCH 观察位阈值。LOEO（留一 SEVERE 事件）在 6 折上给出 0.66~0.76，σ=0.0373，
# 取中位 0.66。**不是 ALARM** —— 召回只有 4/6，它买的是提前量不是覆盖率。
# 重新标定的条件：SEVERE 事件集发生变化时（episodes_causal.json 重建后）。
EW_WATCH = P("ew_watch")

SURGE_W = P("surge_w")        # surge 的因果基线窗口
WARM_COVER = P("warm_cover")  # 杀空秩的暖机（通道本身起步晚，暖机短一点换覆盖）
WARM_PRICE = P("warm_price")  # 与 Layer1 的 RANK_WARMUP 对齐


def expanding_rank(vals, warmup):
    """pX(t) = #{历史值 < x(t)} / #历史，只用 <t 的信息。暖机内为 None。

    与 crash_gate_builder.expanding_rank 同构 —— 两处必须一致，否则 pS 与
    p_price / p_cover 不在同一个尺度上，平均出来的 EW 没有意义。
    """
    out, hist = [], []
    for v in vals:
        if v is None:
            out.append(None)
            continue
        out.append(bisect.bisect_left(sorted(hist), v) / len(hist)
                   if len(hist) >= warmup else None)
        hist.append(v)
    return out


def load_cover_surge(p_codes, px_fallback=None):
    """P 集合的融券偿还额日频 surge。返回 {date: surge}。

    ━━ 为什么需要 px_fallback ━━
    融券偿还额 = 融券偿还量 × 收盘价。两融快照里本该带「收盘价」，
    但 2026-08-30 实测：586 个快照里有 39 个**整份缺这个字段**
    （早期抓取脚本的版本差异），于是当日 n=0、整日被丢掉，
    p_cover 断档 → ew_full 断档。表现就是 Layer0 图上紫线断续而蓝线连续。

    收盘价本身在 OHLCV 里是有的，且是同一个交易日的同一个数 ——
    没有理由因为快照少一个字段就丢掉一整天。故快照缺字段时回落到 OHLCV。
    口径不变：都是该日收盘价（raw，未复权）—— 对手项「融券偿还量」
    也是在该时点度量的股数。
    """
    daily = {}
    n_fallback = 0
    for fp in sorted(glob.glob(str(MARGIN_DIR / "margin_*.json"))):
        d = fp[-15:-5]
        try:
            snap = json.loads(Path(fp).read_text())
        except (json.JSONDecodeError, OSError):
            continue
        total, n, used_fb = 0.0, 0, False
        for code, v in snap.items():
            if code not in p_codes:
                continue
            px = float(v.get("收盘价") or 0)
            if px <= 0 and px_fallback:
                px = px_fallback(code, d) or 0
                if px > 0:
                    used_fb = True
            if px <= 0:
                continue
            n += 1
            total += float(v.get("融券偿还量") or 0) * px
        if n:
            daily[d] = (total, n)
            if used_fb:
                n_fallback += 1
    if n_fallback:
        print(f"  收盘价回落 OHLCV: {n_fallback} 个交易日（快照缺该字段）")
    dates = sorted(daily)
    vals = [daily[d][0] for d in dates]
    out = {}
    for i in range(SURGE_W, len(vals)):
        base = statistics.fmean(vals[i - SURGE_W:i])
        if base > 0:
            out[dates[i]] = vals[i] / base
    n_med = statistics.median(daily[d][1] for d in dates) if dates else 0
    return out, (dates[0] if dates else None), (dates[-1] if dates else None), n_med


def _episodes():
    """事件清单。生产走 episodes_causal.json；冻结增补脚本另行传入冻结副本。"""
    f = SCRIPT_DIR / "episodes_causal.json"
    if not f.exists():
        return []
    d = json.loads(f.read_text())
    return d["episodes"] if isinstance(d, dict) else d


def eval_auc_h10(rows, gate_rows, eps):
    """前瞻判别力实测值 —— **现算**，不再手打。

    ━━ 为什么改 ━━
    原先这里是一个字面量字典，注释标注来源为 scratchpad/probe_early*.py。
    2026-09-18 核查时该脚本已不存在，而 measured_auc_h10 里的 ew_full=0.9029
    在冻结序列上**穷举 7 种合理样本构造均不能复现**（最接近 0.8834，重算 0.8749）。
    最可能的解释是探针跑在 2026-08-26 的线上数据上，而冻结数据封存于 09-08 ——
    两者本就会漂（撕裂侧实测 线上 0.7458 / 冻结 0.7440）。

    ⚠ 范围更正（2026-09-23 核实）：该手打值**没有进论文**。
      论文页的 ew_full 前瞻判别力由 gen_paper_doc.score() 在日频序列上现算，
      **不读 measured_auc_h10**，其值 0.8749 与独立重算完全一致。
      手打值喂的是 crash_panorama 探索页 5.6 节。
      所以这处硬伤的范围是**探索页而非论文** —— 但它仍然该修：
      一个读者验不了的数，放在哪里都是负债。
    撕裂侧已改为从序列重算，本函数把预警侧补齐。

    ━━ 口径 ━━
    直接复用 tearing_builder.eval_inputs —— 全篇一套评估口径，不另立一套：
        样本 = 冻结窗内、不落在任何事件区间内、且该指标有值的交易日
        标签 = 未来 EVAL_H 个交易日内有 SEVERE 事件**开始**
    每个指标各自剔除自己的缺值日，因而 n 不同；n 与 n_pos 一并写出 ——
    只给一个 auc 等于把「几个样本、几个阳性」藏起来，而它们恰恰决定这个值值不值得信。

    ━━ 不再给的三个量 ━━
    short_cover / short_open / long_unwind 是两融分量，既不在 early_warning_daily
    也不在 crash_gate_daily 里落盘，从发布包无法复算。宁可不给，
    也不留一个读者验不了的数。
    """
    import sys as _sys
    _sys.path.insert(0, str(SCRIPT_DIR))
    from tearing_builder import eval_inputs, _auc

    if not eps:
        return {}
    _, sample, lab = eval_inputs(rows, eps)
    if not sample or not any(lab):
        return {}
    G = {r["date"]: r for r in gate_rows}

    def auc_of(fn):
        pair = [(fn(r, G.get(r["date"], {})), l) for (_, r), l in zip(sample, lab)]
        ok = [(v, l) for v, l in pair if v is not None]
        if not ok:
            return None
        pos = sum(1 for _, l in ok if l)
        if pos == 0 or pos == len(ok):
            return None
        return {"auc": round(_auc([v for v, _ in ok], [l for _, l in ok]), 4),
                "n": len(ok), "n_pos": pos}

    def noT(r, k):
        """去掉 pT_fwd 后重算同一条公式 —— 用于量化 T 的实际贡献。"""
        parts = [r.get("pS"),
                 None if r.get("p_price") is None else 1 - r["p_price"]]
        if k == "full":
            parts.append(r.get("p_cover"))
        return None if any(x is None for x in parts) else sum(parts) / len(parts)

    out = {}
    for name, fn in (
            ("ew_base", lambda r, g: r.get("ew_base")),
            ("ew_full", lambda r, g: r.get("ew_full")),
            ("ew_base_noT", lambda r, g: noT(r, "base")),
            ("ew_full_noT", lambda r, g: noT(r, "full")),
            ("pT_fwd", lambda r, g: r.get("pT_fwd")),
            ("pS", lambda r, g: r.get("pS")),
            ("p_cover", lambda r, g: r.get("p_cover")),
            ("price_stress_low", lambda r, g: None if r.get("p_price") is None
             else 1 - r["p_price"]),
            ("risk_layer1", lambda r, g: g.get("risk")),
            ("T", lambda r, g: g.get("T")),
            ("sd_pct", lambda r, g: g.get("sd_pct")),
            ("margin_stress_low", lambda r, g: None if g.get("margin_stress") is None
             else -g["margin_stress"]),
    ):
        v = auc_of(fn)
        if v:
            out[name] = v
    return out


def main():
    print("═══ Layer0 前瞻状态量 ═══")
    if not GATE_JSON.exists():
        raise SystemExit(f"缺 {GATE_JSON.name} —— 先跑 crash_gate_builder.py")
    gate = sorted(json.loads(GATE_JSON.read_text())["daily"], key=lambda r: r["date"])
    dates = [r["date"] for r in gate]
    G = {r["date"]: r for r in gate}
    print(f"  Layer1 序列 {len(dates)} 天 {dates[0]} ~ {dates[-1]}")

    p_codes = set(json.loads(P_SECTOR.read_text())["stocks"])
    # 快照缺「收盘价」时回落到 OHLCV 的原始收盘价（raw：对手项是该时点的股数）
    from price_accessor import PriceAccessor
    _pa = PriceAccessor()
    _px_cache = {}

    def _px(code, date):
        if code not in _px_cache:
            try:
                _px_cache[code] = {r["date"]: r["close"] for r in _pa.raw(code)}
            except Exception:
                _px_cache[code] = {}
        return _px_cache[code].get(date)

    cover, c0, c1, n_med = load_cover_surge(p_codes, px_fallback=_px)
    print(f"  杀空通道: margin_cache {c0} ~ {c1}，surge 可算 {len(cover)} 天"
          f"（P 集合样本股中位 {n_med:.0f} 只）")

    # p_price 在完整日期轴上算；p_cover 只在有 surge 的日子上算
    p_price = dict(zip(dates, expanding_rank(
        [G[d].get("price_stress") for d in dates], WARM_PRICE)))
    cov_dates = [d for d in dates if d in cover]
    p_cover = dict(zip(cov_dates, expanding_rank(
        [cover[d] for d in cov_dates], WARM_COVER)))

    rows = []
    for d in dates:
        pS, pp, pc = G[d].get("pS"), p_price.get(d), p_cover.get(d)
        pT = G[d].get("pT")
        # Layer1 的 pT = 1−rank(T)（低 T = 危险，用于检测）。
        # 预报要的是相反方向，直接取其补 —— 同一次排序，不可能漂开。
        ptf = None if pT is None else round(1 - pT, 4)
        base = (None if None in (ptf, pS, pp)
                else round((ptf + pS + 1 - pp) / 3, 4))
        full = (None if None in (ptf, pc, pS, pp)
                else round((ptf + pc + pS + 1 - pp) / 4, 4))
        rows.append({
            "date": d,
            "pT_fwd": ptf,
            "p_cover": None if pc is None else round(pc, 4),
            "p_price": None if pp is None else round(pp, 4),
            "pS": pS,
            "ew_base": base, "ew_full": full,
            "watch": None if base is None else (base > EW_WATCH),
        })

    # 历史分位：EW 自身的扩展窗口秩。这不是阈值，是「今天在自己历史里的位置」。
    for key in ("ew_base", "ew_full"):
        pct = expanding_rank([r[key] for r in rows], WARM_PRICE)
        for r, p in zip(rows, pct):
            r[f"{key}_pctl"] = None if p is None else round(p, 4)

    n2 = sum(1 for r in rows if r["ew_base"] is not None)
    n3 = sum(1 for r in rows if r["ew_full"] is not None)
    d2 = [r["date"] for r in rows if r["ew_base"] is not None]
    d3 = [r["date"] for r in rows if r["ew_full"] is not None]

    payload = {
        "generated_at": datetime.now(BJT).strftime("%Y-%m-%d %H:%M"),
        "contract": "Layer0 买的是**提前量**不是覆盖率：WATCH 只召回 4/6 SEVERE，"
                    "但每次都提前 ≥7 个交易日。检测仍由 Layer1 负责（6/6）。"
                    "全部因果 —— 扩展窗口秩只用 <t 的历史。",
        "watch_note": "WATCH 是观察位不是 ALARM。阈值 0.66 由 LOEO 标定"
                      "（6 折 0.66~0.76，σ=0.0373）。全集：召回 4/6、报警率 29.7%、"
                      "合计提前日 37（Layer1 是 6/6 / 26.8% / 22）。详见 crash_panorama 5.6。",
        "params": {"surge_window": SURGE_W, "warmup_cover": WARM_COVER,
                   "warmup_price": WARM_PRICE, "ew_watch": EW_WATCH},
        "formulas": {
            "surge_cover": "融券偿还额(t) / mean(融券偿还额[t−60..t−1])，P 集合合计",
            "pT_fwd": "1 − pT = rank(T)　（Layer1 的 pT 用 1−rank(T)，方向相反）",
            "ew_base": "( pT_fwd + pS + (1 − p_price) ) / 3",
            "ew_full": "( pT_fwd + p_cover + pS + (1 − p_price) ) / 4",
        },
        "coverage": {
            "ew_base": [d2[0], d2[-1], n2] if d2 else None,
            "ew_full": [d3[0], d3[-1], n3] if d3 else None,
            "note": "ew_full 起点由 margin_cache 起点 − surge 窗 + 暖机决定；"
                    "EP#3(2024-02-02) 结构性落在覆盖外。",
        },
        # 实测值：**现算**，口径见 eval_auc_h10 的 docstring。
        "measured_auc_h10": eval_auc_h10(rows, gate, _episodes()),
        "daily": rows,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False))

    print(f"  ew_base {n2} 天 {d2[0]} ~ {d2[-1]}")
    print(f"  ew_full {n3} 天 {d3[0]} ~ {d3[-1]}")
    last = rows[-1]
    print(f"\n  最新 {last['date']}: pT_fwd={last['pT_fwd']}  pS={last['pS']}  "
          f"p_price={last['p_price']}  p_cover={last['p_cover']}")
    print(f"    ew_base={last['ew_base']} (历史 P{(last['ew_base_pctl'] or 0)*100:.0f})   "
          f"ew_full={last['ew_full']} (历史 P{(last['ew_full_pctl'] or 0)*100:.0f})")
    nw = sum(1 for r in rows if r["watch"])
    print(f"    WATCH(>{EW_WATCH}): 今日 {'是' if last['watch'] else '否'}"
          f"   历史 {nw}/{n2} 天 ({nw/n2*100:.1f}%)")
    print(f"  → {OUT_PATH.name} ({OUT_PATH.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
