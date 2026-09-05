#!/usr/bin/env python3
"""
gate_params.py — 三层监测系统全部阈值的唯一权威源

━━ 为什么要有这个文件 ━━
阈值原本以字面量写在各 builder 里，同时 recalibrate_frozen 又反过来从
builder 模块读它们去生成 frozen/calibration.json —— 这条链是循环的：
「权威值」既在代码里，又在数据里，谁改了谁没人说得清。

论文要随文发布构造代码，这个循环就必须解开：
**参数与实现分离** —— 代码公开，读者能重跑、能自行重标，
但拿到的不是一套调好的现成参数。参数在 gate_params.json 里，
每一个都带出处与标定方式，改它不需要碰代码。

━━ 谁读它 ━━
    crash_gate_builder      W_T / RISK_ALARM / BAND_* / SHIFT_S0 / RANK_WARMUP
    early_warning_builder   EW_WATCH / SURGE_W / WARM_COVER / WARM_PRICE
    tearing_builder         RANK_WARMUP / EVAL_H / EVAL_WINDOW
    cap_history_builder     COVER_MIN
    views/recalibrate_frozen  生产常数一栏（不再从 builder 模块反读）

━━ 缺文件时的行为 ━━
返回 DEFAULTS 里的同名值并打印一行提示。生产不会因为少一个文件而停摆，
但校验会发现「实际取值 ≠ 文件」并 FAIL。

用法:
    from gate_params import P
    P("risk_alarm")            # 0.65
    P("w_T")                   # 0.45
    python3 stock_cache/gate_params.py     # 打印全部参数与出处
"""

import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# 生产布局是 stock_cache/ 平铺；论文发布包把代码与数据拆成 code/ 与 data/。
# 两种布局都要能找到参数文件，否则读者拿到的发布包会静默回落到 DEFAULTS，
# 与「阈值不硬编码在代码里」的主张相矛盾。
_CANDIDATES = [SCRIPT_DIR / "gate_params.json",
               SCRIPT_DIR.parent / "data" / "gate_params.json"]
PARAMS_PATH = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

# 文件缺失时的兜底。与 gate_params.json 必须一致 —— 校验会比对。
DEFAULTS = {
    "w_T": 0.45, "risk_alarm": 0.65, "shift_s0": 0.15,
    "band_elevated": 0.20, "band_critical": 0.40, "rank_warmup": 250,
    "ew_watch": 0.66, "surge_w": 60, "warm_cover": 120, "warm_price": 250,
    "eval_h": 10, "eval_w0": "2024-01-01", "eval_w1": "2026-08-26",
    "cover_min": 0.70,
}

_cache = None
_warned = False


def _load():
    global _cache, _warned
    if _cache is not None:
        return _cache
    if PARAMS_PATH.exists():
        raw = json.loads(PARAMS_PATH.read_text())
        _cache = {k: v["value"] for k, v in raw.get("params", {}).items()}
    else:
        if not _warned:
            print(f"  ⚠ 缺 {PARAMS_PATH.name}，阈值回落到 DEFAULTS")
            _warned = True
        _cache = dict(DEFAULTS)
    return _cache


def P(key):
    """取一个参数。未在文件中登记则回落 DEFAULTS，再无则 KeyError。"""
    v = _load().get(key)
    return DEFAULTS[key] if v is None else v


def all_params():
    return dict(_load())


def meta():
    if not PARAMS_PATH.exists():
        return {}
    return json.loads(PARAMS_PATH.read_text())


if __name__ == "__main__":
    m = meta()
    print("═══ 三层监测系统的阈值 ═══")
    if not m:
        print("  gate_params.json 不存在，以下为 DEFAULTS")
        for k, v in DEFAULTS.items():
            print(f"  {k:<16}{v}")
    else:
        print(f"  {m.get('note','')}\n")
        for k, v in m["params"].items():
            print(f"  {k:<16}{str(v['value']):<14}{v.get('用途','')}")
            if v.get("标定"):
                print(f"  {'':<16}标定: {v['标定']}")
    bad = [k for k, v in DEFAULTS.items() if _load().get(k, v) != v]
    print(f"\n  文件与 DEFAULTS 的差异: {bad or '无'}")
