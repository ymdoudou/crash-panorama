#!/usr/bin/env python3
"""
crash_episode_builder.py — 崩溃事件识别（因果口径，Layer1 标定的上游）

━━ 定位 ━━
研究链路是「数据根基 → 事件识别 → 阈值标定」。本脚本是第二步：在因果数据集上
识别崩溃事件，产出 `episodes_causal.json`，供 `gate_calibrator.py --episodes` 消费。

现行的 `crash_episodes.json` 那 9 个事件是在**旧（受污染）数据集**上标定的、
全仓无生成脚本、且只覆盖 2024-02 起。本脚本把该方法产品化并搬到因果数据上。

━━ 为什么复用原有 5 通道，而不是重新发明 ━━
原方法在 `For_processing/exp_crash_clustering.py`，三点值得保留：

  1. **零 T 依赖** —— 5 个通道全是价格/宽度类，T 只被读入做描述字段，不参与判别。
     这是它能当 Gate 标定标签的前提（否则就是循环论证）。
  2. **滚动 z-score** —— 每周相对自己近 12 周的历史标准化，解决了「平静期的小回调
     与波动期的大崩溃拿同样高分」的问题。
  3. **tier 由 delta_dd_mean 分档** —— 实测与现有 9 个事件的 tier 一致性 9/9。
     衡量的是**回撤加深速度**（单周百分点），不是回撤深度。

━━ 五个通道（权重来自原方法）━━
    cracking_pct   0.25   板块单周回撤加深 ΔDD ≤ −5% 的占比
    dd_accel       0.25   max(0, −Δdd_mean)，回撤加速度
    d20_breadth    0.15   回撤 < −20% 的个股占比
    pk_density     0.15   本周触及 250 日回撤新低的个股数
    crack_sustain  0.20   cracking_pct 的 3 周均值

    composite(w) = Σ max(0, z_ch(w)) × weight      z 相对该通道近 12 周

━━ 与原方法的三处修正 ━━

  A. **输入改为因果数据集** —— 原方法从 `const D`（冻结在 HTML 里的数据）读；
     本脚本直接从 `ohlcv_cache`（未复权）+ `p_sector.json` 重算。

  B. **阈值可选因果模式** —— 原方法用 `crash_th = max(p87, 0.6)`，p87 是**全样本
     87 分位**，属 as-of-today 污染：窗口一变阈值就变，历史事件集合跟着变。
     `--threshold-mode fixed`（默认）改用固定 0.6 / 0.35；`rolling` 用滚动分位；
     `quantile` 保留原行为供对照。

  C. **峰值周 → 峰值日** —— 取峰值周内 dd_mean 最深的那个交易日，
     与 `crash_episodes.json` 的日粒度对齐。

  E. **事件合并与最小强度门槛** —— 原方法只做周度打标，**不产出事件清单**；
     `crash_episodes.json` 那 9 个是人工策展的（全仓无生成脚本）。算法版若直接把
     每段连续 CRASH 周当一个事件，会得到约 2 倍数量的碎片事件。故补两条规则：
       · 相隔 ≤ MERGE_GAP 周的 CRASH 段合并为同一事件（崩溃常有反复）
       · 峰值周 delta_dd_mean 需 ≤ MIN_DELTA，滤掉够不上「事件」的小波动
     两个参数都可从命令行调，标定时应连同 Gate 阈值一起做敏感性检查。

  D. **回撤须做送转调整** —— 这一点与 PE 相反，容易搞错：
       PE  衡量估值 → 用未复权价 × 当期股本（t_causal_builder）
       dd  衡量持有收益 → 必须消除送转造成的价格阶跃
     未复权价直接算 dd，10送10 会被记成 −50% 的虚假回撤（12.1% 的个股有送转）。
     故用 `close_raw(t) / F(t→now)`，F 由 share_factor 从 xdxr 取，即「除权不除息」口径。
     该式对未来新增送转是稳定的：新事件会给窗口内所有日期乘同一因子，在比值中对消。
     现金分红不调整 —— A股股息率量级 1~3%，远小于送转，且调整会重新引入历史重述。

━━ 用法 ━━
    python3 stock_cache/crash_episode_builder.py
    python3 stock_cache/crash_episode_builder.py --threshold-mode quantile   # 对照原行为
    python3 stock_cache/crash_episode_builder.py --start 2023-07-04
    python3 stock_cache/crash_episode_builder.py --validate                  # 与现有 9 事件对照

输出: stock_cache/episodes_causal.json
"""

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
BASE = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
from share_factor import ShareFactor
BJT = ZoneInfo("Asia/Shanghai")

