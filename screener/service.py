"""编排层：把数据源、因子、策略、打分串成一次选股流程。"""
from __future__ import annotations

import time
from typing import Any

from . import config, datasource as ds, factors, scoring, strategies


def _derive_all(snapshot: list[dict]) -> list[dict]:
    return [factors.derive(r) for r in snapshot]


def market_overview() -> dict:
    """大盘概览：指数、涨跌家数、成交额、板块榜。"""
    snapshot = ds.get_snapshot()
    rows = _derive_all(snapshot)

    up = sum(1 for r in rows if r["pct"] > 0)
    down = sum(1 for r in rows if r["pct"] < 0)
    flat = len(rows) - up - down
    limit_up = sum(1 for r in rows if r["pct"] >= 9.8)
    limit_down = sum(1 for r in rows if r["pct"] <= -9.8)
    total_amount = sum(r["amount"] for r in rows)

    try:
        indexes = ds.get_indexes()
    except Exception:  # noqa: BLE001
        indexes = []
    try:
        industries = ds.get_industries()
    except Exception:  # noqa: BLE001
        industries = []

    return {
        "indexes": indexes,
        "breadth": {
            "total": len(rows),
            "up": up,
            "down": down,
            "flat": flat,
            "limit_up": limit_up,
            "limit_down": limit_down,
        },
        "total_amount_yi": factors.yi(total_amount),
        "industries_top": industries[:8],
        "industries_bottom": industries[-5:][::-1] if industries else [],
        "source": ds.snapshot_source(),
        "snapshot_age_s": ds.cache_age("snapshot"),
        "stale": ds.is_stale("snapshot"),
    }


def screen(params: dict[str, Any] | None = None) -> dict:
    """执行一次选股。

    params:
        filters:    筛选条件（同 config.DEFAULT_FILTERS）
        strategies: 启用的策略 key 列表，None 表示全开
        top_n:      返回条数
    """
    params = params or {}
    started = time.time()
    filters = {**config.DEFAULT_FILTERS, **(params.get("filters") or {})}
    enabled = params.get("strategies")
    if enabled is not None:
        enabled = [k for k in enabled if k in strategies.REGISTRY]

    top_n = int(params.get("top_n") or 50)
    top_n = max(1, min(top_n, 300))

    t0 = time.time()
    snapshot = ds.get_snapshot()
    rows = _derive_all(snapshot)
    universe = factors.apply_filters(rows, filters)

    # 需要 K 线的策略只对「成交额最大的 N 只」取数，避免打爆接口
    active_keys = enabled if enabled is not None else list(strategies.REGISTRY.keys())
    need_kline = any(
        strategies.REGISTRY[k].need_kline for k in active_keys if k in strategies.REGISTRY
    )
    pool = sorted(universe, key=lambda r: -(r.get("amount") or 0))[: config.MAX_KLINE_FETCH]
    t1 = time.time()

    metrics_map: dict[str, dict] = {}
    if need_kline and pool:
        klines = ds.get_kline_batch([(r["code"], r["market"]) for r in pool])
        metrics_map = {
            code: factors.kline_metrics(kline) for code, kline in klines.items()
        }
    t2 = time.time()

    hits = strategies.run_strategies(pool, metrics_map, active_keys)
    scored = scoring.score_rows(universe, hits)
    t3 = time.time()

    top = scored[:top_n]
    for item in top:
        m = metrics_map.get(item["code"]) or {}
        item["kline_stats"] = {
            k: m.get(k)
            for k in ("ma5", "ma10", "ma20", "ma60", "pct_5d", "pct_20d", "pct_60d",
                      "vol_ratio5", "above_ma20", "drawdown3", "last_date")
            if m
        }

    return {
        "meta": {
            "total_stocks": len(rows),
            "universe": len(universe),
            "kline_pool": len(pool),
            "kline_covered": len(metrics_map),
            "signals_hit": len(hits),
            "returned": len(top),
            "strategy_count": len(active_keys),
            "timings_ms": {
                "snapshot": int((t1 - t0) * 1000),
                "kline": int((t2 - t1) * 1000),
                "score": int((t3 - t2) * 1000),
                "total": int((time.time() - started) * 1000),
            },
            "source": ds.snapshot_source(),
            "stale": ds.is_stale("snapshot"),
            "snapshot_age_s": ds.cache_age("snapshot"),
        },
        "filters": filters,
        "strategies": [
            s for s in strategies.list_strategies() if s["key"] in active_keys
        ],
        "rows": top,
        "signal_summary": _summarize_signals(hits),
    }


def _summarize_signals(hits: dict[str, list[dict]]) -> list[dict]:
    counter: dict[str, dict] = {}
    for signals in hits.values():
        for s in signals:
            node = counter.setdefault(
                s["key"], {"key": s["key"], "name": s["name"], "tag": s["tag"], "count": 0}
            )
            node["count"] += 1
    return sorted(counter.values(), key=lambda x: -x["count"])


def stock_detail(code: str, market: int | None = None, bars: int = config.KLINE_BARS) -> dict:
    """个股详情：实时快照 + 日 K + 指标。"""
    if market is None:
        market = 1 if code.startswith(("6", "9")) else 0
    kline = ds.get_kline(code, market, bars)
    metrics = factors.kline_metrics(kline)
    row: dict[str, Any] = {}
    try:
        snapshot = ds.get_snapshot()
        for item in snapshot:
            if item["code"] == code:
                row = factors.derive(item)
                break
    except Exception:  # noqa: BLE001
        pass
    return {"row": row, "kline": kline, "metrics": metrics}
