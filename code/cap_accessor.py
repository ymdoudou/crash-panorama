#!/usr/bin/env python3
"""
cap_accessor.py — 行业 / 子行业 Cap 的唯一访问器（PE + PS，因果口径）

━━ 为什么存在 ━━
2026-08-23 之前，cap 取值散落五处、四种口径，`_applicable_quarter` 被实现三遍：

  t_structural_builder._load_ind_params        行业   history[-1]   含 anchor
  mkr_builder._load_ind_caps                   行业   history[-1]   含 anchor
  mkr_builder._load_hist_sub_caps              子行业 history[-1]   含 anchor
  t_daily_builder._build_historical_cap_lookup 行业   按季度        含 anchor
  semi_valuation_monthly                       子行业 按季度        含 anchor

两类问题：取 history[-1] 是前视；anchor_scale 让全历史随新季度滚入被整列改写。

━━ 2026-08-26 T_v5 改造 ━━
数据源由 `historical_cap_pe.json` 换为 `historical_cap.json`，三处实质变化：

  ① **PE 与 PS 两侧对称。** 两条口径各有自己的逐季 cap 与 R 序列，
     不再由一侧借用另一侧的当日快照 —— 借用会把「今天的横截面」当成历史值，
     |d| 因此可以离谱到 80 那种量级。

  ② **base 逐季度。** 旧文件的 base_pe 是单期冻结值（2026-08-19），2020 年的 cap
     用的是 2026 年横截面推出的 base。

  ③ **`r_floor` 参数整个删除。** 它此前对 99~100% 的行业都在生效，把 cap 压成
     `base_i × 常数`，逐季度的 R_i(Q) 从未起过作用；且它由 H 反解而来，构成
     cap→H→cap 的闭环。T_v5 把这个归一化移到了 H 上（60 阶因果高通滤波），
     cap 得以回归「价值天花板」本义，并与 MKR 共用同一个值。
     详见 views/t_structural_design.html 第 8 节。

━━ 因果契约 ━━
只使用「regime 生效日 ≤ 查询日」的季度：

    cap(name, date)          = base(Q) × R(Q)
    cap_ps(name, date)       = base_ps(Q) × R_ps(Q)
    Q = 生效日 ≤ date 的最晚季度；不存在则返回 None（该行业当日不参与计算）

regime 生效日语义是财报披露截止日的次日（Q1→05-01、Q2→09-01、Q3→11-01、
Q4→次年05-01），本身是因果的。base 与 R 都在该季度及更早的数据上演算。

验收标准：假装今天是 T₀ 重算历史序列，再假装今天是 T₀+90 天（跨一个季度滚入）
重算同一段历史，两次结果应逐日完全相同。

━━ 用法 ━━
    from cap_accessor import CapAccessor
    ca = CapAccessor()
    ca.cap("半导体", "2026-05-15")            # PE cap
    ca.cap_ps("半导体", "2026-05-15")         # PS cap
    ca.caps("2026-05-15")                     # {行业: PE cap} 全量
    ca.caps_ps("2026-05-15")                  # {行业: PS cap} 全量
    ca.sub_cap("芯片", "2026-05-15")          # 子行业 PE cap
    ca.sub_caps_ps("2026-05-15")              # {子行业: PS cap} 全量
    ca.applicable_quarter("2026-08-23")       # '2026Q1'
    ca.cap_shift("2026-08-23")                # 本季 cap 相对上季的中位抬升

命令行自检：
    python3 stock_cache/cap_accessor.py
"""

import json
from functools import lru_cache
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CAP_PATH = SCRIPT_DIR / "historical_cap.json"

# 四张表：(键名, 数据段)
_BLOCKS = {("pe", False): "pe", ("ps", False): "ps",
           ("pe", True): "sub_pe", ("ps", True): "sub_ps"}