OHLCV_DIR = BASE / "ohlcv_cache"
P_SECTOR = SCRIPT_DIR / "indexes" / "p_sector.json"
BW_CLASS = SCRIPT_DIR / "indexes" / "bw_classification.json"
LEGACY_EPISODES = SCRIPT_DIR / "crash_episodes.json"
OUT_PATH = SCRIPT_DIR / "episodes_causal.json"

DD_LOOKBACK = 250        # 回撤基准：过去 250 个交易日最高收盘
DD20_THRESH = -20        # d20_breadth 的深回撤线
CRACK_THRESH = -5        # 板块单周 ΔDD ≤ 此值记为 cracking
ROLL = 12                # z-score 回看周数
MIN_SECTORS = 10         # 有效板块数下限
MERGE_GAP = 3            # 相隔 ≤ 此周数的 CRASH 段合并为同一事件
MIN_DELTA = -2.0         # 峰值周 delta_dd_mean 需 ≤ 此值才算事件

CHANNELS = [
    ("cracking_pct",  lambda f: f["cracking_pct"],            0.25),
    ("dd_accel",      lambda f: max(0, -f["delta_dd_mean"]),  0.25),
    ("d20_breadth",   lambda f: f["d20_pct"],                 0.15),
    ("pk_density",    lambda f: f["pk_n"],                    0.15),
    ("crack_sustain", lambda f: f["cracking_3w"],             0.20),
]

FIXED_CRASH_TH, FIXED_TRANS_TH = 0.6, 0.35

# tier 由峰值周的 delta_dd_mean 分档（与现有 9 事件一致性 9/9）
TIER_SEVERE, TIER_MODERATE = -5.0, -3.0


def isoweek(d):
    y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
    return f"{y}-W{w:02d}"


def load_universe():
    ps = json.loads(P_SECTOR.read_text())
    sector_of = {c: v["sector"] for c, v in ps["stocks"].items() if v.get("sector")}
    zones = json.loads(BW_CLASS.read_text())["sector_zones"]["map"]
    return sector_of, zones


def load_closes(codes, start):
    """{code: {date: close}} —— 未复权收盘价。"""
    out = {}
    for code in codes:
        fp = OHLCV_DIR / f"{code}.csv"
        if not fp.exists():
            continue
        series = {}
        with open(fp) as f:
            rd = csv.DictReader(f)
            if not rd.fieldnames or "date" not in rd.fieldnames:
                continue
            for r in rd:
                d = r.get("date", "")
                if not d:
                    continue
                try:
                    c = float(r.get("close") or 0)
                except (TypeError, ValueError):
                    continue
                if c > 0:
                    series[d] = c
        if series:
            out[code] = series
    return out


