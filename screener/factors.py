"""因子计算与筛选。

设计取舍：新浪快照不提供量比、60 日涨幅、行业，这些因子统一从日 K 派生，
因此「策略需要 K 线」的策略在 service 层只会拿到候选池（按成交额截断）的数据。
"""
from __future__ import annotations

import math
from typing import Iterable

ST_MARKERS = ("ST", "*ST", "退", "PT")


def is_st(name: str) -> bool:
    upper = (name or "").upper().replace(" ", "")
    return any(marker in upper for marker in ST_MARKERS)


def yi(value: float) -> float:
    """元 -> 亿元，保留 2 位。"""
    return round((value or 0.0) / 1e8, 2)


def derive(row: dict) -> dict:
    """给快照行补上便于前端展示/打分的派生字段。"""
    row = dict(row)
    row["amount_yi"] = yi(row.get("amount"))
    row["mv_yi"] = yi(row.get("total_mv"))
    row["float_mv_yi"] = yi(row.get("float_mv"))
    row["is_st"] = is_st(row.get("name", ""))
    row["main_flow_yi"] = yi(row.get("main_flow"))
    return row


def apply_filters(rows: Iterable[dict], f: dict) -> list[dict]:
    """按前端传入的条件过滤。0 值表示「不限」。"""
    min_amount = float(f.get("min_amount_yi") or 0) * 1e8
    min_price = float(f.get("min_price") or 0)
    max_price = float(f.get("max_price") or 0)
    pct_min = f.get("pct_min")
    pct_max = f.get("pct_max")
    to_min = float(f.get("turnover_min") or 0)
    to_max = float(f.get("turnover_max") or 0)
    pe_min = float(f.get("pe_min") or 0)
    pe_max = float(f.get("pe_max") or 0)
    pb_max = float(f.get("pb_max") or 0)
    mv_min = float(f.get("mv_min_yi") or 0) * 1e8
    exclude_st = bool(f.get("exclude_st", True))
    exclude_suspended = bool(f.get("exclude_suspended", True))

    out: list[dict] = []
    for row in rows:
        price = row.get("price") or 0
        amount = row.get("amount") or 0
        pe = row.get("pe") or 0
        pb = row.get("pb") or 0
        mv = row.get("total_mv") or 0
        turnover = row.get("turnover") or 0

        if price <= 0:                       # 停牌/无报价
            continue
        if exclude_suspended and amount <= 0:
            continue
        if min_price and price < min_price:
            continue
        if max_price and price > max_price:
            continue
        if amount < min_amount:
            continue
        if pct_min is not None and pct_min != "" and row.get("pct", 0) < float(pct_min):
            continue
        if pct_max is not None and pct_max != "" and row.get("pct", 0) > float(pct_max):
            continue
        if to_min and turnover < to_min:
            continue
        if to_max and turnover > to_max:
            continue
        if pe_max and (pe <= 0 or pe > pe_max):
            continue
        if pe_min and pe < pe_min:
            continue
        if pb_max and (pb <= 0 or pb > pb_max):
            continue
        if mv_min and mv < mv_min:
            continue
        if exclude_st and is_st(row.get("name", "")):
            continue
        out.append(row)
    return out


# ------------------------------------------------------------------ 排名工具
def percentile_ranks(values: list[float]) -> list[float]:
    """把一组数映射到 0~1 的分位（并列取平均秩）。"""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank / (n - 1)
        i = j + 1
    return ranks


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def band_score(value: float, low: float, high: float, soft: float = 0.35) -> float:
    """区间打分：落在 [low, high] 得满分，向两侧线性衰减。

    soft 控制衰减宽度（相对区间宽度），越大衰减越慢。
    """
    if value is None:
        return 0.0
    if low <= value <= high:
        return 1.0
    width = max(high - low, 1e-9)
    dist = (low - value) if value < low else (value - high)
    return clamp01(1.0 - dist / (width * soft * 4))


def log_scale(value: float, base: float = 10.0) -> float:
    return math.log10(max(value, 0.0) + 1.0) / math.log10(base)


# ------------------------------------------------------------------ K 线派生因子
def pct_change(klines: list[dict], bars: int) -> float | None:
    """近 bars 个交易日的涨跌幅（%）。"""
    if not klines or len(klines) <= bars:
        return None
    old, new = klines[-bars - 1]["close"], klines[-1]["close"]
    if old <= 0:
        return None
    return round((new / old - 1) * 100, 2)


def volume_ratio(klines: list[dict], window: int = 5) -> float | None:
    """量比：今日成交量 / 前 window 日均量（不含今日）。"""
    if not klines or len(klines) <= window:
        return None
    today = klines[-1]["volume"]
    hist = [k["volume"] for k in klines[-window - 1 : -1]]
    if not hist or sum(hist) <= 0:
        return None
    avg = sum(hist) / len(hist)
    return round(today / avg, 2) if avg > 0 else None


def kline_metrics(kline: dict) -> dict:
    """从日 K 提取策略与前端都要用的指标。"""
    kl = (kline or {}).get("klines") or []
    if not kl:
        return {}
    last = kl[-1]
    closes = [k["close"] for k in kl]
    highs = [k["high"] for k in kl]
    ma5 = (kline.get("ma5") or [None])[-1]
    ma10 = (kline.get("ma10") or [None])[-1]
    ma20 = (kline.get("ma20") or [None])[-1]
    ma60 = (kline.get("ma60") or [None])[-1]

    def ma_at(n: int, offset: int = 0) -> float | None:
        series = kline.get(f"ma{n}") or []
        idx = len(series) - 1 - offset
        return series[idx] if 0 <= idx < len(series) else None

    high20 = max(highs[-20:]) if len(highs) >= 20 else max(highs)
    high20_prev = max(highs[-21:-1]) if len(highs) >= 21 else high20
    drawdown3 = 0.0
    if len(closes) >= 4:
        recent_high = max(closes[-4:])
        drawdown3 = round((last["close"] / recent_high - 1) * 100, 2) if recent_high else 0.0

    # 近 20 日最高收盘价（不含今日），用于海龟突破判定
    close20_prev = max(closes[-21:-1]) if len(closes) >= 21 else max(closes[:-1] or closes)

    return {
        "last_date": last["date"],
        "last_close": last["close"],
        "prev_close": closes[-2] if len(closes) >= 2 else last["close"],
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "ma5_prev": ma_at(5, 1),
        "high20": round(high20, 2),
        "close20_prev": round(close20_prev, 2),
        "drawdown3": drawdown3,
        "pct_5d": pct_change(kl, 5),
        "pct_20d": pct_change(kl, 20),
        "pct_60d": pct_change(kl, 60),
        "vol_ratio5": volume_ratio(kl, 5),
        "above_ma20": bool(ma20 and last["close"] > ma20),
        "above_ma60": bool(ma60 and last["close"] > ma60),
        "bars": len(kl),
    }
