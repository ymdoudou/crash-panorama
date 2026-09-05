#!/usr/bin/env python3
"""
crash_gate_builder.py — Crash Gate + SI 日頻生產腳本

Layer 1 Gate: T < 1.0 AND sd% < 25 → ALARM
Layer 2 SI:  (price_stress + margin_stress + temp_stress) / 3

輸出: stock_cache/crash_gate_daily.json (追加式)

模式:
  python3 crash_gate_builder.py              # 增量: 補齊缺失日期
  python3 crash_gate_builder.py --refresh-t  # T 口径变更后原地刷新（推荐）
  python3 crash_gate_builder.py --backfill   # 补缺口（有护栏，见下）

管道位置: m_daily_pipeline Phase 2.7a (在 t_structural_builder 之後)
"""

import argparse
import bisect
import csv
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
# 阈值不写在代码里 —— 参数与实现分离，见 gate_params.py 的说明。
from gate_params import P

SCRIPT_DIR = Path(__file__).resolve().parent
BASE = SCRIPT_DIR.parent
OHLCV_DIR = BASE / "ohlcv_cache"
SC = SCRIPT_DIR

OUTPUT = SC / "crash_gate_daily.json"
T_CAUSAL = SC / "t_causal.json"

# ── Layer1 Gate 阈值（2026-08-26 重标）────────────────────────────────
# 旧式 `gate = ALARM if T < 1.0 and sd_pct < 25`。T_v5 换 cap 后 T 分布整体
# 下移（中位 1.04 → 0.973），旧阈值报警率 44.5%，近一半交易日在报警。
#
# 对 14 个 crash episode（SEVERE 6 个，由 crash_episode_builder 独立识别、零 T
# 依赖）标定，窗口 2023-07-04 ~ 2026-08-21：
#
#   T< 逐阈值        报警率   SEVERE精确   SEVERE召回
#     0.59           6.7%      72.5%       100%
#     0.69          13.5%      66.0%       100%     ← 采用
#     0.85          32.2%      48.6%       100%
#     1.00          62.3%      31.4%       100%     ← 旧值
#   SEVERE 召回在 T<0.85 以下全部 100% —— 收紧只提精确率，不丢事件。
#   取 0.69 而非 0.59：Layer1 是漏斗入口，宁松勿漏，精细判别交给 Layer2 的 SI。
#
# ── sd% 为何退出判定 ──
# 单变量 AUC：低=风险 0.5207 / 高=风险 0.4793 —— 两个方向都贴着随机线。
# 55 格二维扫描（T 六档 × sd% 十档、含两个方向），每格与**同等报警量下单纯收紧 T**
# 对照：93% 的格子是负增益，中位 −7.4pp。仅有的 4 格正增益方向自相矛盾
# （最好的一格是 sd%>15，与旧设计的 sd%<25 反向）、且样本极小（n=49，换 3 天即翻盘）。
# 结论不是「阈值没调对」，是这一维不携带 T 之外的信息。
#
# sd_pct / n_sig 仍逐日测量并落盘 —— 它们是两融 epoch 信号的原始统计，
# 有诊断价值，且 crash_panorama 的 Layer1 页面在展示。只是不再进判定式。
# ── 2026-08-26 第二次重标：Layer1 由「T 单变量」改为「T + cap_shift 秩合成」──
# 起因：修正 H_base 的口径错误（基线须与当日同 cap）后，T 的 SEVERE AUC 从
# 0.7899 掉到 0.6281。追查发现旧口径的判别力有一部分来自「财报季 cap 跃升」——
# 那个位移被 H_base 的错误吸收成两月假降温，同时也编码了真实信息：
# 盈利兑现 → 估值基准上移 → 市场随后回调。
#
# 把它显式拆出来（cap_accessor.cap_shift），与 T 并列：
#     cap_shift 单独 SEVERE AUC 0.6967（高于修正后的 T 0.6281）
#     corr(T, cap_shift) = +0.12                     —— 几乎正交
#     加权合成 SEVERE AUC 0.8008                      —— 超过被污染的旧基准 0.7899
#
# 合成用**扩展窗口百分位秩**（只用 t 之前的历史），而非全样本秩：
#     pT(t) = 1 − rank( T(t)         | T(<t) )        低 T = 高风险
#     pS(t) = rank( cap_shift(t)     | cap_shift(<t) ) 高 shift = 高风险
#     risk(t) = W_T·pT + (1−W_T)·pS
# 因果成立，且实测优于全样本秩（0.8008 vs 0.7881）—— 「今天的 T 在至今为止的
# 历史里有多极端」本就不该由未来数据判定。
#
# 权重 0.45 经 LOEO 验证：6 折中 5 折选 0.50、1 折选 0.30，离散度 0.20（稳定）；
# 样本外均值 0.7866，5/6 折优于纯 T。曾试「取大」以防无效维稀释有效维，
# 最坏折确实更好（0.6145 vs 0.4109）但均值更低（0.7341），故仍用加权。
#
# 已知盲区：cap_shift 在「基准平稳期的崩溃」上无信息（如 2024-02 那次，
# 距生效日已两月），此时会稀释 T。这是可解释的失败模式，写入文档而非掩盖。
W_T = P("w_T")             # T 的权重，其余给 cap_shift
RANK_WARMUP = P("rank_warmup")   # 秩的暖机天数；不足则该日不出 risk
# ── RISK_ALARM 标定（2026-08-26）──
# 逐档扫描（762 天可用 risk，SEVERE 6 事件）：
#     risk>0.65   报警率 26.8%   SEVERE 精确 44.1%   SEVERE 召回 100%   全召回 71%
#     risk>0.78   报警率  6.3%   SEVERE 精确 64.6%   SEVERE 召回  83%
#   对照 纯 T<0.85 报警率 30.2%  SEVERE 精确 30.9%   SEVERE 召回 100%   全召回 79%
#   同报警率下 risk 的精确率高 13~30pp。
#
# LOEO（硬约束 SEVERE 全召回 + 报警率 ≤30%，目标最大化精确率）：
#     训练最优 thr 取值 [0.65, 0.66, 0.67, 0.78]，离散度 0.13 —— 判定跳动
#     样本外命中:  训练最优 5/6   固定 0.65 **6/6**   纯 T<0.85 6/6
#   跳动来源明确：只有留出 #3（2024-02，cap_shift 盲区）那一折跳到 0.78，
#   其余四折都在 [0.65, 0.67] 窄带内。故取窄带下沿 0.65 —— 偏保守，
#   正好补偿盲区，且样本外命中优于训练最优。
RISK_ALARM = P("risk_alarm")
T_ALARM = 0.69             # risk 不可用时（暖机期）的回落阈值

