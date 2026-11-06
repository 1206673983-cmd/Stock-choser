"""策略引擎。

结构参考 sngyai/Sequoia-X 的 strategy/base.py：
所有策略继承 BaseStrategy，统一返回 0~1 的置信分 + 可读理由，
由 scoring 层负责把信号折算成总分，策略之间互不感知。
"""
from __future__ import annotations

from typing import Any

from . import factors


class BaseStrategy:
    """策略基类。"""

    key: str = ""
    name: str = ""
    desc: str = ""
    tag: str = ""
    need_kline: bool = True
    need_rank: bool = False
    weight: float = 1.0

    def evaluate(self, row: dict, metrics: dict, ctx: dict) -> dict | None:
        """命中则返回 {'score': 0~1, 'reasons': [...], 'metrics': {...}}，否则 None。"""
        raise NotImplementedError

    def meta(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "desc": self.desc,
            "tag": self.tag,
            "need_kline": self.need_kline,
            "weight": self.weight,
        }


REGISTRY: dict[str, BaseStrategy] = {}


def _vol_ratio(row: dict, m: dict) -> float | None:
    """量比：优先快照的交易所口径（东财 f10，盘中已按时段折算），缺失时回退 K 线派生值。

    K 线派生值 = 当日累计成交量 / 前 5 日完整均量，盘中会系统性偏低
    （2026-11-05 10:10 实测 160 只池内中位仅 0.45），只应作离线兜底。
    """
    v = row.get("vol_ratio")
    if v and v > 0:
        return round(float(v), 2)
    return m.get("vol_ratio5")


def register(cls: type[BaseStrategy]) -> type[BaseStrategy]:
    REGISTRY[cls.key] = cls()
    return cls


# ------------------------------------------------------------------ 1. 海龟突破
@register
class TurtleBreakout(BaseStrategy):
    key = "turtle_breakout"
    name = "海龟突破"
    tag = "动量"
    desc = "收盘创 20 日新高，且当日成交额过亿、收阳线，防诱多"
    need_kline = True
    weight = 1.2

    def evaluate(self, row: dict, m: dict, ctx: dict) -> dict | None:
        if not m or m.get("bars", 0) < 21:
            return None
        close = m["last_close"]
        level = m["close20_prev"]
        if not level or close < level * 0.998:
            return None
        amount_est = row.get("amount_est") or row.get("amount") or 0
        if amount_est < 1e8:
            return None
        if close < (row.get("open") or 0):
            return None
        breakout = (close / level - 1) * 100
        vol_ratio = _vol_ratio(row, m) or 1.0
        score = min(1.0, 0.55 + min(breakout, 6) * 0.05 + min(vol_ratio, 3) * 0.08)
        reasons = [
            f"突破 20 日高点 {level:.2f}（+{breakout:.1f}%）",
            f"成交额(预估) {amount_est / 1e8:.2f} 亿",
            f"量比 {vol_ratio:.2f}",
        ]
        return {"score": round(score, 3), "reasons": reasons,
                "metrics": {"breakout_pct": round(breakout, 2), "vol_ratio5": vol_ratio}}


# ------------------------------------------------------------------ 2. 均线多头放量
@register
class MaBullVolume(BaseStrategy):
    key = "ma_bull_volume"
    name = "均线多头放量"
    tag = "趋势"
    desc = "MA5 > MA10 > MA20 多头排列，且成交量放大到 5 日均量 1.3 倍以上"
    need_kline = True
    weight = 1.1

    def evaluate(self, row: dict, m: dict, ctx: dict) -> dict | None:
        ma5, ma10, ma20 = m.get("ma5"), m.get("ma10"), m.get("ma20")
        if not (ma5 and ma10 and ma20):
            return None
        if not (ma5 > ma10 > ma20):
            return None
        vr = _vol_ratio(row, m) or 0
        if vr < 1.3:
            return None
        if (row.get("pct") or 0) <= 0:
            return None
        spread = (ma5 / ma20 - 1) * 100
        score = min(1.0, 0.5 + min(vr - 1.3, 2.0) * 0.15 + min(spread, 10) * 0.02)
        reasons = [
            f"MA5/10/20 多头排列（5-20 日线乖离 {spread:.1f}%）",
            f"量比 {vr:.2f}",
            f"今日 {row.get('pct', 0):+.2f}%",
        ]
        return {"score": round(score, 3), "reasons": reasons,
                "metrics": {"ma_spread": round(spread, 2), "vol_ratio5": vr}}


