"""数据源层：多源容错。

实测结论（2026-10）：
  * 东财 push2 clist 每页硬顶 100 条，且深分页/高频请求会被掐断连接，不适合做全市场主源
  * 腾讯 web.ifzq.gtimg.cn K 线稳定、响应快，作为 K 线主源
  * 新浪 Market_Center 支持 100 条/页且并发耐受好，8 并发 60 页约 3 秒，作为快照主源

因此分工：
    全市场快照  -> 新浪（主） / 东财（备）
    K 线        -> 腾讯（主） / 东财（备）
    行业板块    -> 东财

容错：重试退避 → 多源切换 → stale-while-error（返回最近一次成功数据）
"""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import config

SINA_LIST = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
EM_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
EM_ULIST = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TX_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

SINA_PAGE_SIZE = 100
SINA_WORKERS = 8
SINA_MAX_PAGES = 80

_cache: dict[str, tuple[float, Any]] = {}
_last_good: dict[str, tuple[float, Any]] = {}
_stale_keys: set[str] = set()
_cache_lock = threading.Lock()


# ------------------------------------------------------------------ 基础工具
def _http_get(url: str, timeout: int | None = None, tries: int | None = None,
              headers: dict | None = None, encoding: str = "utf-8") -> str:
    timeout = timeout or config.HTTP_TIMEOUT
    tries = tries or config.HTTP_RETRIES
    hdrs = headers or config.HEADERS
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode(encoding, "ignore")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < tries - 1:
                time.sleep(min(6.0, 0.8 * (2 ** attempt)) + random.uniform(0, 0.3))
    raise RuntimeError(f"请求失败: {url[:100]} -> {last}")


def _cached(key: str, ttl: int, producer: Callable[[], Any], force: bool = False) -> Any:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not force and now - hit[0] < ttl:
            _stale_keys.discard(key)
            return hit[1]
    try:
        value = producer()
    except Exception:
        with _cache_lock:
            fallback = _last_good.get(key)
        if fallback:
            _stale_keys.add(key)
            return fallback[1]
        raise
    with _cache_lock:
        _cache[key] = (now, value)
        _last_good[key] = (now, value)
        _stale_keys.discard(key)
    return value


def _num(value: Any, default: float = 0.0) -> float:
    if value is None or value == "-" or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def cache_age(key: str) -> float | None:
    with _cache_lock:
        hit = _cache.get(key)
    return None if not hit else round(time.time() - hit[0], 1)


def is_stale(key: str) -> bool:
    with _cache_lock:
        return key in _stale_keys


# ------------------------------------------------------------------ 快照：新浪
SINA_HEADERS = {**config.HEADERS, "Referer": "https://finance.sina.com.cn"}


def _sina_total() -> int:
    """新浪沪深 A 股总数，用于确定分页数（避免把 null 误判成末页）。"""
    url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeStockCount?node=hs_a"
    )
    text = _http_get(url, tries=3, headers=SINA_HEADERS).strip().strip('"')
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 5600  # 兜底估值


def _sina_page(page: int, tries: int = 4) -> list[dict]:
    url = (
        f"{SINA_LIST}?page={page}&num={SINA_PAGE_SIZE}&sort=symbol&asc=1"
        "&node=hs_a&symbol=&_s_r_a=page"
    )
    last: Exception | None = None
    for attempt in range(tries):
        try:
            text = _http_get(url, tries=2, headers=SINA_HEADERS).strip()
            if not text or text == "null":
                raise RuntimeError("空响应/被限流")
            data = json.loads(text)
            if not isinstance(data, list):
                raise RuntimeError(f"异常结构 {type(data)}")
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.5 * (attempt + 1) + random.uniform(0, 0.3))
    raise RuntimeError(f"第 {page} 页失败: {last}")