# ── Layer2 SI 的第四个分量（2026-08-26 新增）──
# Gate 换成二维（T + cap_shift）后，Layer2 仍是三个 stress 的等权平均，里面只有
# T 这一维、没有 cap_shift —— 两层口径脱节。后果是分档失效：
#     三分量 SI   SEVERE [0.214, 0.586]   MODERATE [0.252, 0.292]   间隙 −0.077 ✗
# 一个 SEVERE（#11 2025-09-04，maxSI 0.214）低于一个 MODERATE（#4，0.292）。
# 那些日子由 cap_shift 触发报警，T 并不极端，故 temp_stress 很小、SI 被拉低。
#
# 补上第四维后两 tier 分开：
#     四分量 SI   SEVERE [0.404, 0.689]   MODERATE [0.189, 0.251]   间隙 +0.153 ✓
#
# 除数 s0 的稳健性扫描（0.03 ~ 1.00 共 10 档）：9 档间隙为正，峰在 0.15，
# 0.08~0.25 之间都在 +0.075 以上 —— 宽平台，不是窄区间巧合。
# s0 ≥ 0.20 时饱和消失但误报翻倍（14 → 26 → 32 天），说明那四个 SEVERE 的
# cap_shift 确实极端，压到饱和是有区分力的。0.15 是「保饱和」与「少误报」的交点。
#
# 已知限制：SEVERE 下沿 0.404 由 #11 撑着，而它靠 shift_stress 饱和才被托起来 ——
# 整个间隙依赖 cap_shift 在该事件上饱和这一件事。且 83 个非事件 ALARM 日里仍有
# 14 天（17%）超过 SEVERE 下沿，这是 tier 分离与误报率的固有张力，s0 调不掉。
SHIFT_S0 = P("shift_s0")