def daily_drawdowns(closes, all_dates, sf, pa=None):
    """{code: {date: (dd, is_newlow)}} —— dd 相对过去 DD_LOOKBACK 日最高收盘。

    价格走 price_accessor.split_adjusted（除权不除息），否则 10送10 会变成
    −50% 的虚假回撤。

    ━━ 2026-08-27 重构 ━━
    此前这里内联了一遍 `f_adj = Π{mult : 权益登记日 > date}` 的循环。同一段逻辑
    当时散落在 price_accessor / 本文件 / margin_fetcher / overfit_backtest 四处 ——
    正是 cap 当年「散落五处、四种口径」那个问题在价格侧的重演。
    现全部收拢到 price_accessor，它是价格口径的唯一入口。

    重构前后**逐字节验证过输出不变**：本文件是冻结基准的生产者，
    数值若有任何变化，全部阈值都要重标。
    """
    if pa is None:
        import sys as _s
        _s.path.insert(0, str(SCRIPT_DIR))
        from price_accessor import PriceAccessor
        pa = PriceAccessor()
    out = {}
    for code, series in closes.items():
        adj = {r["date"]: r["close"] for r in pa.split_adjusted(code)}
        ds = sorted(d for d in series if d in adj)
        vals, dd_map, run_min = [], {}, None
        for d in ds:
            vals.append(adj[d])
            if len(vals) < 20:
                continue
            peak = max(vals[-DD_LOOKBACK:])
            dd = (vals[-1] / peak - 1) * 100
            newlow = run_min is None or dd < run_min
            if newlow:
                run_min = dd
            dd_map[d] = (dd, newlow)
        if dd_map:
            out[code] = dd_map
    return out


def weekly_features(dd_by_code, sector_of, all_dates):
    """按周聚合出 5 通道所需的全部特征。"""
    weeks = defaultdict(list)
    for d in all_dates:
        weeks[isoweek(d)].append(d)
    wk_list = sorted(weeks)

    feats, sector_dd_prev = {}, {}
    crack_hist = []
    for w in wk_list:
        days = sorted(weeks[w])
        last = days[-1]
        per_stock, newlows = [], 0
        sector_vals = defaultdict(list)
        for code, dd_map in dd_by_code.items():
            row = None
            for d in reversed(days):          # 该周最后一个有数据的交易日
                if d in dd_map:
                    row = dd_map[d]
                    break
            if row is None:
                continue
            dd, nl = row
            per_stock.append(dd)
            if nl:
                newlows += 1
            sec = sector_of.get(code)
            if sec:
                sector_vals[sec].append(dd)
        if len(per_stock) < 50:
            continue

        sector_dd = {s: statistics.median(v) for s, v in sector_vals.items() if len(v) >= 3}
        if len(sector_dd) < MIN_SECTORS:
            continue

        n_crack = sum(1 for s, v in sector_dd.items()
                      if s in sector_dd_prev and (v - sector_dd_prev[s]) <= CRACK_THRESH)
        cracking_pct = 100 * n_crack / len(sector_dd)
        crack_hist.append(cracking_pct)

        dd_mean = statistics.median(per_stock)
        prev = feats[wk_list[wk_list.index(w) - 1]] if feats and wk_list.index(w) > 0 else None
        prev_f = None
        for pw in reversed(wk_list[:wk_list.index(w)]):
            if pw in feats:
                prev_f = feats[pw]
                break

        feats[w] = {
            "week": w, "date": last,
            "dd_mean": round(dd_mean, 2),
            "delta_dd_mean": round(dd_mean - prev_f["dd_mean"], 2) if prev_f else 0.0,
            "d20_pct": round(100 * sum(1 for x in per_stock if x < DD20_THRESH) / len(per_stock), 1),
            "pk_n": newlows,
            "cracking_pct": round(cracking_pct, 1),
            "cracking_3w": round(statistics.mean(crack_hist[-3:]), 1),
            "n_stocks": len(per_stock), "n_sectors": len(sector_dd),
            "days": days,
            "sector_dd": sector_dd,
        }
        sector_dd_prev = sector_dd
    return feats


def composite_scores(feats):
    weeks = sorted(feats)
    scores = {}
    for i, w in enumerate(weeks):
        f = feats[w]
        total = 0.0
        for name, fn, weight in CHANNELS:
            hist = [fn(feats[weeks[j]]) for j in range(max(0, i - ROLL), i)]
            if len(hist) >= 4:
                mu = statistics.mean(hist)
                sd = statistics.stdev(hist) if len(hist) > 1 else 0
                z = (fn(f) - mu) / sd if sd > 1e-6 else 0
            else:
                z = 0
            total += max(0, z) * weight
        scores[w] = round(total, 3)
    return scores


