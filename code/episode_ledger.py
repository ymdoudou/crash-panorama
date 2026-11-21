#!/usr/bin/env python3
"""
episode_ledger.py — 生产侧的 append-only 事件台账

━━ 要解决的问题 ━━
`crash_episode_builder` 每次跑都会**重算全部历史事件**。它本身是确定性的，
但依赖人工维护的 `indexes/p_sector.json` —— 成分一变，2023 年事件的
dd_mean 能从 −22.97 跳到 −33.35，tier 跟着掉档。

把它直接放进日频管道 = 让标定基准每天静默漂移。但完全不放，未来的崩溃就
永远不会出现在生产图上。

━━ 解法：只追加，不重写 ━━
台账的规则是：**已结束的事件，指标永不重算。**

    1. 冻结区的 12 个事件先播种进台账（id 1..12，状态 frozen）
    2. 每次跑，让 builder 在**当前**数据上识别一遍
    3. **进行中**的事件（end 等于数据里最后一个交易日）每次刷新 ——
       它还没成为历史，冻住它就是冻住一个半截状态
    4. **已结束**的事件一个字段都不碰 —— 哪怕 builder 这次算出了不同的值
    5. 新事件只在与任何进行中事件都**不重叠**时才追加

━━ 2026-11-19 修的那个错 ━━
第 3、5 两条原先不存在。原规则是「只有峰日晚于台账最后一个事件的才追加」，
它隐含假设事件在追加时已经收尾。进行中的事件每加深一次谷、pk 就后移一次，
于是**同一个事件被反复追加**：2026-10-12 起的那一次深跌被记成了
#13（MODERATE，10-12~10-14）、#14（SEVERE，10-12~10-15）、
#15（SEVERE，10-12~10-30）三条，起点相同、区间层层嵌套、cat 全为 null。
而检测器在 10-14 与 10-15 两个峰日上并不识别任何事件。
公开看板把三条都当独立事件展示，线上报告写成「新增 2 个事件」。

append-only 还把进行中事件的指标也冻住了：#15 记 end 2026-10-30、
delta_dd_mean −5.10，而实际是 end 2026-11-19、−6.04 —— 低估了深度。

修法（用户定的乙案）：这是代码错误导致的错误数据，不是需要被保护的历史，
故**删除三条伪记录、next_id 回拨到 13**，由修正后的代码重新识别并记为 #13。
被删记录连同原因存进 repair_log，留审计痕迹。
配对判据也改了：进行中事件按**区间重叠**配对，不按 pk ——
pk 与 end 都会移动，start 也可能漂（见 drift_log 里 #2 的 start），
区间重叠是唯一稳的判据。

这样不要求 builder 完美因果（那是另一个更大的改造），只要求「历史不可变」。
代价是台账里早期事件的指标是「当时算出来的」，不是「今天重算的」——
这正是我们想要的：论文引用的数字必须是当时那份。

差异不丢弃：若 builder 对已有事件算出了不同的值，记进 `drift_log`，
供人核查，但不覆盖台账。

输出: stock_cache/episode_ledger.json
用法:
    python3 stock_cache/episode_ledger.py --seed     从冻结区播种（只需一次）
    python3 stock_cache/episode_ledger.py            日常追加
管道位置: m_daily_pipeline，crash_gate_builder 之后
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
BJT = ZoneInfo("Asia/Shanghai")

LEDGER = SCRIPT_DIR / "episode_ledger.json"
FROZEN_EP = SCRIPT_DIR / "frozen" / "episodes_paper.json"
CAUSAL = SCRIPT_DIR / "episodes_causal.json"
BUILDER = SCRIPT_DIR / "crash_episode_builder.py"

# 台账里参与「是否漂移」比对的字段。峰日/区间/分档是关键，其余是描述量。
CORE_FIELDS = ("tier", "pk", "start", "end", "delta_dd_mean")


def load_ledger():
    if LEDGER.exists():
        return json.loads(LEDGER.read_text())
    return None


def save(led):
    led["updated_at"] = datetime.now(BJT).strftime("%Y-%m-%d %H:%M")
    LEDGER.write_text(json.dumps(led, ensure_ascii=False, indent=1))


def seed():
    if not FROZEN_EP.exists():
        raise SystemExit(f"缺 {FROZEN_EP} —— 先跑 python3 stock_cache/paper_freeze.py")
    fr = json.loads(FROZEN_EP.read_text())
    eps = []
    for e in fr["episodes"]:
        r = {k: v for k, v in e.items()}
        r["status"] = "frozen"
        r["recorded_at"] = fr.get("frozen_window", ["", ""])[1]
        eps.append(r)
    led = {
        "contract": "append-only：写过的事件指标永不重算。新事件 id 从最后一个 +1 续编。",
        "seeded_from": "frozen/episodes_paper.json",
        "frozen_window": fr["frozen_window"],
        "n_frozen": len(eps),
        "next_id": max(e["id"] for e in eps) + 1 if eps else 1,
        "episodes": eps,
        "drift_log": [],
    }
    save(led)
    print(f"  播种 {len(eps)} 个冻结事件，下一个 id = {led['next_id']}")
    return led


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true", help="从冻结区播种（只需一次）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写台账")
    args = ap.parse_args()

    print("═══ 事件台账（append-only）═══")
    led = load_ledger()
    if args.seed or led is None:
        if led is not None and args.seed:
            raise SystemExit("  台账已存在。播种只做一次 —— 要重来请先手动删除文件。")
        led = seed()
        if args.seed:
            return

    known = {e["pk"] for e in led["episodes"]}
    last_pk = max(e["pk"] for e in led["episodes"]) if led["episodes"] else "0000-00-00"
    n_open = sum(1 for e in led["episodes"] if e.get("open"))
    print(f"  台账 {len(led['episodes'])} 个事件（进行中 {n_open}），"
          f"最后峰日 {last_pk}，下一个 id {led['next_id']}")

    # 在当前数据上重新识别（不落盘到 episodes_causal，用临时输出）
    tmp = SCRIPT_DIR / "_ledger_scan.json"
    r = subprocess.run([sys.executable, str(BUILDER), "--out", str(tmp)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-800:]); print(r.stderr[-800:])
        raise SystemExit("  crash_episode_builder 失败")
    scan = json.loads(tmp.read_text())
    cur = scan["episodes"] if isinstance(scan, dict) else scan
    # 数据里最后一个交易日。事件是否「进行中」只能由它判定 ——
    # 没有它就无法区分「事件结束了」和「数据到这里为止」。
    last_day = (scan.get("window") or [None, None])[1] if isinstance(scan, dict) else None
    tmp.unlink(missing_ok=True)
    if not last_day:
        raise SystemExit("  scan 里没有 window —— 无法判定事件是否进行中，拒绝写盘")
    print(f"  本次识别 {len(cur)} 个事件，数据末日 {last_day}")

    def overlaps(a, b):
        """两个事件区间是否重叠。进行中事件的 pk/end/start 都会动，只有重叠稳。"""
        return a["start"] <= b["end"] and b["start"] <= a["end"]

    # ① 漂移检查：已在台账里的事件，builder 这次算出的值是否变了
    by_pk = {e["pk"]: e for e in cur}
    drift = []
    for e in led["episodes"]:
        # 进行中的事件不参与漂移检查：它本来就该变，由下面的 ② 刷新。
        # 把它算成漂移会让 drift_log 每天多出一条噪音，并掩盖真正的漂移。
        if e.get("open"):
            continue
        c = by_pk.get(e["pk"])
        if not c:
            drift.append({"pk": e["pk"], "id": e["id"], "issue": "本次未识别出该事件"})
            continue
        diff = {k: [e.get(k), c.get(k)] for k in CORE_FIELDS if e.get(k) != c.get(k)}
        if diff:
            drift.append({"pk": e["pk"], "id": e["id"], "diff": diff})
    if drift:
        print(f"\n  ⚠ {len(drift)} 个已记录事件在本次识别中数值不同 —— "
              f"**台账不改**，只记进 drift_log：")
        for d in drift[:6]:
            print(f"    #{d['id']} {d['pk']}: {d.get('diff') or d.get('issue')}")
        if len(drift) > 6:
            print(f"    …… 另 {len(drift)-6} 条")
        print(f"    这正是不把 builder 放进日频管道的原因。")

    # ② 进行中事件：刷新，不冻结。它还没成为历史。
    #    配对按区间重叠，不按 pk —— pk 与 end 每天都在动。
    refreshed, closed = [], []
    for e in led["episodes"]:
        if not e.get("open"):
            continue
        m = next((c for c in cur if overlaps(e, c)), None)
        if m is None:
            # 检测器不再识别它：事件已退出（可能因阈值回落），就地封存
            e["open"] = False
            e["closed_at"] = datetime.now(BJT).strftime("%Y-%m-%d")
            e["close_why"] = "本次识别中已不成立，按最后一次取值封存"
            closed.append(e)
            continue
        before = {k: e.get(k) for k in CORE_FIELDS}
        for k in CORE_FIELDS + ("dd_mean", "cracking_pct", "d20_pct", "score",
                                "pk_week", "pk_n"):
            if k in m:
                e[k] = m[k]
        e["refreshed_at"] = datetime.now(BJT).strftime("%Y-%m-%d")
        if m["end"] < last_day:          # 不再延长到数据末日 ⇒ 已收尾
            e["open"] = False
            e["closed_at"] = datetime.now(BJT).strftime("%Y-%m-%d")
            e["close_why"] = f"end {m['end']} 早于数据末日 {last_day}，视为已结束"
            closed.append(e)
        after = {k: e.get(k) for k in CORE_FIELDS}
        if before != after:
            refreshed.append((e["id"], {k: [before[k], after[k]]
                                        for k in CORE_FIELDS
                                        if before[k] != after[k]}))
    for i, diff in refreshed:
        print(f"  ↻ #{i} 进行中，已刷新：{diff}")
    for e in closed:
        print(f"  ■ #{e['id']} 已结束并封存：{e['start']} ~ {e['end']} "
              f"{e['tier']}（{e['close_why']}）")

    # ③ 追加：与任何**进行中**事件都不重叠，且峰日晚于台账最后一个
    open_eps = [e for e in led["episodes"] if e.get("open")]
    new = [c for c in cur
           if c["pk"] > last_pk and c["pk"] not in known
           and not any(overlaps(e, c) for e in open_eps)]
    skipped = [c for c in cur
               if c["pk"] > last_pk and c["pk"] not in known
               and any(overlaps(e, c) for e in open_eps)]
    for c in skipped:
        print(f"  ∅ 不追加 {c['start']} ~ {c['end']}：与进行中事件重叠，"
              f"同一次下跌不记两条")
    if new:
        print(f"\n  新事件 {len(new)} 个：")
        for c in sorted(new, key=lambda x: x["pk"]):
            rec = {k: v for k, v in c.items()}
            rec["src_id"] = c["id"]
            rec["id"] = led["next_id"]
            rec["status"] = "appended"
            rec["recorded_at"] = datetime.now(BJT).strftime("%Y-%m-%d")
            rec["cat"] = None
            rec["cat_why"] = "形态分类需周度全景 D 覆盖该周，追加时暂缺"
            rec["open"] = (c["end"] >= last_day)
            if rec["open"]:
                rec["status"] = "appended-open"
            led["episodes"].append(rec)
            print(f"    + #{rec['id']} {rec['tier']:<9} {rec['pk']}  "
                  f"{rec['start']} ~ {rec['end']}")
            led["next_id"] += 1
    else:
        # 这句话曾经是错的：last_pk 在 ② 刷新之前就算好了，刷新后台账的峰日
        # 已经前移，于是日志会写出「11-24 ≤ 10-30」这种自相矛盾的话。
        # 误导性的日志正是掩盖 bug 的东西，故现读台账。
        _lp = max((e["pk"] for e in led["episodes"]), default="—")
        _cp = max((c["pk"] for c in cur), default="—")
        print(f"\n  无新事件（本次识别的最晚峰日 {_cp}，"
              f"台账最晚 {_lp}，无与进行中事件不重叠的新区间）")

    if drift:
        led["drift_log"].append({
            "at": datetime.now(BJT).strftime("%Y-%m-%d %H:%M"),
            "n": len(drift), "items": drift,
        })
        led["drift_log"] = led["drift_log"][-20:]      # 只留最近 20 次

    if args.dry_run:
        print("\n  --dry-run：未写盘")
        return
    save(led)
    print(f"\n  → {LEDGER.name}  共 {len(led['episodes'])} 个事件"
          f"（frozen {sum(1 for e in led['episodes'] if e['status']=='frozen')} / "
          f"已结束追加 {sum(1 for e in led['episodes'] if e['status'].startswith('appended') and not e.get('open'))} / "
          f"进行中 {sum(1 for e in led['episodes'] if e.get('open'))}）")


if __name__ == "__main__":
    main()