# ── Layer2 risk_band 分档（2026-08-26 重标）──
# 旧值 elevated>0.30 / critical>0.50 是三分量 SI 的口径。四分量下 SEVERE 的
# maxSI 落在 [0.404, 0.689]、MODERATE/MILD 落在 [0.189, 0.251]，间隙很宽。
#
#   critical>   SEVERE判对   MOD/MILD误判   非事件误报   误报率
#     0.33         6/6           0            32       38.6%
#     0.38         6/6           0            21       25.3%
#     0.40         6/6           0            14       16.9%   ← 采用
#     0.45         4/6           0             6        7.2%
#     0.50         2/6           0             3        3.6%   ← 旧值
# 0.40 是「SEVERE 全对」的最严阈值；再严立刻掉两个，再松只增误报不增收益。
# 取间隙中点（0.33）是错的 —— 中点在 SEVERE 侧无收益，误报却翻倍。
# 约束边界是 SEVERE 下沿（#3 的 0.404），故贴着它取。
#
# LOEO 14 折：critical 取值 [0.40] 离散度 0.000（零分歧），
# elevated 取值 [0.15, 0.20] 离散度 0.05（仅 #13 一折选 0.20，且两值下均判对），
# 留出集 14/14 全对。elevated 取偏保守的 0.20。
BAND_ELEVATED = P("band_elevated")
BAND_CRITICAL = P("band_critical")
# 2026-08-23 之前 T 拼接自两个来源：--backfill 读 t_extended_production.json
# （For_processing 里的手动一次性产物，未复权价、含 anchor 的 cap），增量读
# t_structural.json（前复权价、250 日滚动）。实测旧文件 189 天重叠区里 187 天
# 用的是 t_extended，两套同日取值可差 0.09（2026-05-15：0.94 vs 0.852）。
# 现统一为 t_causal.json —— 全历史、单一因果口径。
P_SECTOR = SC / "indexes" / "p_sector.json"
BW_CLASSIFICATION = SC / "indexes" / "bw_classification.json"
MARGIN_HIST_DIR = SC / "history" / "margin"
MARGIN_CACHE_DIR = SC / "margin_cache"

def _load_gate_layers():
    """从权威索引加载 Bit/Support 板块分层。

    此前 BIT_CORE/SUPPORT 是本文件里的硬编码常量，只覆盖 50 个板块，而 P 集合有 62 个 ——
    13 个板块 53 只股票(10.1%)被 compute_price_stress 直接 continue 跳过，从未参与 Layer1 演算。
    2026-08-23 起统一由 bw_classification.json 持有，并由 verify_crash_panorama.py 强制校验 100% 覆盖。
    """
    data = json.loads(BW_CLASSIFICATION.read_text())
    gl = data["gate_layers"]
    return set(gl["bit_core"]["list"]), set(gl["support"]["list"])


BIT_CORE, SUPPORT = _load_gate_layers()

SS_SPIKE_THRESH = 2.0
MR_SPIKE_THRESH = 1.5


def clamp01(x):
    return max(0.0, min(1.0, x))


def load_t_data(backfill=False):
    """唯一 T 源 = t_causal.json（全历史因果口径）。backfill 参数保留仅为兼容调用。"""
    if not T_CAUSAL.exists():
        raise SystemExit(f"ERROR: 缺 {T_CAUSAL.name}，先跑 t_causal_builder.py")
    data = json.loads(T_CAUSAL.read_text())
    return {r["date"]: r["T"] for r in data.get("daily", [])}


def load_cap_shift():
    """{date: cap_shift}。由 t_causal_builder 落盘，定义见 cap_accessor.cap_shift。"""
    if not T_CAUSAL.exists():
        return {}
    data = json.loads(T_CAUSAL.read_text())
    return {r["date"]: r["cap_shift"] for r in data.get("daily", [])
            if r.get("cap_shift") is not None}


def expanding_rank(seq, warm=RANK_WARMUP, lower_is_risk=False):
    """扩展窗口百分位秩。seq 为 [(date, value)] 升序，只用 t 之前的历史。

    暖机期（历史不足 warm 天）返回 None —— 宁可缺值也不用一个统计上无意义的秩。
    """
    out = {}
    hist = []
    for d, v in seq:
        if v is None:
            continue
        if len(hist) >= warm:
            r = bisect.bisect_left(hist, v) / len(hist)
            out[d] = round(1 - r if lower_is_risk else r, 4)
        bisect.insort(hist, v)
    return out


def compute_risk(rows):
    """给 rows 逐日补 pT / pS / risk 三列（原地修改）。"""
    cs = load_cap_shift()
    pT = expanding_rank([(r["date"], r["T"]) for r in rows], lower_is_risk=True)
    pS = expanding_rank([(r["date"], cs.get(r["date"])) for r in rows],
                        lower_is_risk=False)
    n_ok = 0
    for r in rows:
        d = r["date"]
        r["cap_shift"] = cs.get(d)
        a, b = pT.get(d), pS.get(d)
        r["pT"], r["pS"] = a, b
        if a is not None and b is not None:
            r["risk"] = round(W_T * a + (1 - W_T) * b, 4)
            n_ok += 1
        else:
            r["risk"] = None
    return n_ok


