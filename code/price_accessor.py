#!/usr/bin/env python3
"""
price_accessor.py — OHLCV 价格序列的唯一访问器（三种口径，显式选择）

━━ 为什么存在 ━━
`ohlcv_cache` 此前有两个语义相反的入口，且没有任何机制规定该用哪个：

    ohlcv_cache.load_ohlcv(code)   → 完整前复权（_forward_adjust 在读取时调整）  37 个脚本在用
    直读 ohlcv_cache/*.csv          → 未复权                                    8 个脚本在用

2026-08-24 实测发现 `crash_gate_builder.load_ohlcv_r60` 走了直读路径拿到未复权价，
却用它算 `r60 = close / max(high, 60日)` —— 一个比值型收益指标。送转让分子腰斩、
分母仍是复权前的高价，产生虚假回撤：

    002594 比亚迪 2025-07-29 十送二十(因子3.0) → 2025-09-17 的 r60 显示 0.320，实际 0.952
    P 集合 525 只中 100 只(19.0%) 在窗口内有送转，其 r60 全部受失真

这直接污染了 price_stress → SI → Gate 的 risk_band。

━━ 三种口径，按用途选，不要凭直觉 ━━

    raw(code)              未复权。**市值与 PE 用这个。**
                           mcap(t) = shares(t) × close_raw(t)。现金分红在除息日确实
                           让市值下降，那是 t 时刻的真实经济事件，不该被抹掉。

    split_adjusted(code)   除权不除息：close / F(t→now)，F 只含送转与配股。
                           **回撤、收益率、r60、动量类比值用这个。**
                           送转不改变持有人财富（股数↑价格↓成比例），必须消除；
                           现金分红不调整 —— 见下方因果性说明。

    qfq(code)              完整前复权（扣分红 + 除送转），等价于 ohlcv_cache.load_ohlcv。
                           **仅为兼容既有调用方保留。历史值不稳定，勿用于需要可复现
                           历史序列的场景。**

━━ 为什么 split_adjusted 而不是 qfq ━━
比值型指标（dd、r60）满足 `X(t) / max(X(s))`。

    送转调整是除法：新增一次送转，窗口内每一天都被同一因子 F 除 → 在比值中对消
                    ⇒ 历史值恒定 ✓
    扣现金分红是减法：窗口内每一天都减去同一个 D → 在比值中**不**对消
                    ⇒ 历史值随每次新分红单调漂移，永不收敛 ✗

实证（688498，2026-05-18 送转）：在除权前后两个时点重算同一段历史，
split_adjusted 口径 709 天不一致 **0 天**；qfq 口径不一致 **650 天**。

现金分红被有意忽略：对回撤度量的影响是 A 股股息率量级(1~3%)，远小于送转(可达 50%+)，
用精度换可复现性。此取舍已记入 paper 方法论。

━━ 用法 ━━
    from price_accessor import PriceAccessor
    pa = PriceAccessor()
    rows = pa.split_adjusted("002594")        # [{date, open, high, low, close, volume, amount}]
    f    = pa.split_factor("002594", "2025-07-01")   # 该日之后的送转累乘因子

自检: python3 stock_cache/price_accessor.py
"""

import csv
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from share_factor import ShareFactor

OHLCV_DIR = SCRIPT_DIR.parent / "ohlcv_cache"
PRICE_COLS = ("open", "high", "low", "close")


