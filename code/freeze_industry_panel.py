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

# WARMUP 的那一项：窗口前还要留多少个**该行业自己的**观测。
# 2026-11-21 加。第三个事件口径（Yeganeh 的 E2）要把收盘价和它前 jump_w 期的
# 最低价比，于是窗口内第一天就需要窗口**之前**的 jump_w 个观测。按窗口精确裁
# 之后，发布包里这一行的结果整组复算不出来（dd 0.5889 对归档 0.6197）。
#
# 两个坑都踩过：
#   · 余量按**所有行业日期的并集**取 24 天不够 —— 各行业自身的交易日集合不同，
#     覆盖稀疏的行业拿不到属于它自己的 24 个观测（dd 0.6144，仍对不上）。
#     必须逐行业取它自己的前 N 个观测。
#   · 只有 ret 需要预热。dd 与 d 是逐点量，按窗口精确裁不影响任何结果，
#     实测用包内的 dd 配生产版 ret 即可逐项复现。
# N 取 2×jump_w：够用的下界是 jump_w，留一倍余量，且取值随判据走不写死。
SERIES = [(".industry_dd_cache.json", "industry_dd_paper.json",
           "逐行业逐日回撤（相对自身 250 日峰值）", 0),
          (".industry_ret_cache.json", "industry_ret_paper.json",
           "逐行业逐日收益（含窗口前预热）", None)]
# 从**已冻结**文件派生，不是从日频缓存裁窗。
# 2026-11-18 补：发布包里没有逐行业的 d 序列，于是 §5.6 的 AUC 与置换 p
# 在包内无法复算 —— FINR 审稿人 1 正是要求能验这两个数。t_causal_paper.json
# 有 23 MB，整份进包不现实；而面板只用到其中的 r（d = r − 1），裁窗后 1.3 MB。
FROM_FROZEN = [("t_causal_paper.json", "industry_d_paper.json",
                "逐行业逐日估值偏离 d 的来源 r（d = r − 1）")]
# 2026-11-19 加这两份：审稿人 1 要的 Friedman/Wilcoxon 与审稿人 2 要的
# cap 参数敏感性扫描。两者都是**结论 JSON**，自带 window 或 design 字段自证。
RESULTS = [("industry_nonparam.json", "industry_nonparam_paper.json"),
           ("cap_scan_ratios.json", "cap_scan_ratios_paper.json"),
           ("cap_sensitivity.json", "cap_sensitivity_paper.json"),
           ("industry_panel.json", "industry_panel_paper.json"),
           ("industry_panel_dd.json", "industry_panel_dd_paper.json"),
           # 2026-11-20 加：第三个事件口径（Yeganeh et al. 2026 的 E2）。
           # §5.6 用它检验「结论是否我们自己的选择造出来的」。
           ("industry_panel_jump.json", "industry_panel_jump_paper.json"),
           ("industry_ident.json", "industry_ident_paper.json"),
           ("industry_econ.json", "industry_econ_paper.json")]


def jump_warmup():
    """窗口前要留多少个观测，由第三个事件口径的回溯窗决定，不写死。

    取值来自 industry_panel_jump.json 的 design.jump_w —— 那是跑出归档结果的
    那一次用的真值。若口径的回溯窗改了而这里不跟着改，发布包里那一行就会
    悄悄复算不出来，所以让它跟着判据走。
    """
    f = SCRIPT_DIR / "industry_panel_jump.json"
    if not f.exists():
        raise SystemExit("✗ 缺 industry_panel_jump.json —— "
                         "先跑 industry_panel_study.py --event-mode jump")
    w = ((json.loads(f.read_text()).get("design") or {}).get("jump_w"))
    if not isinstance(w, int) or w <= 0:
        raise SystemExit("✗ industry_panel_jump.json 的 design.jump_w 不可用")
    return 2 * w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对，不写盘")
    a = ap.parse_args()
    print(f"═══ 冻结 §5.6 行业级证据　窗口 {W0} ~ {W1} ═══")

    # 本脚本自己拥有的输出不算「既有文件」—— 否则第二次运行会把上一次的
    # 产物当成被篡改的冻结文件。守的是**别人的**文件一个字节都不许动。
    OWN = ({d for _, d, _, _ in SERIES} | {d for _, d in RESULTS} |
           {d for _, d, _ in FROM_FROZEN})
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in FZ.glob("*.json") if p.name not in OWN}
    warm_n = jump_warmup()
    out = {}
    for src_n, dst_n, desc, warm in SERIES:
        src = SCRIPT_DIR / src_n
        if not src.exists():
            raise SystemExit(f"✗ 缺 {src_n} —— 先跑 industry_panel_study.py")
        n = warm_n if warm is None else warm
        raw = json.loads(src.read_text())
        cut = {}
        for ind, ser in raw.items():
            ds = sorted(ser)
            inw = [d for d in ds if W0 <= d <= W1]
            if not inw:
                continue
            # 逐行业取它**自己的**前 n 个观测，不是按并集日期取
            pre = [d for d in ds if d < W0][-n:] if n else []
            cut[ind] = {d: ser[d] for d in pre + inw}
        n_in = sum(len(s) for s in cut.values())
        n_pre = sum(sum(1 for d in s if d < W0) for s in cut.values())
        n_all = sum(len(s) for s in raw.values())
        out[dst_n] = json.dumps(cut, ensure_ascii=False, separators=(",", ":"))
        extra = f"，其中预热 {n_pre}" if n_pre else ""
        print(f"  {desc:<28} {len(cut):>4} 行业  {n_in:>7} 点"
              f"（原 {n_all}{extra}）")

    for src_n, dst_n, desc in FROM_FROZEN:
        src = FZ / src_n
        if not src.exists():
            raise SystemExit(f"✗ 缺 frozen/{src_n} —— 先跑 paper_freeze.py")
        raw = json.loads(src.read_text())["industries"]
        cut = {}
        for ind, v in raw.items():
            # 连 r 为 null 的日期一并保留：只多 26 个点、体积不变，
            # 而形状与 t_causal 完全一致，消费方不必为副本写第二套分支。
            ser = {d: rec.get("r") for d, rec in (v.get("dates") or {}).items()
                   if W0 <= d <= W1}
            if ser:
                cut[ind] = ser
        n_in = sum(len(s_) for s_ in cut.values())
        out[dst_n] = json.dumps(cut, ensure_ascii=False, separators=(",", ":"))
        print(f"  {desc:<28} {len(cut):>4} 行业  {n_in:>7} 点"
              f"（源 {src.stat().st_size // 1024 // 1024} MB "
              f"→ {len(out[dst_n]) // 1024} KB）")

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
