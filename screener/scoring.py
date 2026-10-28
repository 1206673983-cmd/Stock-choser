"""复合打分。

模型：基准分（快照因子，75%）+ 策略信号分（25%），输出 0~100。

基准分因子与权重：
    流动性(成交额)   25%
    估值(PE/PB)      25%
    换手活跃度       15%
    动量(今日涨幅)   15%
    市值适中度       12%
    振幅适中度       8%

这样即使一只票没有任何策略信号，也能凭基本面/流动性拿到合理分数；
命中策略则在基准分之上最多加 25 分，避免「没跑K线就必输」的偏差。
"""
from __future__ import annotations

from . import factors

BASE_WEIGHTS = {
    "liquidity": 0.25,
    "valuation": 0.25,
    "turnover": 0.15,
    "momentum": 0.15,
    "size": 0.12,
    "amplitude": 0.08,
}

SIGNAL_SHARE = 0.25


def _valuation_score(pe: float, pb: float) -> float:
    """PE 0~40、PB 0~6 区间内越低越好；亏损或缺失给 0.15 的底分。"""
    if pe <= 0 or pb <= 0:
        return 0.15
    pe_part = factors.clamp01(1 - (pe - 8) / 42) if pe > 8 else 1.0
    pb_part = factors.clamp01(1 - (pb - 1) / 7) if pb > 1 else 1.0
    return round(pe_part * 0.6 + pb_part * 0.4, 4)


def _turnover_score(turnover: float) -> float:
    """换手 3%~10% 最优，过低无人问津、过高博弈过度。"""
    if turnover <= 0:
        return 0.1
    if 3 <= turnover <= 10:
        return 1.0
    if turnover < 3:
        return factors.clamp01(turnover / 3)
    return factors.clamp01(1 - (turnover - 10) / 20)


def _size_score(mv_yi: float) -> float:
    """50~800 亿最舒服：太小流动性差，太大弹性不足。"""
    if mv_yi <= 0:
        return 0.1
    return factors.band_score(mv_yi, 50, 800, soft=0.6)


def _amplitude_score(amplitude: float) -> float:
    """振幅 2%~8% 有波动但不失控。"""
    return factors.band_score(amplitude, 2, 8, soft=0.5)


def base_components(rows: list[dict]) -> dict[str, list[float]]:
    """对候选全集做横向分位归一，返回每个因子 0~1 的分数序列。"""
    amounts = [float(r.get("amount") or 0) for r in rows]
    pcts = [float(r.get("pct") or 0) for r in rows]

    liquidity = factors.percentile_ranks(amounts)
    momentum = factors.percentile_ranks(pcts)
    valuation = [_valuation_score(float(r.get("pe") or 0), float(r.get("pb") or 0)) for r in rows]
    turnover = [_turnover_score(float(r.get("turnover") or 0)) for r in rows]
    size = [_size_score(float(r.get("mv_yi") or 0)) for r in rows]
    amplitude = [_amplitude_score(float(r.get("amplitude") or 0)) for r in rows]
    return {
        "liquidity": liquidity,
        "valuation": valuation,
        "turnover": turnover,
        "momentum": momentum,
        "size": size,
        "amplitude": amplitude,
    }


def score_rows(rows: list[dict], hits: dict[str, list[dict]] | None = None) -> list[dict]:
    """给每个候选打总分并挂上策略信号，按总分降序返回。"""
    hits = hits or {}
    comps = base_components(rows)
    scored: list[dict] = []

    for idx, row in enumerate(rows):
        base = 0.0
        detail = {}
        for key, weight in BASE_WEIGHTS.items():
            value = round(comps[key][idx], 4)
            detail[key] = round(value * weight * 100, 2)
            base += value * weight

        signals = hits.get(row.get("code"), [])
        signal_part = 0.0
        if signals:
            signal_part = max(min(1.0, s["score"] * s.get("weight", 1.0)) for s in signals)

        total = (base * (1 - SIGNAL_SHARE) + signal_part * SIGNAL_SHARE) * 100
        item = dict(row)
        item["score"] = round(total, 1)
        item["base_score"] = round(base * 100, 1)
        item["signal_score"] = round(signal_part * 100, 1)
        item["components"] = detail
        item["signals"] = signals
        item["signal_names"] = [s["name"] for s in signals]
        scored.append(item)

    scored.sort(key=lambda r: (-r["score"], -float(r.get("amount") or 0)))
    for rank, item in enumerate(scored, start=1):
        item["rank"] = rank
    return scored