def load_p_classification():
    """Load P collection and classify into Bit/Support layers."""
    p_data = json.loads(P_SECTOR.read_text())
    stocks_info = p_data["stocks"]
    p_codes = sorted(c for c in stocks_info if not c.startswith("920"))

    code_layer = {}
    sector_codes = defaultdict(list)
    for code in p_codes:
        sec = stocks_info[code].get("sector", "")
        sector_codes[sec].append(code)
        if sec in BIT_CORE:
            code_layer[code] = "bit"
        elif sec in SUPPORT:
            code_layer[code] = "sup"

    return p_codes, code_layer, sector_codes


def load_margin_spikes(p_codes, target_dates=None):
    """Load spike fields from history/margin for P stocks → date→set(sig codes).

    When target_dates is a small set (≤10), only reads the tail of each file.
    """
    daily_sig = defaultdict(set)
    loaded = 0
    incremental = target_dates and len(target_dates) <= 10
    for code in p_codes:
        fp = MARGIN_HIST_DIR / f"{code}.json"
        if not fp.exists():
            continue
        data = json.loads(fp.read_text())
        rows = data.get("rows", [])
        if incremental:
            rows = rows[:30]
        for r in rows:
            d = r["date"]
            if incremental and d not in target_dates:
                continue
            ss = r.get("short_sell_spike_20d")
            mr = r.get("margin_repay_spike_20d")
            if ss is None and mr is None:
                continue
            if (ss or 0) > SS_SPIKE_THRESH or (mr or 0) > MR_SPIKE_THRESH:
                daily_sig[d].add(code)
        loaded += 1
    return daily_sig, loaded


def load_ohlcv_r60(codes, target_dates=None):
    """计算 ratio60 = close / max(high, 过去60日)。

    价格经 price_accessor.split_adjusted（除权不除息）—— 此前直读未复权 CSV，
    送转会让分子腰斩而窗口内 max_high 仍是复权前的高价，产生虚假回撤。
    2026-08-24 实测：P 集合 525 只中 100 只(19.0%)有送转，r60 全部受失真；
    比亚迪 2025-07-29 十送二十后，r60 显示 0.320 而实际 0.952。
    该失真直接污染 price_stress → SI → risk_band。

    选除权不除息而非完整前复权：r60 是比值，送转是除法可在比值中对消（历史值恒定），
    扣现金分红是减法不对消（历史值随每次分红漂移）。见 price_accessor 文档。
    """
    from price_accessor import PriceAccessor
    pa = PriceAccessor()
    stock_r60 = {}
    incremental = target_dates and len(target_dates) <= 10
    for code in codes:
        rows = pa.split_adjusted(code)
        if len(rows) < 60:
            continue
        if incremental:
            tail = rows[-70:]
            r60 = {}
            for j, row in enumerate(tail):
                if row["date"] not in target_dates:
                    continue
                gj = len(rows) - len(tail) + j
                window = rows[max(0, gj - 59):gj + 1]
                max_high = max(w["high"] for w in window)
                if max_high > 0:
                    r60[row["date"]] = row["close"] / max_high
        else:
            r60 = {}
            for j, row in enumerate(rows):
                max_high = max(w["high"] for w in rows[max(0, j - 59):j + 1])
                if max_high > 0:
                    r60[row["date"]] = row["close"] / max_high
        if r60:
            stock_r60[code] = r60
    return stock_r60


def load_margin_balances(bit_sup_codes, target_dates=None):
    """Load margin balances from margin_cache for Bit/Support stocks.

    When target_dates is small, loads only the needed dates (target + ~20d before).
    """
    margin_bal = {}
    margin_dates = []
    files = sorted(MARGIN_CACHE_DIR.glob("margin_*.json"))

    if target_dates and len(target_dates) <= 10:
        earliest = min(target_dates)
        ref_start = (datetime.strptime(earliest, "%Y-%m-%d")
                     - timedelta(days=25)).strftime("%Y-%m-%d")
        files = [f for f in files if f.stem.replace("margin_", "") >= ref_start]

    for fp in files:
        date_str = fp.stem.replace("margin_", "")
        data = json.loads(fp.read_text())
        sample = next(iter(data.values()), {})
        if "融资余额" not in sample:
            continue
        bals = {}
        for code in bit_sup_codes:
            entry = data.get(code, {})
            bal = entry.get("融资余额", 0)
            if bal and bal > 0:
                bals[code] = bal
        margin_bal[date_str] = bals
        margin_dates.append(date_str)
    margin_dates.sort()
    return margin_bal, margin_dates


