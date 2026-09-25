#!/usr/bin/env python3
"""
freeze_industry_panel.py — 把 §5.6 的行业级证据冻结进 frozen/

━━ 为什么需要这一步 ━━
2026-09-25 把 .industry_dd_cache.json 等六份直接加进发布包，被
verify 的 release/frozen-only 守卫拦下，拦得对：那两条缓存由当前数据构建，
明天重跑就会越过冻结窗末日；发出去的数据集与论文对不上只是时间问题。

发布包的规矩是「只发 frozen/」。本脚本按同一规矩补齐 §5.6 这一块：
    两条逐行业序列  裁到 [eval_w0, eval_w1]，窗外一天不留
    四份结论 JSON   原样拷贝（它们本就只在窗内计算，且带 window 字段自证）

━━ 与 paper_freeze.py 的关系 ━━
不改 paper_freeze.py，也不重跑它 —— 重跑等于把论文里所有数字悄悄换掉。
本脚本只**新增**文件，与 augment_frozen_* 同类：既有冻结文件一个字节都不动，
脚本会在写盘前自己核对这一点。

用法: python3 stock_cache/freeze_industry_panel.py [--check]
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from gate_params import P                                   # noqa: E402

FZ = SCRIPT_DIR / "frozen"
W0, W1 = P("eval_w0"), P("eval_w1")

SERIES = [(".industry_dd_cache.json", "industry_dd_paper.json",
           "逐行业逐日回撤（相对自身 250 日峰值）"),
          (".industry_ret_cache.json", "industry_ret_paper.json",
           "逐行业逐日收益")]
RESULTS = [("industry_panel.json", "industry_panel_paper.json"),
           ("industry_panel_dd.json", "industry_panel_dd_paper.json"),
           ("industry_ident.json", "industry_ident_paper.json"),
           ("industry_econ.json", "industry_econ_paper.json")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对，不写盘")
    a = ap.parse_args()
    print(f"═══ 冻结 §5.6 行业级证据　窗口 {W0} ~ {W1} ═══")

    # 本脚本自己拥有的输出不算「既有文件」—— 否则第二次运行会把上一次的
    # 产物当成被篡改的冻结文件。守的是**别人的**文件一个字节都不许动。
    OWN = {d for _, d, _ in SERIES} | {d for _, d in RESULTS}
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in FZ.glob("*.json") if p.name not in OWN}
    out = {}
    for src_n, dst_n, desc in SERIES:
        src = SCRIPT_DIR / src_n
        if not src.exists():
            raise SystemExit(f"✗ 缺 {src_n} —— 先跑 industry_panel_study.py")
        raw = json.loads(src.read_text())
        cut = {ind: {d: v for d, v in ser.items() if W0 <= d <= W1}
               for ind, ser in raw.items()}
        cut = {i: s for i, s in cut.items() if s}
        n_in = sum(len(s) for s in cut.values())
        n_all = sum(len(s) for s in raw.values())
        out[dst_n] = json.dumps(cut, ensure_ascii=False, separators=(",", ":"))
        print(f"  {desc:<28} {len(cut):>4} 行业  {n_in:>7} 点"
              f"（原 {n_all}，裁掉 {n_all - n_in}）")

    for src_n, dst_n in RESULTS:
        src = SCRIPT_DIR / src_n
        if not src.exists():
            raise SystemExit(f"✗ 缺 {src_n}")
        d = json.loads(src.read_text())
        w = d.get("window") or (d.get("design") or {}).get("window")
        if w and (w[0] != W0 or w[1] != W1):
            raise SystemExit(f"✗ {src_n} 的窗口 {w} 与冻结窗 [{W0}, {W1}] 不符")
        out[dst_n] = src.read_text()
        print(f"  {src_n:<28} 窗口自证 {w}")

    if a.check:
        same = all((FZ / k).exists() and (FZ / k).read_text() == v
                   for k, v in out.items())
        print(f"\n  --check：frozen/ {'已是当前取值' if same else '与当前取值不一致'}")
        return 0

    for k, v in out.items():
        (FZ / k).write_text(v, encoding="utf-8")
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in FZ.glob("*.json") if p.name not in OWN}
    changed = [k for k in before if before[k] != after.get(k)]
    if changed:
        raise SystemExit(f"✗ 既有冻结文件被改动：{changed} —— 本脚本只许新增")
    print(f"\n  → frozen/ 新增 {len(out)} 份，既有文件零改动")
    return 0


if __name__ == "__main__":
    sys.exit(main())