def thresholds(scores, mode):
    """返回 {week: (crash_th, trans_th)}。"""
    weeks = sorted(scores)
    if mode == "fixed":
        return {w: (FIXED_CRASH_TH, FIXED_TRANS_TH) for w in weeks}
    if mode == "quantile":       # 原方法：全样本分位，as-of-today
        vals = sorted(scores.values())
        c = max(vals[int(len(vals) * 0.87)], FIXED_CRASH_TH)
        t = max(vals[int(len(vals) * 0.78)], FIXED_TRANS_TH)
        return {w: (c, t) for w in weeks}
    # rolling：只用该周之前的历史分位，因果
    out = {}
    for i, w in enumerate(weeks):
        hist = sorted(scores[weeks[j]] for j in range(0, i))
        if len(hist) >= 26:
            c = max(hist[int(len(hist) * 0.87)], FIXED_CRASH_TH)
            t = max(hist[int(len(hist) * 0.78)], FIXED_TRANS_TH)
        else:
            c, t = FIXED_CRASH_TH, FIXED_TRANS_TH
        out[w] = (c, t)
    return out


def tier_of(delta):
    if delta <= TIER_SEVERE:
        return "SEVERE"
    if delta <= TIER_MODERATE:
        return "MODERATE"
    return "MILD"


def build_episodes(feats, scores, ths, dd_daily_mean, merge_gap=MERGE_GAP, min_delta=MIN_DELTA):
    """连续 CRASH 周聚合成事件；峰值周取合成分最高，峰值日取该周 dd 最深那天。"""
    weeks = sorted(scores)
    labels = {w: ("CRASH" if scores[w] >= ths[w][0]
                  else "TRANSITION" if scores[w] >= ths[w][1] else "BASELINE")
              for w in weeks}
    crash_idx = [i for i, w in enumerate(weeks) if labels[w] == "CRASH"]
    eps, cur = [], []
    for i in crash_idx:
        if cur and i - cur[-1] > merge_gap:      # 间隔超过 merge_gap 才断开
            eps.append(cur)
            cur = []
        cur.append(i)
    if cur:
        eps.append(cur)
    eps = [[weeks[i] for i in range(g[0], g[-1] + 1)] for g in eps]

    out = []
    for span in eps:
        pk_w = max(span, key=lambda w: scores[w])
        f = feats[pk_w]
        if f["delta_dd_mean"] > min_delta:        # 强度不足，不计为事件
            continue
        pk_day = min(f["days"], key=lambda d: dd_daily_mean.get(d, 0))
        out.append({
            "id": len(out) + 1, "pk": pk_day, "pk_week": pk_w,
            "tier": tier_of(f["delta_dd_mean"]),
            "delta_dd_mean": f["delta_dd_mean"],
            "dd_mean": f["dd_mean"],
            "cracking_pct": f["cracking_pct"],
            "d20_pct": f["d20_pct"],
            "pk_n": f["pk_n"],
            "score": scores[pk_w],
            "weeks": span,
            "start": feats[span[0]]["days"][0],
            "end": feats[span[-1]]["days"][-1],
        })
    return out, labels