def compute_price_stress(date, sector_codes, stock_r60):
    """Compute price_stress = clamp(-delta(ratio60, Bit-Support) / 10, 0, 1)."""
    bit_dmeans = []
    sup_dmeans = []
    for sec, codes in sector_codes.items():
        if sec in BIT_CORE:
            layer = "bit"
        elif sec in SUPPORT:
            layer = "sup"
        else:
            continue
        r60_vals = []
        for code in codes:
            r60 = stock_r60.get(code, {}).get(date)
            if r60 is not None:
                r60_vals.append(r60)
        if len(r60_vals) < 2:
            continue
        d_mean = statistics.mean([r - 1.0 for r in r60_vals]) * 100
        if layer == "bit":
            bit_dmeans.append(d_mean)
        else:
            sup_dmeans.append(d_mean)

    if bit_dmeans and sup_dmeans:
        delta = statistics.mean(bit_dmeans) - statistics.mean(sup_dmeans)
    else:
        delta = 0.0
    return clamp01(-delta / 10), delta


def compute_margin_stress(date, margin_bal, margin_dates, bit_sup_codes, code_layer):
    """Compute margin_stress = clamp(-deltaG%(Bit-Support) / 8, 0, 1)."""
    if date not in margin_bal:
        return None, None

    target_ref = (datetime.strptime(date, "%Y-%m-%d")
                  - timedelta(days=14)).strftime("%Y-%m-%d")
    ref_idx = bisect.bisect_right(margin_dates, target_ref) - 1
    if ref_idx < 0:
        return None, None

    ref_date = margin_dates[ref_idx]
    gap = (datetime.strptime(date, "%Y-%m-%d")
           - datetime.strptime(ref_date, "%Y-%m-%d")).days
    if not (10 <= gap <= 20):
        return None, None

    ref_bal = margin_bal[ref_date]
    cur_bal = margin_bal[date]
    bit_g, sup_g = [], []
    for code in bit_sup_codes:
        ly = code_layer[code]
        cb = cur_bal.get(code, 0)
        rb = ref_bal.get(code, 0)
        if cb > 0 and rb > 0:
            g = (cb - rb) / rb * 100
            if ly == "bit":
                bit_g.append(g)
            else:
                sup_g.append(g)

    if bit_g and sup_g:
        delta = statistics.median(bit_g) - statistics.median(sup_g)
        return clamp01(-delta / 8), delta
    return None, None


_CAP_SHIFT = {}      # 由 main/refresh 预填；compute_day 用


def compute_day(date, T, n_p, daily_sig, sector_codes, stock_r60,
                margin_bal, margin_dates, bit_sup_codes, code_layer):
    """Compute one day's Gate + SI record."""
    # sd%
    n_sig = len(daily_sig.get(date, set()))
    sd_pct = n_sig / n_p * 100 if n_p > 0 else 0.0

    # Gate —— 2026-08-26 重标为单条件，sd% 退出判定
    gate = "ALARM" if T < T_ALARM else "CLEAR"

    # price_stress
    ps, delta_price = compute_price_stress(date, sector_codes, stock_r60)

    # margin_stress
    ms_raw, delta_margin = compute_margin_stress(
        date, margin_bal, margin_dates, bit_sup_codes, code_layer)

    # temp_stress
    ts = clamp01((1 - T) / 0.6)

    # shift_stress —— 与 Layer1 的 cap_shift 同源，见 SHIFT_S0 注释块
    cs = _CAP_SHIFT.get(date)
    ss = clamp01(cs / SHIFT_S0) if cs is not None else 0.0

    # SI
    ms_used = ms_raw if ms_raw is not None else 0.05
    SI = (ps + ms_used + ts + ss) / 4

    # risk_band
    if gate == "CLEAR":
        band = "none"
    elif SI > BAND_CRITICAL:
        band = "critical"
    elif SI > BAND_ELEVATED:
        band = "elevated"
    else:
        band = "mild"

    return {
        "date": date,
        "T": round(T, 4),
        "sd_pct": round(sd_pct, 1),
        "gate": gate,
        "price_stress": round(ps, 4),
        "margin_stress": round(ms_raw, 4) if ms_raw is not None else None,
        "temp_stress": round(ts, 4),
        "shift_stress": round(ss, 4),
        "SI": round(SI, 4),
        "delta_price": round(delta_price, 2),
        "delta_margin": round(delta_margin, 2) if delta_margin is not None else None,
        "n_sig": n_sig,
        "n_p": n_p,
        "risk_band": band,
    }


def compute_si_max14(records):
    """Add SI_max14 (backward 14-day rolling max) to each record."""
    si_vals = [r["SI"] for r in records]
    for i, r in enumerate(records):
        start = max(0, i - 13)
        r["SI_max14"] = round(max(si_vals[start:i + 1]), 4)