class CapAccessor:
    """historical_cap.json 的因果读取层。PE/PS 对称，无 anchor，无 r_floor。"""

    def __init__(self, path=None):
        self.path = Path(path) if path else CAP_PATH
        if not self.path.exists():
            raise SystemExit(
                f"缺 {self.path.name} —— 先跑 python3 stock_cache/cap_history_builder.py")
        raw = json.loads(self.path.read_text())
        self.generated_at = raw.get("generated_at")

        # regime: [(生效日, 季度)]，按生效日升序
        self._regime = sorted(
            ((eff, q) for q, eff in raw.get("regime_dates", {}).items()),
            key=lambda x: x[0],
        )

        # (kind, sub) -> {name: {quarter: cap}}
        self._tab = {}
        for key, section in _BLOCKS.items():
            out = {}
            for name, byq in (raw.get(section) or {}).items():
                cells = {q: c["cap"] for q, c in byq.items()
                         if c.get("cap") and c["cap"] > 0}
                if cells:
                    out[name] = cells
            self._tab[key] = out

    # ── 季度归属 ──────────────────────────────────────────────

    @lru_cache(maxsize=4096)
    def applicable_quarter(self, date):
        """生效日 ≤ date 的最晚季度；date 早于所有生效日则返回 None。"""
        q = None
        for eff, quarter in self._regime:
            if eff <= date:
                q = quarter
            else:
                break
        return q

    # ── 核心取值 ──────────────────────────────────────────────

    def _lookup(self, name, date, kind, sub):
        cells = self._tab[(kind, sub)].get(name)
        if not cells:
            return None
        q = self.applicable_quarter(date)
        if q is None:
            return None
        # 该行业自己的历史里，不晚于 q 的最晚季度（行业可能缺某些季度）
        avail = [x for x in cells if x <= q]
        return cells[max(avail)] if avail else None

    def cap(self, name, date, sub=False):
        """PE cap = base(Q) × R(Q)。无已生效季度则 None。"""
        return self._lookup(name, date, "pe", sub)

    def cap_ps(self, name, date, sub=False):
        """PS cap = base_ps(Q) × R_ps(Q)。无已生效季度则 None。"""
        return self._lookup(name, date, "ps", sub)

    def caps(self, date, sub=False):
        """{name: PE cap} —— 当日所有有已生效季度的行业。

        cap 只在季度边界变化，故按（适用季度, sub）缓存 —— 逐日调用是常态。
        """
        return self._caps_by_quarter(self.applicable_quarter(date), "pe", sub)

    def caps_ps(self, date, sub=False):
        return self._caps_by_quarter(self.applicable_quarter(date), "ps", sub)

    @lru_cache(maxsize=512)
    def _caps_by_quarter(self, quarter, kind, sub):
        if quarter is None:
            return {}
        out = {}
        for name, cells in self._tab[(kind, sub)].items():
            avail = [x for x in cells if x <= quarter]
            if avail:
                out[name] = cells[max(avail)]
        return out

    # ── 估值基准迁移 ──────────────────────────────────────────

    @lru_cache(maxsize=64)
    def _quarters(self, kind="pe", sub=False):
        return sorted({q for cells in self._tab[(kind, sub)].values() for q in cells})

    @lru_cache(maxsize=4096)
    def cap_shift(self, date, kind="pe", sub=False):
        """本季 cap 相对上一季的中位抬升幅度。季度内为常数。

        ━━ 为什么是一等公民 ━━
        财报生效日 cap 阶跃上移，d_i = (m−cap)/cap 随之骤降 —— 这不是市场变冷，
        是估值基准换季重定价。T_v5 之前这个位移被 H_base 的口径错误吸收成一段
        为期两月的假降温；2026-08-26 修正口径后它从 T 里消失了。

        但它并非噪声：实测 cap_shift 单独对 SEVERE 崩溃的 AUC 达 0.6967，
        高于修正后的 T（0.6281），且与 T 几乎正交（corr +0.12）。
        经济含义清楚 —— 盈利兑现、估值基准上移，市场随后回调。

        故拆成独立信号，与 T 并列进入 Layer1，而不是藏在 H_base 的错误里。
        加 60 日衰减反而更差（0.6967 → 0.5981）：起作用的是「整个季度处在更高
        基准上」这个持续状态，不是「刚发生跃升」这个瞬时事件，故季度内保持常数。

        返回 None 表示当日无已生效季度或无上一季可比。
        """
        q = self.applicable_quarter(date)
        if q is None:
            return None
        qs = self._quarters(kind, sub)
        if q not in qs:
            avail = [x for x in qs if x <= q]
            if not avail:
                return None
            q = max(avail)
        i = qs.index(q)
        if i == 0:
            return None
        prev = qs[i - 1]
        table = self._tab[(kind, sub)]
        rs = []
        for cells in table.values():
            a, b = cells.get(prev), cells.get(q)
            if a and b and a > 0:
                rs.append(b / a - 1)
        if not rs:
            return None
        rs.sort()
        return rs[len(rs) // 2]

    # ── 子行业便捷入口 ────────────────────────────────────────

    def sub_cap(self, sub_name, date):
        return self.cap(sub_name, date, sub=True)

    def sub_cap_ps(self, sub_name, date):
        return self.cap_ps(sub_name, date, sub=True)

    def sub_caps(self, date):
        return self.caps(date, sub=True)

    def sub_caps_ps(self, date):
        return self.caps_ps(date, sub=True)

    # ── 自检 ──────────────────────────────────────────────────

    def names(self, kind="pe", sub=False):
        return sorted(self._tab[(kind, sub)])


def _selftest():
    import datetime

    ca = CapAccessor()
    print(f"{ca.path.name} generated_at = {ca.generated_at}")
    print(f"行业   PE {len(ca.names('pe'))} / PS {len(ca.names('ps'))}")
    print(f"子行业 PE {len(ca.names('pe', True))} / PS {len(ca.names('ps', True))}")

    today = datetime.date.today().isoformat()
    print(f"\n今天 {today} 适用季度: {ca.applicable_quarter(today)}")

    print("\n半导体 cap 随日期（因果，无 anchor / 无 r_floor）:")
    print(f"   {'日期':<13}{'季度':<9}{'cap_pe':>9}{'cap_ps':>9}{'cap_shift':>11}")
    for d in ["2021-06-30", "2024-10-15", "2025-06-30", "2026-05-15", "2026-08-23"]:
        cs = ca.cap_shift(d)
        print(f"   {d:<13}{str(ca.applicable_quarter(d)):<9}"
              f"{ca.cap('半导体', d) or 0:>9.1f}{ca.cap_ps('半导体', d) or 0:>9.2f}"
              f"{(f'{cs:+.4f}' if cs is not None else '—'):>11}")

    # 因果性断言：查询日在某季度生效日之前，不得取到该季度
    bad = []
    for eff, q in ca._regime:
        prev = (datetime.date.fromisoformat(eff) - datetime.timedelta(days=1)).isoformat()
        if ca.applicable_quarter(prev) == q:
            bad.append((prev, q))
    print(f"\n因果性断言（生效日前一天不得取到该季度）: "
          f"{'✓ 通过' if not bad else f'✗ {bad}'}")

    # 稳定性断言：跨季度滚入不得改写历史值
    d = "2024-10-15"
    snap = {n: ca.cap(n, d) for n in ca.names("pe")}
    ca2 = CapAccessor()
    drift = [n for n in snap if snap[n] != ca2.cap(n, d)]
    print(f"重建访问器后 {d} 的 cap 一致性: {'✓ 通过' if not drift else f'✗ {drift[:5]}'}")

    d = "2026-08-23"
    print(f"\n{d} 全量: PE {len(ca.caps(d))} 行业 / {len(ca.sub_caps(d))} 子行业   "
          f"PS {len(ca.caps_ps(d))} 行业 / {len(ca.sub_caps_ps(d))} 子行业")


if __name__ == "__main__":
    _selftest()
