"""编排层：把数据源、因子、策略、打分串成一次选股流程。"""
from __future__ import annotations

import time
from typing import Any

from . import config, datasource as ds, factors, scoring, strategies


def _derive_all(snapshot: list[dict]) -> list[dict]:
    return [factors.derive(r) for r in snapshot]


def _snapshot_closed(snapshot: list[dict]) -> bool:
    """快照是否停留在收盘状态（ticktime 停在 15:00 后）。

    用于识别节假日：纯时钟判断会把无交易的假日误判为盘中，导致把上一交易日的
    全天累计值再做折算放大。快照已收盘而时钟在盘中 -> 按全天口径（frac=1.0）。
    """
    sample = snapshot[:500]
    if not sample:
        return False
    closed = sum(1 for r in sample if str(r.get("ticktime") or "") >= "15:00")
    return closed > len(sample) * 0.9


def _session_frac(snapshot: list[dict]) -> float:
    """有效交易时段占比：盘中折算 + 节假日校正。"""
    frac = ds.session_fraction()
    if frac < 1.0 and _snapshot_closed(snapshot):
        return 1.0
    return frac


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
        "snapshot": ds.snapshot_meta(),
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
    frac = _session_frac(snapshot)
    universe = factors.apply_filters(rows, filters, session_frac=frac)

    # 需要 K 线的策略只对「成交额最大的 N 只」取数，避免打爆接口；
    # 纯快照策略（如低估值优质）则对整个候选池执行，避免漏掉池外的票。
    active_keys = enabled if enabled is not None else list(strategies.REGISTRY.keys())
    kline_keys = [k for k in active_keys
                  if k in strategies.REGISTRY and strategies.REGISTRY[k].need_kline]
    snapshot_keys = [k for k in active_keys
                     if k in strategies.REGISTRY and not strategies.REGISTRY[k].need_kline]

    pool = sorted(universe, key=lambda r: -(r.get("amount") or 0))[: config.MAX_KLINE_FETCH]
    t1 = time.time()

    metrics_map: dict[str, dict] = {}
    if kline_keys and pool:
        klines = ds.get_kline_batch([(r["code"], r["market"]) for r in pool])
        metrics_map = {
            code: factors.kline_metrics(kline) for code, kline in klines.items()
        }
    t2 = time.time()

    # 盘中折算：给候选池各行写入「全天预估」成交额/换手，策略层门槛统一用该口径，
    # 避免筛选层已折算而策略层仍用累计值的口径不一致。
    for r in universe:
        amt, to = r.get("amount") or 0, r.get("turnover") or 0
        r["amount_est"] = amt / frac if frac < 1.0 else amt
        r["turnover_est"] = to / frac if frac < 1.0 else to

    # 东财轻量补充：交易所口径量比 / 60 日涨幅 / PE(TTM)，写回候选池各行。
    # 量比在盘中已由交易所按时段折算，解决 K 线派生值系统性偏低的问题；
    # 失败时静默降级，策略层回退 K 线派生值。
    enrich = ds.get_enrichment([(r["code"], r["market"]) for r in pool])
    if enrich:
        for r in universe:
            e = enrich.get(r["code"])
            if not e:
                continue
            if e.get("vol_ratio", 0) > 0:
                r["vol_ratio"] = e["vol_ratio"]
            if e.get("pct_60d"):
                r["pct_60d"] = e["pct_60d"]
            if e.get("pe_ttm", 0) > 0:
                r["pe_ttm"] = e["pe_ttm"]
    t25 = time.time()

    hits: dict[str, list[dict]] = {}
    if kline_keys:
        hits.update(strategies.run_strategies(pool, metrics_map, kline_keys))
    if snapshot_keys:
        hits.update(strategies.run_strategies(universe, metrics_map, snapshot_keys))
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
            "kline_strategies": len(kline_keys),
            "snapshot_strategies": len(snapshot_keys),
            "session_frac": round(frac, 3),
            "intraday_adjusted": frac < 1.0,
            "enrich_ok": bool(enrich),
            "enrich_count": len(enrich),
            "timings_ms": {
                "snapshot": int((t1 - t0) * 1000),
                "kline": int((t2 - t1) * 1000),
                "enrich": int((t25 - t2) * 1000),
                "score": int((t3 - t2) * 1000),
                "total": int((time.time() - started) * 1000),
            },
            "source": ds.snapshot_source(),
            "stale": ds.is_stale("snapshot"),
            "snapshot_age_s": ds.cache_age("snapshot"),
            "snapshot": ds.snapshot_meta(),
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