def load_existing():
    """Load existing crash_gate_daily.json if present."""
    if not OUTPUT.exists():
        return [], set()
    data = json.loads(OUTPUT.read_text())
    daily = data.get("daily", [])
    existing_dates = {r["date"] for r in daily}
    return daily, existing_dates


def save_output(records):
    """Write crash_gate_daily.json with meta + daily array."""
    from zoneinfo import ZoneInfo
    BJT = ZoneInfo("Asia/Shanghai")
    now_str = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")

    dates = [r["date"] for r in records]
    output = {
        "meta": {
            "version": "1.0",
            "gate_formula": "T<1.0 AND sd%<25",
            "si_formula": "(price_stress+margin_stress+temp_stress)/3",
            "generated": now_str,
            "date_range": f"{min(dates)} ~ {max(dates)}" if dates else "",
            "total_days": len(records),
        },
        "daily": records,
    }
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=None,
                                 separators=(",", ":")))
    print(f"  Output: {OUTPUT} ({OUTPUT.stat().st_size / 1024:.0f} KB, {len(records)} days)")


def main():
    parser = argparse.ArgumentParser(description="Crash Gate + SI daily builder")
    parser.add_argument("--refresh-t", action="store_true",
                        help="只刷新 T 依赖的字段（T/temp_stress/SI/SI_max14/gate/risk_band），"
                             "其余原样保留。T 口径变更后用它，不要用 --backfill")
    parser.add_argument("--backfill", action="store_true",
                        help="全量回填（T 源 = t_causal.json；有护栏，需 --force）")
    parser.add_argument("--out", default=None,
                        help="输出到指定文件（对照用，不动生产文件）")
    parser.add_argument("--force", action="store_true",
                        help="允许 --backfill 覆盖已有记录（会丢失不可再生的历史，见下方护栏）")
    args = parser.parse_args()

    global OUTPUT
    if args.out:
        OUTPUT = Path(args.out)
    t0 = time.time()
    print("═══ Crash Gate + SI Builder ═══")
    print(f"  T 源: {T_CAUSAL.name}   输出: {OUTPUT.name}")

    # 1. Load T
    t_by_date = load_t_data(args.backfill)
    all_dates = sorted(t_by_date.keys())
    print(f"  T: {len(all_dates)} days ({all_dates[0]} ~ {all_dates[-1]})")

    # 2. Load existing output → find missing dates
    existing, existing_dates = load_existing()

    # ── --refresh-t：T 口径变更后的原地刷新 ──
    # --backfill 不适用：它只补缺口（missing），不重算已有行；而真正的全量重建会
    # 毁数据 —— margin_cache 是滚动窗口（当前只剩 18 天），重算会让 1500+ 天的
    # margin_stress 变 None 且不可恢复。故只重算确实依赖 T 的那几个字段：
    #     T · temp_stress · SI · SI_max14 · gate · risk_band
    # 保留：sd_pct · n_sig · price_stress · margin_stress · delta_* · n_p
    _CAP_SHIFT.update(load_cap_shift())
    if args.refresh_t:
        t_map = load_t_data()
        n_chg = 0
        for r in existing:
            newT = t_map.get(r["date"])
            if newT is None:
                continue
            if abs(r["T"] - newT) >= 1e-9:
                n_chg += 1
            r["T"] = round(newT, 4)
            r["temp_stress"] = round(clamp01((1 - newT) / 0.6), 4)

        n_risk = compute_risk(existing)      # 补 cap_shift / pT / pS / risk

        # gate / shift_stress / SI / risk_band 全在这里定，避免两处各写一遍
        for r in existing:
            if RISK_ALARM is not None and r.get("risk") is not None:
                r["gate"] = "ALARM" if r["risk"] > RISK_ALARM else "CLEAR"
            else:
                r["gate"] = "ALARM" if r["T"] < T_ALARM else "CLEAR"   # 暖机期回落
            ms = r["margin_stress"] if r["margin_stress"] is not None else 0.05
            cs = r.get("cap_shift")
            ss = clamp01(cs / SHIFT_S0) if cs is not None else 0.0
            r["shift_stress"] = round(ss, 4)
            SI = (r["price_stress"] + ms + r["temp_stress"] + ss) / 4
            r["SI"] = round(SI, 4)
            r["risk_band"] = ("none" if r["gate"] == "CLEAR" else
                              "critical" if SI > BAND_CRITICAL else
                              "elevated" if SI > BAND_ELEVATED else "mild")
        compute_si_max14(existing)
        out_path = Path(args.out) if args.out else OUTPUT
        payload = json.loads(out_path.read_text())
        payload["daily"] = existing
        from zoneinfo import ZoneInfo
        payload.setdefault("meta", {})["t_refreshed_at"] = \
            datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
        out_path.write_text(json.dumps(payload, ensure_ascii=False))
        from collections import Counter
        mode = f"risk>{RISK_ALARM}" if RISK_ALARM is not None else f"T<{T_ALARM}（risk 未标定）"
        print(f"  --refresh-t: {n_chg}/{len(existing)} 天 T 变动，"
              f"全部 {len(existing)} 天重判；risk 可用 {n_risk} 天；判定式 {mode}")
        print(f"  gate: {dict(Counter(r['gate'] for r in existing))}")
        print(f"  risk_band: {dict(Counter(r['risk_band'] for r in existing))}")
        print(f"  → {out_path.name}")
        return

    # ── 护栏：--backfill 会用今天的源数据重算全历史，但部分源是滚动窗口 ──
    # history/margin 最早只到 2025-01 左右 → 更早日期的 n_sig 会塌成偏小值；
    # margin_cache 早期覆盖不全 → margin_stress 大量变 None。
    # 2026-08-23 实测：一次无保护的 --backfill 让 2023-07~2025-11 共 571 天的
    # n_sig 均值从 122.6 掉到 47.2、margin_stress 的 None 从 19 涨到 379，
    # 且这些值再也无法重新生成。故默认拒绝覆盖已有记录。
    if args.backfill and existing_dates and not args.force:
        overlap = [d for d in all_dates if d in existing_dates]
        if overlap:
            print(f"  REFUSED: --backfill 会重写 {len(overlap)} 条已有记录 "
                  f"({min(overlap)} ~ {max(overlap)})。")
            print(f"  history/margin 与 margin_cache 是滚动窗口，早期日期的 "
                  f"n_sig / margin_stress 一旦重算即退化且不可恢复。")
            print(f"  确需重建请先备份 crash_gate_daily.json，再加 --force。")
            sys.exit(2)
    missing = [d for d in all_dates if d not in existing_dates]

    if not missing:
        # 没有缺口，但仍要补 risk —— 它依赖全序列的扩展窗口秩，不是逐日算得出的。
        # 若某天 risk 缺失（例如上一轮在 cap_shift 就位之前跑过），这里补上并重判。
        # 幂等：已有 risk 的行重算结果相同。
        n_gap = sum(1 for r in existing if r.get("risk") is None)
        n_risk = compute_risk(existing)
        n_regate = 0
        for r in existing:
            rk = r.get("risk")
            if rk is None:
                continue
            g = "ALARM" if rk > RISK_ALARM else "CLEAR"
            if g != r.get("gate"):
                n_regate += 1
            r["gate"] = g
            cs = r.get("cap_shift")
            r["shift_stress"] = round(clamp01(cs / SHIFT_S0), 4) if cs is not None else 0.0
            ms = r["margin_stress"] if r.get("margin_stress") is not None else 0.05
            r["SI"] = round((r["price_stress"] + ms + r["temp_stress"]
                             + r["shift_stress"]) / 4, 4)
            r["risk_band"] = ("none" if g == "CLEAR" else
                              "critical" if r["SI"] > BAND_CRITICAL else
                              "elevated" if r["SI"] > BAND_ELEVATED else "mild")
        still = sum(1 for r in existing if r.get("risk") is None)
        if n_regate or n_gap != still:
            compute_si_max14(existing)
            save_output(existing)
            print(f"  无新增日，但补齐了 risk：缺口 {n_gap} → {still} 天"
                  f"（暖机期 {RANK_WARMUP} 天本就无 risk），gate 改判 {n_regate} 天")
            latest = existing[-1]
            print(f"  Latest: {latest['date']}  T={latest['T']}  risk={latest.get('risk')}  "
                  f"Gate={latest['gate']}  SI={latest['SI']}  band={latest['risk_band']}")
        else:
            print(f"  All {len(all_dates)} days already computed，risk 也已齐备。")
        return

    # Guard: margin data is T+1; skip dates beyond latest available margin file
    margin_avail = {f.stem.replace("margin_", "")
                    for f in (SC / "margin_cache").glob("margin_*.json")}
    if margin_avail:
        latest_margin = max(margin_avail)
        beyond = [d for d in missing if d > latest_margin]
        if beyond:
            print(f"  Skipping {len(beyond)} dates beyond latest margin data"
                  f" ({latest_margin}): {beyond}")
            missing = [d for d in missing if d <= latest_margin]

    if not missing:
        print("  No computable dates (margin data not yet available).")
        return

    print(f"  Missing: {len(missing)} days "
          f"({missing[0]} ~ {missing[-1]})")

    incremental = len(missing) <= 10
    target = set(missing) if incremental else None

    # 3. Load P classification
    p_codes, code_layer, sector_codes = load_p_classification()
    bit_sup_codes = set(code_layer.keys())
    n_p = len(p_codes)
    n_bit = sum(1 for v in code_layer.values() if v == "bit")
    n_sup = sum(1 for v in code_layer.values() if v == "sup")
    print(f"  P: {n_p} stocks (bit={n_bit}, sup={n_sup})"
          + (" [incremental]" if incremental else ""))

    # 4. Load margin spikes → sd%
    print("  Loading margin spikes...")
    daily_sig, loaded = load_margin_spikes(p_codes, target)
    print(f"    {loaded} stocks loaded, {len(daily_sig)} days with signals")

    # 5. Load OHLCV → ratio60 (only Bit+Support stocks)
    print("  Loading OHLCV → ratio60...")
    stock_r60 = load_ohlcv_r60(bit_sup_codes, target)
    print(f"    {len(stock_r60)} Bit/Sup stocks loaded")

    # 6. Load margin balances
    print("  Loading margin_cache...")
    margin_bal, margin_dates = load_margin_balances(bit_sup_codes, target)
    print(f"    {len(margin_dates)} margin cache dates")

    # 7. Compute missing days
    print(f"  Computing {len(missing)} days...")
    new_records = []
    for i, date in enumerate(missing):
        T = t_by_date[date]
        rec = compute_day(date, T, n_p, daily_sig, sector_codes, stock_r60,
                          margin_bal, margin_dates, bit_sup_codes, code_layer)
        new_records.append(rec)
        if (i + 1) % 200 == 0:
            print(f"    {i + 1}/{len(missing)} ({date})")

    # 8. Merge + sort
    all_records = existing + new_records
    all_records.sort(key=lambda r: r["date"])

    # 8b. 补 cap_shift / pT / pS / risk，并按 risk 重判 gate 与 risk_band
    #
    # ━━ 2026-08-26 修复 ━━
    # compute_day 只算得出「不依赖扩展窗口秩」的那部分。pT/pS/risk 需要全序列
    # 才能算（扩展窗口秩要看整段历史），所以必须在合并之后补。
    # 此前 compute_risk 只挂在 --refresh-t 和全量重建上，**日频追加路径没有**，
    # 于是每天新增的那一行 risk=None，gate 静默落回已退役的 `T < T_ALARM` 老规则。
    # 实测 2026-08-26：risk 应为可算，却因此判成 CLEAR，而 08-25 是 ALARM(risk=0.687)。
    # 最新一天恰恰是最要紧的一天 —— 这个洞必须堵在追加路径上。
    n_risk = compute_risk(all_records)
    n_regate = 0
    for r in all_records:
        rk = r.get("risk")
        if rk is None:
            continue                      # 暖机期：保留 compute_day 的 T 阈值回退
        g = "ALARM" if rk > RISK_ALARM else "CLEAR"
        if g != r.get("gate"):
            n_regate += 1
        r["gate"] = g
        cs = r.get("cap_shift")
        r["shift_stress"] = round(clamp01(cs / SHIFT_S0), 4) if cs is not None else 0.0
        ms = r["margin_stress"] if r.get("margin_stress") is not None else 0.05
        r["SI"] = round((r["price_stress"] + ms + r["temp_stress"]
                         + r["shift_stress"]) / 4, 4)
        r["risk_band"] = ("none" if g == "CLEAR" else
                          "critical" if r["SI"] > BAND_CRITICAL else
                          "elevated" if r["SI"] > BAND_ELEVATED else "mild")
    if n_regate:
        print(f"  risk 重判 gate: {n_regate} 天改判（risk 可用 {n_risk} 天）")

    compute_si_max14(all_records)

    # 9. Save
    save_output(all_records)

    elapsed = time.time() - t0
    print(f"  Done: +{len(new_records)} new, {len(all_records)} total ({elapsed:.1f}s)")

    # 10. Print latest state
    latest = all_records[-1]
    print(f"\n  Latest: {latest['date']}  T={latest['T']}  "
          f"sd%={latest['sd_pct']}  Gate={latest['gate']}  "
          f"SI={latest['SI']}  SI_max14={latest['SI_max14']}  "
          f"alert={latest['risk_band']}")


if __name__ == "__main__":
    main()