def validate(eps):
    legacy = json.loads(LEGACY_EPISODES.read_text())
    print(f"\n【与现有 crash_episodes.json 对照】—— sanity check，非拟合目标")
    print(f"  现有 {len(legacy)} 个 / 新识别 {len(eps)} 个")
    new_by_day = {e["pk"]: e for e in eps}
    print(f"  {'旧EP':<6}{'旧峰值日':<13}{'旧tier':<10}{'最近新事件':<13}{'相距(日历日)':<14}{'新tier'}")
    for L in legacy:
        best, gap = None, None
        for e in eps:
            g = abs((datetime.strptime(e["pk"], "%Y-%m-%d")
                     - datetime.strptime(L["pk"], "%Y-%m-%d")).days)
            if gap is None or g < gap:
                best, gap = e, g
        if best:
            mark = "✓" if gap <= 7 else ("~" if gap <= 21 else "✗")
            print(f"  #{L['id']:<5}{L['pk']:<13}{L['tier']:<10}{best['pk']:<13}"
                  f"{str(gap) + ' ' + mark:<14}{best['tier']}")
        else:
            print(f"  #{L['id']:<5}{L['pk']:<13}{L['tier']:<10}{'—':<13}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-07-04")
    ap.add_argument("--threshold-mode", default="fixed",
                    choices=["fixed", "rolling", "quantile"],
                    help="fixed/rolling 因果；quantile 是原方法（全样本分位，as-of-today）")
    ap.add_argument("--merge-gap", type=int, default=MERGE_GAP,
                    help="相隔 ≤N 周的 CRASH 段合并为同一事件")
    ap.add_argument("--min-delta", type=float, default=MIN_DELTA,
                    help="峰值周 delta_dd_mean 门槛，> 此值不计为事件")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args()

    print("═══ 崩溃事件识别（因果口径）═══")
    sector_of, zones = load_universe()
    print(f"  P 集合 {len(sector_of)} 只 / {len(set(sector_of.values()))} 板块")

    closes = load_closes(list(sector_of), args.start)
    all_dates = sorted({d for s in closes.values() for d in s if d >= args.start})
    print(f"  收盘价（未复权）{len(closes)} 只，交易日 {len(all_dates)} 天 "
          f"({all_dates[0]} ~ {all_dates[-1]})")

    sf = ShareFactor()
    n_split = sum(1 for c in closes if sf.factor(c, args.start) > 1.0)
    print(f"  送转调整: {n_split} 只在窗口内有送转/配股")
    dd_by_code = daily_drawdowns(closes, all_dates, sf)
    dd_daily_mean = {}
    for d in all_dates:
        v = [dd_by_code[c][d][0] for c in dd_by_code if d in dd_by_code[c]]
        if v:
            dd_daily_mean[d] = statistics.median(v)

    feats = weekly_features(dd_by_code, sector_of, all_dates)
    print(f"  周度特征 {len(feats)} 周")

    scores = composite_scores(feats)
    ths = thresholds(scores, args.threshold_mode)
    eps, labels = build_episodes(feats, scores, ths, dd_daily_mean,
                                 args.merge_gap, args.min_delta)

    from collections import Counter
    print(f"  阈值模式 [{args.threshold_mode}]  "
          f"CRASH 周 {sum(1 for v in labels.values() if v == 'CRASH')} / "
          f"TRANSITION {sum(1 for v in labels.values() if v == 'TRANSITION')} / "
          f"BASELINE {sum(1 for v in labels.values() if v == 'BASELINE')}")
    print(f"\n识别到 {len(eps)} 个事件  {dict(Counter(e['tier'] for e in eps))}")
    print(f"  {'ID':<4}{'峰值日':<13}{'tier':<10}{'Δdd':>7}{'dd均值':>8}{'crack%':>8}"
          f"{'d20%':>7}{'新低数':>7}{'分数':>7}  区间")
    for e in eps:
        print(f"  #{e['id']:<3}{e['pk']:<13}{e['tier']:<10}{e['delta_dd_mean']:>7.1f}"
              f"{e['dd_mean']:>8.1f}{e['cracking_pct']:>8.1f}{e['d20_pct']:>7.1f}"
              f"{e['pk_n']:>7}{e['score']:>7.2f}  {e['start']}~{e['end']}")

    payload = {
        "generated_at": datetime.now(BJT).strftime("%Y-%m-%d %H:%M"),
        "method": "5-channel rolling z-score on causal dataset",
        "threshold_mode": args.threshold_mode,
        "params": {"dd_lookback": DD_LOOKBACK, "dd20": DD20_THRESH,
                   "crack": CRACK_THRESH, "roll": ROLL,
                   "tier_bands": {"SEVERE": TIER_SEVERE, "MODERATE": TIER_MODERATE},
                   "channels": {n: w for n, _, w in CHANNELS}},
        "window": [all_dates[0], all_dates[-1]],
        "episodes": [{k: v for k, v in e.items() if k != "weeks"} for e in eps],
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\n  输出: {Path(args.out).name}")

    if args.validate:
        validate(eps)
    print()


if __name__ == "__main__":
    main()
