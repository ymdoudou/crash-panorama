#!/usr/bin/env python3
"""
share_factor.py — 历史股本还原因子（因果口径）

━━ 为什么存在 ━━
计算历史 PE 需要历史市值 mcap(t) = shares(t) × close_raw(t)。
系统里没有历史股本时序（total_shares.json 只是当前快照），但
ohlcv_cache/xdxr/{code}.json 有全市场 5219 只的除权除息事件，
可以从当前股本反推任意历史时点的股本：

    F(t→now) = Π (1 + songzhuangu/10 + peigu/10)     对 t 之后的所有除权事件
    shares(t) = shares_now / F(t→now)

━━ 为什么不能用前复权价 ━━
t_daily_builder 原先走 history/price（前复权）并用比值反推市值：

    mcap_hist = mcap_now × close_qfq(t) / close_qfq(now)
              = shares_now × [close_raw(t) − D(t,now)] / F(t,now)
    真值        = shares(t) × close_raw(t) = [shares_now / F(t,now)] × close_raw(t)

送转因子 F 精确对消（前复权对送转是对的），但现金分红项 D 不对消 ——
历史市值被系统性低估 D/close_raw(t)，且 D 随每次新分红单调累加，
于是同一个历史日期的 PE 持续向下漂移、永不收敛。

改用未复权价（ohlcv_cache）+ 本模块还原的 shares(t)，两个误差同时消除：
分红不再污染历史（现金分红在除息日确实让市值下降，那是 t 时刻的真实经济事件），
送转由 F 显式处理（送转不改变市值，shares↑ 与 price↓ 成比例）。

━━ 因果性 ━━
F(t→now) 只累乘 **除权日 > t** 的事件。查询任意历史 t 得到的 shares(t)
不随 t 之后新增事件而改变其历史含义 —— 新事件只会让 F 变大、shares(t) 相应
保持不变（因为 shares_now 同步变大）。

自检: python3 stock_cache/share_factor.py
"""

import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
XDXR_DIR = SCRIPT_DIR.parent / "ohlcv_cache" / "xdxr"
TOTAL_SHARES = SCRIPT_DIR / "total_shares.json"


class ShareFactor:
    """从 xdxr 事件还原历史股本因子。"""

    def __init__(self, xdxr_dir=None, shares_path=None):
        self.xdxr_dir = Path(xdxr_dir) if xdxr_dir else XDXR_DIR
        p = Path(shares_path) if shares_path else TOTAL_SHARES
        self.shares_now = json.loads(p.read_text()) if p.exists() else {}
        self._events = {}          # code -> [(date, mult)]  升序，mult>1 才收录

    def _load(self, code):
        if code in self._events:
            return self._events[code]
        path = self.xdxr_dir / f"{code}.json"
        evs = []
        if path.exists():
            try:
                recs = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                recs = []
            if isinstance(recs, dict):
                recs = recs.get("records") or recs.get("data") or []
            for r in recs:
                if not isinstance(r, dict):
                    continue
                d = r.get("date")
                if not d:
                    continue
                # 送转 + 配股都会增加股本；单位是「每 10 股」
                mult = 1.0 + (r.get("songzhuangu") or 0) / 10.0 \
                           + (r.get("peigu") or 0) / 10.0
                if mult > 1.0:
                    evs.append((d[:10], mult))
        evs.sort()
        self._events[code] = evs
        return evs

    def factor(self, code, date):
        """F(date→now)：date 之后所有除权事件的股本膨胀累乘。无事件则 1.0。"""
        f = 1.0
        for d, mult in self._load(code):
            if d > date:
                f *= mult
        return f

    def shares(self, code, date):
        """shares(date) = shares_now / F(date→now)。缺当前股本则 None。"""
        now = self.shares_now.get(code)
        if not now or now <= 0:
            return None
        return now / self.factor(code, date)

    def has_events(self, code):
        return bool(self._load(code))

    def event_dates(self, code):
        return list(self._load(code))


def _selftest():
    import csv
    sf = ShareFactor()
    print(f"total_shares.json: {len(sf.shares_now)} 只")
    print(f"xdxr 目录: {len(list(sf.xdxr_dir.glob('*.json')))} 只\n")

    # 用财报股本交叉验证：balance.json 的 SHARE_CAPITAL 是该报告期的真实股本
    checks = [
        ("688498", "2026-03-31", 85947726.0),    # 2026-05-18 送转 4.5
        ("001309", "2026-03-31", 226845658.0),   # 2025-07-10 送转 4.0
        ("600519", "2026-03-31", 1252270215.0),  # 只有现金分红，无送转
    ]
    print(f"{'代码':<9}{'报告期':<13}{'财报股本':>16}{'还原股本':>16}{'F':>8}{'偏差':>9}")
    for code, rd, actual in checks:
        est = sf.shares(code, rd)
        f = sf.factor(code, rd)
        if est is None:
            print(f"{code:<9}{rd:<13}{actual:>16,.0f}{'—':>16}")
            continue
        dev = (est - actual) / actual * 100
        print(f"{code:<9}{rd:<13}{actual:>16,.0f}{est:>16,.0f}{f:>8.4f}{dev:>+8.2f}%")

    print("\n除权事件样例:")
    for code in ["688498", "001309", "600519"]:
        evs = [e for e in sf.event_dates(code) if e[0] >= "2023-01-01"]
        print(f"   {code}: {evs if evs else '2023 年后无送转/配股'}")

    # 因果性：F 只累乘 date 之后的事件
    code = "688498"
    d_before, d_after = "2026-05-17", "2026-05-19"
    print(f"\n因果性检查 {code}（除权日 2026-05-18）:")
    print(f"   F({d_before}) = {sf.factor(code, d_before):.4f}  ← 含该次送转")
    print(f"   F({d_after}) = {sf.factor(code, d_after):.4f}  ← 不含")

    # 覆盖率
    import random
    codes = [p.stem for p in sf.xdxr_dir.glob("*.json")]
    smp = random.Random(7).sample(codes, min(500, len(codes)))
    have_sh = sum(1 for c in smp if sf.shares_now.get(c))
    with_ev = sum(1 for c in smp if sf.factor(c, "2023-07-04") > 1.0)
    print(f"\n抽样 {len(smp)} 只: 有当前股本 {have_sh} ({have_sh/len(smp)*100:.0f}%), "
          f"2023-07-04 以来有送转/配股 {with_ev} ({with_ev/len(smp)*100:.0f}%)")


if __name__ == "__main__":
    _selftest()
