#!/usr/bin/env python3
"""
tech_lock.py — 科技/传统行业切分的唯一指纹源

━━ 为什么需要一把锁 ━━
`bw_classification.tech_industries` 决定两件事：
    · tearing_builder 的 gap = median{d_i : 科技} − median{d_i : 传统}
    · concentration_updater 的 BW 成交额 / 两融占比
两个消费端都是**长序列**，且 concentration_updater 是增量追加的 ——
名单改了不整段重建，历史按旧切分、最新几天按新切分，同一条序列前后口径不一致，
而这种不一致**不会报错**，只会让 2024 年的 BW 占比和 2026 年的不可比。

2026-08-28 已经踩过一次：名单 36→37，跑 concentration_updater 只输出
"no new dates"，643 天历史原封不动。

━━ 锁的机制 ━━
名单的 sha256 是它的身份。三处必须一致：
    ① bw_classification.tech_industries.lock.sha256   声明值
    ② tearing_daily.tech_sha                          撕裂序列建在哪一版上
    ③ volume/margin_concentration.meta.tech_sha       集中度序列建在哪一版上
任一处不符 → verify_crash_panorama FAIL，并给出重建命令。

改名单的正确流程（缺一步即 FAIL）：
    1. 改 bw_classification.tech_industries.list
    2. python3 stock_cache/tech_lock.py --seal      重新声明 lock（记录变动）
    3. python3 stock_cache/concentration_updater.py --rebuild
    4. python3 stock_cache/tearing_builder.py
    5. 重跑 views/ 下的生成器 + verify_crash_panorama.py

用法：
    from tech_lock import tech_sha, tech_list
    python3 stock_cache/tech_lock.py            查看当前状态
    python3 stock_cache/tech_lock.py --seal     把当前名单封为新的声明值
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
BW_JSON = SCRIPT_DIR / "indexes" / "bw_classification.json"
BJT = ZoneInfo("Asia/Shanghai")


def tech_list(path=None):
    """当前名单，排序后返回 —— 顺序不参与身份认定。"""
    d = json.loads(Path(path or BW_JSON).read_text())
    return sorted(d["tech_industries"]["list"])


def tech_sha(path=None):
    """名单的 sha256（前 16 位）。换行分隔的排序名单，UTF-8。"""
    blob = "\n".join(tech_list(path)).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def declared(path=None):
    """bw_classification 里声明的 lock，无则 None。"""
    d = json.loads(Path(path or BW_JSON).read_text())
    return (d["tech_industries"].get("lock") or {}) or None


def check(path=None):
    """(是否一致, 当前sha, 声明sha)。声明缺失时视为不一致。"""
    cur = tech_sha(path)
    lk = declared(path) or {}
    return (lk.get("sha256") == cur), cur, lk.get("sha256")


def seal(note=""):
    """把当前名单封为新的声明值，并把上一版记进 history。"""
    d = json.loads(BW_JSON.read_text())
    ti = d["tech_industries"]
    old = ti.get("lock") or {}
    hist = old.pop("history", []) if old else []
    if old.get("sha256"):
        hist.append({k: old[k] for k in ("version", "sha256", "sealed_at", "n", "note")
                     if k in old})
    ti["lock"] = {
        "version": (old.get("version", 0) or 0) + 1,
        "sha256": tech_sha(),
        "n": len(ti["list"]),
        "sealed_at": datetime.now(BJT).strftime("%Y-%m-%d %H:%M"),
        "note": note,
        "contract": "改名单后必须依次跑：concentration_updater.py --rebuild → "
                    "tearing_builder.py → views 生成器 → verify_crash_panorama.py。"
                    "跳过 --rebuild 会让集中度序列前后口径不一致且不报错。",
        "history": hist,
    }
    BW_JSON.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    return ti["lock"]


if __name__ == "__main__":
    import sys

    if "--seal" in sys.argv:
        i = sys.argv.index("--seal")
        note = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        lk = seal(note)
        print(f"已封版 v{lk['version']}  sha={lk['sha256']}  {lk['n']} 个行业")
        if lk["history"]:
            print(f"  上一版 v{lk['history'][-1]['version']} "
                  f"sha={lk['history'][-1]['sha256']} ({lk['history'][-1]['n']} 个)")
        print("  接下来必须跑: concentration_updater.py --rebuild → tearing_builder.py")
    else:
        okk, cur, dec = check()
        lk = declared() or {}
        print(f"当前名单 {len(tech_list())} 个行业   sha={cur}")
        print(f"声明 lock v{lk.get('version', '—')}  sha={dec}  "
              f"封于 {lk.get('sealed_at', '—')}")
        print("一致性:", "✓ 通过" if okk else "✗ 名单已漂移 —— 跑 --seal 并重建全部序列")
