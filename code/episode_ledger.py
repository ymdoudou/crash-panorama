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
台账的规则只有一条：**写过的事件，指标永不重算。**

    1. 冻结区的 12 个事件先播种进台账（id 1..12，状态 frozen）
    2. 每次跑，让 builder 在**当前**数据上识别一遍
    3. 只有峰日晚于台账最后一个事件的，才作为新事件追加（id 从 13 续编）
    4. 已在台账里的，一个字段都不碰 —— 哪怕 builder 这次算出了不同的值

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
    print(f"  台账 {len(led['episodes'])} 个事件，最后峰日 {last_pk}，下一个 id {led['next_id']}")

    # 在当前数据上重新识别（不落盘到 episodes_causal，用临时输出）
    tmp = SCRIPT_DIR / "_ledger_scan.json"
    r = subprocess.run([sys.executable, str(BUILDER), "--out", str(tmp)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-800:]); print(r.stderr[-800:])
        raise SystemExit("  crash_episode_builder 失败")
    scan = json.loads(tmp.read_text())
    cur = scan["episodes"] if isinstance(scan, dict) else scan
    tmp.unlink(missing_ok=True)
    print(f"  本次识别 {len(cur)} 个事件")

    # ① 漂移检查：已在台账里的事件，builder 这次算出的值是否变了
    by_pk = {e["pk"]: e for e in cur}
    drift = []
    for e in led["episodes"]:
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

    # ② 追加：只收峰日严格晚于台账最后一个的
    new = [c for c in cur if c["pk"] > last_pk and c["pk"] not in known]
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
            led["episodes"].append(rec)
            print(f"    + #{rec['id']} {rec['tier']:<9} {rec['pk']}  "
                  f"{rec['start']} ~ {rec['end']}")
            led["next_id"] += 1
    else:
        print(f"\n  无新事件（本次识别的最晚峰日 "
              f"{max((c['pk'] for c in cur), default='—')} ≤ 台账 {last_pk}）")

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
          f"appended {sum(1 for e in led['episodes'] if e['status']=='appended')}）")


if __name__ == "__main__":
    main()