def _sina_normalize(item: dict) -> dict | None:
    symbol = str(item.get("symbol") or "")
    if not (symbol.startswith("sh") or symbol.startswith("sz")):
        return None  # 剔除北交所，保持与东财 fs 口径一致
    price = _num(item.get("trade"))
    pre_close = _num(item.get("settlement"))
    high, low = _num(item.get("high")), _num(item.get("low"))
    return {
        "code": symbol[2:],
        "name": str(item.get("name") or "").strip(),
        "market": 1 if symbol.startswith("sh") else 0,
        "price": price,
        "pct": _num(item.get("changepercent")),
        "change": _num(item.get("pricechange")),
        "volume": round(_num(item.get("volume")) / 100, 0),      # 股 -> 手
        "amount": _num(item.get("amount")),                       # 元
        "amplitude": round((high - low) / pre_close * 100, 2) if pre_close else 0.0,
        "turnover": _num(item.get("turnoverratio")),              # %
        "pe": _num(item.get("per")),
        "pe_ttm": _num(item.get("per")),
        "vol_ratio": 0.0,                                          # 由 K 线计算
        "high": high,
        "low": low,
        "open": _num(item.get("open")),
        "pre_close": pre_close,
        "total_mv": _num(item.get("mktcap")) * 1e4,                # 万元 -> 元
        "float_mv": _num(item.get("nmc")) * 1e4,
        "pb": _num(item.get("pb")),
        "pct_60d": 0.0,                                            # 由 K 线计算
        "pct_ytd": 0.0,
        "main_flow": 0.0,
        "industry": "",
        "ticktime": str(item.get("ticktime") or ""),
    }