# ------------------------------------------------------------------ 3. 强势缩量回踩
@register
class PullbackHold(BaseStrategy):
    key = "pullback_hold"
    name = "强势缩量回踩"
    tag = "低吸"
    desc = "20 日涨幅超 15% 的强势股，回调 3%~12% 且明显缩量、仍站上 MA20"
    need_kline = True
    weight = 1.0

    def evaluate(self, row: dict, m: dict, ctx: dict) -> dict | None:
        pct20 = m.get("pct_20d")
        if pct20 is None or pct20 < 15:
            return None
        dd = m.get("drawdown3") or 0
        if not (-12 <= dd <= -3):
            return None
        vr = _vol_ratio(row, m)
        if vr is None or vr > 0.9:
            return None
        if not m.get("above_ma20"):
            return None
        score = min(1.0, 0.5 + min(pct20, 60) / 200 + (0.9 - vr) * 0.3)
        reasons = [
            f"20 日涨幅 {pct20:+.1f}%",
            f"近 3 日回撤 {dd:.1f}%",
            f"缩量至 5 日均量 {vr:.2f} 倍",
            "仍站上 MA20",
        ]
        return {"score": round(score, 3), "reasons": reasons,
                "metrics": {"pct_20d": pct20, "drawdown3": dd, "vol_ratio5": vr}}


# ------------------------------------------------------------------ 4. 低估值优质
@register
class ValueQuality(BaseStrategy):
    key = "value_quality"
    name = "低估值优质"
    tag = "价值"
    desc = "PE 0~20、PB 0~2.5、市值 100 亿以上、换手温和，剔除非盈利公司"
    need_kline = False
    weight = 1.0

    def evaluate(self, row: dict, m: dict, ctx: dict) -> dict | None:
        pe = row.get("pe") or 0
        pb = row.get("pb") or 0
        mv = row.get("mv_yi") or 0
        to = row.get("turnover_est") or row.get("turnover") or 0
        if not (0 < pe <= 20):
            return None
        if not (0 < pb <= 2.5):
            return None
        if mv < 100:
            return None
        if not (0.2 <= to <= 6):
            return None
        if (row.get("pct") or 0) < -3:
            return None
        score = min(
            1.0,
            0.35
            + (1 - pe / 20) * 0.35
            + (1 - pb / 2.5) * 0.2
            + min(mv, 1000) / 1000 * 0.1,
        )
        reasons = [
            f"PE {pe:.1f} / PB {pb:.2f}",
            f"总市值 {mv:.0f} 亿",
            f"换手 {to:.2f}%",
        ]
        return {"score": round(score, 3), "reasons": reasons,
                "metrics": {"pe": pe, "pb": pb, "mv_yi": mv}}


