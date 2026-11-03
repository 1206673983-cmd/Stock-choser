"""A 股智能选股工作台 —— 零依赖 HTTP 服务。

启动：
    python app.py                # 默认 127.0.0.1:8770
    python app.py --port 9000 --host 0.0.0.0

只用标准库 http.server，不需要 pip install 任何东西。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from screener import config, datasource as ds, service

STATIC_DIR = config.STATIC_DIR
_screen_lock = threading.Lock()

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


class Handler(BaseHTTPRequestHandler):
    server_version = "AShareScreener/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------- 基础响应
    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"error": "not found", "path": str(path)}, 404)
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 本地工具：一律不缓存，改完前端刷新即生效
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: Exception, status: int = 500) -> None:
        traceback.print_exc()
        self._send_json({"error": type(exc).__name__, "message": str(exc)[:400]}, status)

    def log_message(self, fmt: str, *args) -> None:  # 降噪：只打印 API 调用
        # 注意：send_error 路径下 args[0] 可能是 HTTPStatus 枚举而非字符串，
        # 直接做 `"x" in args[0]` 会抛 TypeError，必须先转成 str。
        try:
            target = str(args[0]) if args else ""
            if "/api/" in target:
                print(f"[{time.strftime('%H:%M:%S')}] {target} -> {args[1] if len(args) > 1 else ''}",
                      flush=True)
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if route in ("/", "/index.html"):
                self._send_file(STATIC_DIR / "index.html")
            elif route.startswith("/static/"):
                rel = route[len("/static/"):]
                target = (STATIC_DIR / rel).resolve()
                if not str(target).startswith(str(STATIC_DIR.resolve())):
                    self._send_json({"error": "forbidden"}, 403)
                    return
                self._send_file(target)
            elif route == "/api/health":
                self._send_json(ds.health())
            elif route == "/api/overview":
                self._send_json(service.market_overview())
            elif route == "/api/strategies":
                from screener import strategies
                self._send_json(
                    {
                        "strategies": strategies.list_strategies(),
                        "defaults": config.DEFAULT_FILTERS,
                        "max_kline_fetch": config.MAX_KLINE_FETCH,
                    }
                )
            elif route == "/api/kline":
                code = (query.get("code") or [""])[0]
                if not code:
                    self._send_json({"error": "code 必填"}, 400)
                    return
                market = query.get("market")
                market_i = int(market[0]) if market else None
                bars = int((query.get("bars") or [config.KLINE_BARS])[0])
                bars = max(30, min(bars, 500))
                self._send_json(service.stock_detail(code, market_i, bars))
            else:
                self._send_json({"error": "unknown route", "path": route}, 404)
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/screen":
            self._send_json({"error": "unknown route"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            params = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": "bad json", "message": str(exc)}, 400)
            return
        try:
            # 串行化，避免并发点选股把外部接口打爆
            if not _screen_lock.acquire(timeout=90):
                self._send_json({"error": "busy", "message": "上一次选股还在执行，请稍候"}, 429)
                return
            try:
                result = service.screen(params)
            finally:
                _screen_lock.release()
            self._send_json(result)
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description="A 股智能选股工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--no-warmup", action="store_true", help="启动时不预拉数据")
    args = parser.parse_args()

    if not args.no_warmup:
        def warm() -> None:
            try:
                started = time.time()
                rows = ds.get_snapshot()
                print(f"[warmup] 快照 {len(rows)} 只，{time.time() - started:.1f}s")
                ds.get_indexes()
                print("[warmup] 指数 OK")
            except Exception as exc:  # noqa: BLE001
                print(f"[warmup] 失败（不影响启动）：{exc}")

        threading.Thread(target=warm, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print("=" * 58)
    print("  A 股智能选股工作台 已启动")
    print(f"  http://{args.host}:{args.port}")
    print("  Ctrl+C 停止")
    print("=" * 58)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