def _snapshot_from_sina() -> list[dict]:
    total = _sina_total()
    total_pages = min(SINA_MAX_PAGES, (total + SINA_PAGE_SIZE - 1) // SINA_PAGE_SIZE)
    pages = list(range(1, total_pages + 1))
    rows: list[dict] = []
    missing: list[int] = []

    def worker(page: int):
        try:
            return page, _sina_page(page)
        except Exception:  # noqa: BLE001
            return page, None

    with ThreadPoolExecutor(max_workers=SINA_WORKERS) as pool:
        for page, data in pool.map(worker, pages):
            if data is None:
                missing.append(page)
                continue
            for item in data:
                norm = _sina_normalize(item)
                if norm:
                    rows.append(norm)

    # 失败页串行补，最多两轮，避免并发被限流导致样本缺失
    for round_no in range(2):
        if not missing:
            break
        if round_no:
            time.sleep(1.5)
        for page in list(missing):
            try:
                data = _sina_page(page, tries=3)
            except Exception:  # noqa: BLE001
                continue
            missing.remove(page)
            for item in data:
                norm = _sina_normalize(item)
                if norm:
                    rows.append(norm)

    if not rows:
        raise RuntimeError(f"新浪快照为空（缺失页 {missing[:5]}）")
    if len(missing) > 3:
        raise RuntimeError(f"新浪缺失 {len(missing)} 页，降级到备用源")
    return rows


# ------------------------------------------------------------------ 快照：东财（备用）
def _snapshot_from_eastmoney() -> list[dict]:
    def fetch_page(pn: int):
        url = (
            f"{EM_CLIST}?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
            f"&fs={config.FS_ALL_A}&fields={config.SNAPSHOT_FIELDS}"
        )
        payload = json.loads(_http_get(url, tries=2))
        data = payload.get("data") or {}
        diff = data.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        return diff

    rows: list[dict] = []
    page = 1
    while page <= 60:
        try:
            diff = fetch_page(page)
        except Exception:  # noqa: BLE001
            break
        if not diff:
            break
        for item in diff:
            rows.append(
                {
                    "code": str(item.get("f12") or ""),
                    "name": str(item.get("f14") or ""),
                    "market": int(_num(item.get("f13"))),
                    "price": _num(item.get("f2")),
                    "pct": _num(item.get("f3")),
                    "change": _num(item.get("f4")),
                    "volume": _num(item.get("f5")),
                    "amount": _num(item.get("f6")),
                    "amplitude": _num(item.get("f7")),
                    "turnover": _num(item.get("f8")),
                    "pe": _num(item.get("f9")),
                    "pe_ttm": _num(item.get("f115")),
                    "vol_ratio": _num(item.get("f10")),
                    "high": _num(item.get("f15")),
                    "low": _num(item.get("f16")),
                    "open": _num(item.get("f17")),
                    "pre_close": _num(item.get("f18")),
                    "total_mv": _num(item.get("f20")),
                    "float_mv": _num(item.get("f21")),
                    "pb": _num(item.get("f23")),
                    "pct_60d": _num(item.get("f24")),
                    "pct_ytd": _num(item.get("f25")),
                    "main_flow": _num(item.get("f62")),
                    "industry": str(item.get("f100") or ""),
                    "ticktime": "",
                }
            )
        if len(diff) < 100:
            break
        page += 1
        time.sleep(0.3)
    if not rows:
        raise RuntimeError("东财快照为空")
    return rows


def get_snapshot(force: bool = False) -> list[dict]:
    """全市场（沪深 A 股）实时快照。新浪优先，失败切东财。"""

    def build() -> list[dict]:
        try:
            rows = _snapshot_from_sina()
            _snapshot_meta.update(source="sina", fetched_at=time.time())
            return rows
        except Exception as sina_err:  # noqa: BLE001
            try:
                rows = _snapshot_from_eastmoney()
                _snapshot_meta.update(source="eastmoney", fetched_at=time.time())
                return rows
            except Exception as em_err:  # noqa: BLE001
                raise RuntimeError(f"新浪失败({sina_err}); 东财失败({em_err})") from em_err

    return _cached("snapshot", config.SNAPSHOT_TTL, build, force)


_snapshot_meta: dict[str, Any] = {"source": "sina", "fetched_at": None}


def snapshot_source() -> str:
    """当前快照由哪个源提供（用于前端展示）。"""
    return str(_snapshot_meta.get("source") or "sina")


def snapshot_fetched_at() -> float | None:
    return _snapshot_meta.get("fetched_at")


# ------------------------------------------------------------------ K 线
def _calc_ma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < n:
            out.append(None)
        else:
            window = values[i + 1 - n : i + 1]
            out.append(round(sum(window) / n, 3))
    return out


def _package(code: str, name: str, market: int, klines: list[dict], source: str) -> dict:
    closes = [k["close"] for k in klines]
    vols = [k["volume"] for k in klines]
    vol_ma5: list[float | None] = []
    for i in range(len(vols)):
        window = vols[max(0, i - 4) : i + 1]
        vol_ma5.append(round(sum(window) / len(window), 1) if window else None)
    return {
        "code": code,
        "name": name,
        "market": market,
        "bars": len(klines),
        "klines": klines,
        "ma5": _calc_ma(closes, 5),
        "ma10": _calc_ma(closes, 10),
        "ma20": _calc_ma(closes, 20),
        "ma60": _calc_ma(closes, 60),
        "vol_ma5": vol_ma5,
        "source": source,
    }


def _kline_from_tencent(code: str, market: int, bars: int) -> dict:
    symbol = f"{'sh' if market == 1 else 'sz'}{code}"
    url = f"{TX_KLINE}?param={symbol},day,,,{bars},qfq"
    payload = json.loads(_http_get(url, tries=3))
    node = (payload.get("data") or {}).get(symbol) or {}
    raw = node.get("qfqday") or node.get("day") or []
    klines: list[dict] = []
    prev = None
    for row in raw:
        if len(row) < 6:
            continue
        close = _num(row[2])
        high, low = _num(row[3]), _num(row[4])
        klines.append(
            {
                "date": row[0],
                "open": _num(row[1]),
                "close": close,
                "high": high,
                "low": low,
                "volume": _num(row[5]),
                "amount": 0.0,
                "amplitude": round((high - low) / prev * 100, 2) if prev else 0.0,
                "pct": round((close / prev - 1) * 100, 2) if prev else 0.0,
                "change": round(close - prev, 3) if prev else 0.0,
                "turnover": 0.0,
            }
        )
        prev = close
    if not klines:
        raise RuntimeError("腾讯 K 线为空")
    return _package(code, code, market, klines, "tencent")


def _kline_from_eastmoney(code: str, market: int, bars: int) -> dict:
    url = (
        f"{EM_KLINE}?secid={market}.{code}&klt=101&fqt=1&beg=0&end=20500101&lmt={bars}"
        "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    )
    payload = json.loads(_http_get(url, tries=2))
    data = payload.get("data") or {}
    klines = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 11:
            continue
        klines.append(
            {
                "date": p[0],
                "open": _num(p[1]),
                "close": _num(p[2]),
                "high": _num(p[3]),
                "low": _num(p[4]),
                "volume": _num(p[5]),
                "amount": _num(p[6]),
                "amplitude": _num(p[7]),
                "pct": _num(p[8]),
                "change": _num(p[9]),
                "turnover": _num(p[10]),
            }
        )
    if not klines:
        raise RuntimeError("东财 K 线为空")
    return _package(code, data.get("name") or code, market, klines, "eastmoney")


def _kline_path(market: int, code: str, bars: int):
    return config.CACHE_DIR / f"kline_{market}_{code}_{bars}.json"


def get_kline(code: str, market: int, bars: int = config.KLINE_BARS, force: bool = False) -> dict:
    path = _kline_path(market, code, bars)
    if not force and path.exists():
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - blob.get("ts", 0) < config.KLINE_TTL and (blob.get("data") or {}).get("klines"):
                return blob["data"]
        except (OSError, json.JSONDecodeError):
            pass

    data = None
    for fetcher in (_kline_from_tencent, _kline_from_eastmoney):
        try:
            data = fetcher(code, market, bars)
            break
        except Exception:  # noqa: BLE001
            continue
    if data is None:
        raise RuntimeError(f"{code} K 线获取失败")
    try:
        path.write_text(json.dumps({"ts": time.time(), "data": data}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return data


def get_kline_batch(targets: list[tuple[str, int]], bars: int = config.KLINE_BARS) -> dict[str, dict]:
    def worker(t: tuple[str, int]):
        try:
            return t[0], get_kline(t[0], t[1], bars)
        except Exception:  # noqa: BLE001
            return t[0], None

    out: dict[str, dict] = {}
    if not targets:
        return out
    with ThreadPoolExecutor(max_workers=config.KLINE_WORKERS) as pool:
        for code, data in pool.map(worker, targets):
            if data:
                out[code] = data
    return out


# ------------------------------------------------------------------ 指数 / 板块
def get_indexes(force: bool = False) -> list[dict]:
    def build() -> list[dict]:
        secids = ",".join(s for s, _ in config.INDEX_LIST)
        url = f"{EM_ULIST}?secids={secids}&fields=f2,f3,f4,f12,f14&fltt=2&invt=2"
        payload = json.loads(_http_get(url, tries=3))
        diff = (payload.get("data") or {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        name_map = dict(config.INDEX_LIST)
        if not diff:
            raise RuntimeError("指数为空")
        return [
            {
                "code": str(item.get("f12") or ""),
                "name": item.get("f14") or name_map.get(str(item.get("f12") or ""), ""),
                "price": _num(item.get("f2")),
                "pct": _num(item.get("f3")),
                "change": _num(item.get("f4")),
            }
            for item in diff
        ]

    return _cached("indexes", config.INDEX_TTL, build, force)


SINA_HY = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"


def _industries_from_sina() -> list[dict]:
    """新浪行业板块：返回 49 个行业，含涨跌幅/成交额/成分股数（GBK 编码）。"""
    import re

    raw = _http_get(SINA_HY, tries=3, headers=SINA_HEADERS, encoding="gbk")
    match = re.search(r"=\s*(\{.*\})\s*;?\s*$", raw, re.S)
    if not match:
        raise RuntimeError("新浪行业数据结构异常")
    rows: list[dict] = []
    for _, value in re.findall(r'"([^"]+)":"([^"]*)"', match.group(1)):
        parts = value.split(",")
        if len(parts) < 9:
            continue
        rows.append(
            {
                "code": parts[0],
                "name": parts[1],
                "pct": _num(parts[5]),
                "price": _num(parts[3]),
                "main_flow": 0.0,
                "amount": _num(parts[7]),
                "up": 0,
                "down": 0,
                "count": int(_num(parts[2])),
            }
        )
    if not rows:
        raise RuntimeError("新浪行业为空")
    rows.sort(key=lambda r: -r["pct"])
    return rows


def get_industries(force: bool = False) -> list[dict]:
    """行业板块涨幅榜。东财优先，失败切新浪。"""

    def build() -> list[dict]:
        try:
            url = (
                f"{EM_CLIST}?pn=1&pz=90&po=1&np=1&fltt=2&invt=2&fid=f3"
                f"&fs={config.FS_INDUSTRY}&fields=f2,f3,f12,f14,f62,f104,f105"
            )
            payload = json.loads(_http_get(url, tries=2))
            diff = (payload.get("data") or {}).get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            if not diff:
                raise RuntimeError("东财板块为空")
            return [
                {
                    "code": str(item.get("f12") or ""),
                    "name": str(item.get("f14") or ""),
                    "pct": _num(item.get("f3")),
                    "price": _num(item.get("f2")),
                    "main_flow": _num(item.get("f62")),
                    "up": int(_num(item.get("f104"))),
                    "down": int(_num(item.get("f105"))),
                }
                for item in diff
            ]
        except Exception:  # noqa: BLE001
            return _industries_from_sina()

    return _cached("industries", config.INDUSTRY_TTL, build, force)


def health() -> dict:
    started = time.time()
    try:
        rows = get_snapshot()
        return {
            "ok": True,
            "stocks": len(rows),
            "latency_ms": int((time.time() - started) * 1000),
            "snapshot_age_s": cache_age("snapshot"),
            "stale": is_stale("snapshot"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