class PriceAccessor:
    """ohlcv_cache 的三口径读取层。CSV 磁盘内容是未复权，调整在读取时完成。"""

    def __init__(self, ohlcv_dir=None, share_factor=None):
        self.dir = Path(ohlcv_dir) if ohlcv_dir else OHLCV_DIR
        self.sf = share_factor or ShareFactor()
        self._raw_cache = {}

    # ── 底层 ──────────────────────────────────────────────

    def raw(self, code, start=None):
        """未复权。市值/PE 用。"""
        key = (code, start)
        if key in self._raw_cache:
            return self._raw_cache[key]
        fp = self.dir / f"{code}.csv"
        rows = []
        if fp.exists():
            with open(fp) as f:
                rd = csv.DictReader(f)
                if rd.fieldnames and "date" in rd.fieldnames:
                    for r in rd:
                        d = r.get("date", "")
                        if not d or (start and d < start):
                            continue
                        try:
                            row = {"date": d}
                            for c in PRICE_COLS:
                                row[c] = float(r.get(c) or 0)
                            row["volume"] = float(r.get("volume") or 0)
                            row["amount"] = float(r.get("amount") or 0)
                        except (TypeError, ValueError):
                            continue
                        if row["close"] > 0:
                            rows.append(row)
        rows.sort(key=lambda x: x["date"])
        if len(self._raw_cache) < 256:
            self._raw_cache[key] = rows
        return rows

    def split_factor(self, code, date):
        """F(date→now)：date 之后所有送转/配股的累乘因子。"""
        return self.sf.factor(code, date)

    # ── 口径 ──────────────────────────────────────────────

    def split_adjusted(self, code, start=None):
        """除权不除息。回撤 / 收益率 / r60 / 动量类比值用。"""
        evs = self.sf.event_dates(code)
        rows = self.raw(code, start)
        if not evs:
            return [dict(r) for r in rows]
        out = []
        for r in rows:
            f = 1.0
            for ed, mult in evs:
                if ed > r["date"]:
                    f *= mult
            nr = dict(r)
            if f != 1.0:
                for c in PRICE_COLS:
                    nr[c] = r[c] / f
            out.append(nr)
        return out

    def qfq(self, code, start=None):
        """完整前复权（扣分红 + 除送转）。仅为兼容既有调用方；历史值不稳定。"""
        evs = self._xdxr_full(code)
        rows = [dict(r) for r in self.raw(code, start)]
        for ev in sorted(evs, key=lambda x: x["date"]):
            fh, pg, pgj, szg = ev["fenhong"], ev["peigu"], ev["peigujia"], ev["songzhuangu"]
            divisor = 10 + pg + szg
            if divisor <= 0:
                continue
            for r in rows:
                if r["date"] < ev["date"]:
                    for c in PRICE_COLS:
                        r[c] = (r[c] * 10 - fh + pg * pgj) / divisor
        return rows

    def _xdxr_full(self, code):
        import json
        fp = self.sf.xdxr_dir / f"{code}.json"
        if not fp.exists():
            return []
        try:
            recs = json.loads(fp.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        out = []
        for r in recs:
            if not isinstance(r, dict) or not r.get("date"):
                continue
            out.append({"date": r["date"][:10], "fenhong": r.get("fenhong") or 0,
                        "peigu": r.get("peigu") or 0, "peigujia": r.get("peigujia") or 0,
                        "songzhuangu": r.get("songzhuangu") or 0})
        return out


def _selftest():
    pa = PriceAccessor()
    code = "002594"          # 比亚迪：2025-07-29 十送二十，因子 3.0
    raw = pa.raw(code)
    adj = pa.split_adjusted(code)
    print(f"{code} 比亚迪   {len(raw)} 行 {raw[0]['date']} ~ {raw[-1]['date']}")
    print(f"  送转事件: {[(d, round(m, 2)) for d, m in pa.sf.event_dates(code) if d >= '2023-01-01']}")

    def r60(rows):
        o = {}
        for j in range(59, len(rows)):
            hi = max(x["high"] for x in rows[j - 59:j + 1])
            if hi > 0:
                o[rows[j]["date"]] = rows[j]["close"] / hi
        return o

    a, b = r60(raw), r60(adj)
    worst = max((d for d in a if d in b), key=lambda d: abs(a[d] - b[d]))
    print(f"\n  r60 失真最严重日 {worst}:  未复权 {a[worst]:.3f}   除权不除息 {b[worst]:.3f}   "
          f"偏差 {a[worst] - b[worst]:+.3f}")
    n = sum(1 for d in a if d in b and abs(a[d] - b[d]) > 0.02)
    print(f"  |偏差|>0.02 的天数: {n}/{len(a)}")

    # 因果稳定性：除权前后两个时点算同一段历史，split_adjusted 应恒定
    ev = next(d for d, _ in pa.sf.event_dates(code) if d >= "2025-01-01")
    print(f"\n  因果稳定性检验（除权日 {ev}）:")
    for label, cutoff in [("除权前", "2025-07-01"), ("除权后", "2026-08-21")]:
        known = [(d, m) for d, m in pa.sf.event_dates(code) if d <= cutoff]
        sub = [r for r in raw if r["date"] <= cutoff]
        adj2 = []
        for r in sub:
            f = 1.0
            for ed, m in known:
                if ed > r["date"]:
                    f *= m
            adj2.append({**r, "high": r["high"] / f, "close": r["close"] / f})
        s = r60(adj2)
        d = "2025-03-31"
        if d in s:
            print(f"    {label}({cutoff}) 算得 r60({d}) = {s[d]:.6f}")

    print(f"\n  三口径同日取值 {raw[-200]['date']}: "
          f"raw={raw[-200]['close']:.2f}  "
          f"split_adj={adj[-200]['close']:.2f}  "
          f"qfq={pa.qfq(code)[-200]['close']:.2f}")


if __name__ == "__main__":
    _selftest()