# ------------------------------------------------------------------ 5. 资金异动
@register
class CapitalSurge(BaseStrategy):
    key = "capital_surge"
    name = "资金异动"
    tag = "资金"
    desc = "量比 2 倍以上、涨幅 2%~9.5% 未封板、换手 2%~15%、成交额 3 亿以上"
    need_kline = True
    weight = 1.1

    def evaluate(self, row: dict, m: dict, ctx: dict) -> dict | None:
        vr = _vol_ratio(row, m)
        if vr is None or vr < 2:
            return None
        pct = row.get("pct") or 0
        if not (2 <= pct <= 9.5):
            return None
        to = row.get("turnover_est") or row.get("turnover") or 0
        if not (2 <= to <= 15):
            return None
        amount_est = row.get("amount_est") or row.get("amount") or 0
        if amount_est < 3e8:
            return None
        score = min(1.0, 0.5 + min(vr - 2, 4) * 0.1 + min(to, 10) * 0.02)
        reasons = [
            f"量比 {vr:.2f} 倍",
            f"今日 {pct:+.2f}%（未封板）",
            f"换手(预估) {to:.2f}%，成交额(预估) {amount_est / 1e8:.2f} 亿",
        ]
        return {"score": round(score, 3), "reasons": reasons,
                "metrics": {"vol_ratio5": vr, "turnover": to}}


# ------------------------------------------------------------------ 6. 相对强度 RPS
@register
class RpsStrength(BaseStrategy):
    key = "rps_strength"
    name = "相对强度 RPS"
    tag = "动量"
    desc = "60 日涨幅位居候选池前 10%、站上 MA20 且今日收红（欧奈尔 RPS 思路）"
    need_kline = True
    need_rank = True
    weight = 1.2

    def evaluate(self, row: dict, m: dict, ctx: dict) -> dict | None:
        ranks = ctx.get("pct60_rank") or {}
        rank = ranks.get(row.get("code"))
        if rank is None or rank < 0.9:
            return None
        pct60 = m.get("pct_60d")
        if pct60 is None:
            return None
        if not m.get("above_ma20"):
            return None
        if (row.get("pct") or 0) <= 0:
            return None
        score = min(1.0, 0.6 + (rank - 0.9) * 3 + min(pct60, 150) / 500)
        reasons = [
            f"60 日涨幅 {pct60:+.1f}%（池内前 {(1 - rank) * 100:.0f}%）",
            "站上 MA20",
            f"今日 {row.get('pct', 0):+.2f}%",
        ]
        return {"score": round(score, 3), "reasons": reasons,
                "metrics": {"pct_60d": pct60, "rank": round(rank, 3)}}


# ------------------------------------------------------------------ 对外接口
def list_strategies() -> list[dict]:
    return [s.meta() for s in REGISTRY.values()]


def get_strategy(key: str) -> BaseStrategy | None:
    return REGISTRY.get(key)


def build_rank_context(metrics_map: dict[str, dict]) -> dict[str, Any]:
    """为需要横向排名的策略准备上下文（当前只有 60 日涨幅分位）。"""
    codes, values = [], []
    for code, m in metrics_map.items():
        pct60 = (m or {}).get("pct_60d")
        if pct60 is not None:
            codes.append(code)
            values.append(pct60)
    ranks = factors.percentile_ranks(values) if values else []
    return {"pct60_rank": dict(zip(codes, ranks))}


def run_strategies(rows: list[dict], metrics_map: dict[str, dict],
                   enabled: list[str] | None = None) -> dict[str, list[dict]]:
    """对候选池执行策略，返回 code -> [信号, ...]。"""
    ctx = build_rank_context(metrics_map)
    keys = enabled or list(REGISTRY.keys())
    hits: dict[str, list[dict]] = {}
    for key in keys:
        strategy = REGISTRY.get(key)
        if strategy is None:
            continue
        for row in rows:
            code = row.get("code")
            metrics = metrics_map.get(code) or {}
            if strategy.need_kline and not metrics:
                continue
            try:
                result = strategy.evaluate(row, metrics, ctx)
            except Exception:  # noqa: BLE001 - 单策略异常不影响整体
                continue
            if not result:
                continue
            hits.setdefault(code, []).append(
                {
                    "key": strategy.key,
                    "name": strategy.name,
                    "tag": strategy.tag,
                    "score": result["score"],
                    "weight": strategy.weight,
                    "reasons": result["reasons"],
                    "metrics": result.get("metrics", {}),
                }
            )
    for signals in hits.values():
        signals.sort(key=lambda s: -s["score"] * s["weight"])
    return hits
