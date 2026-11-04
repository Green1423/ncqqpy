"""把 `Backend` 挂到一个真实 HTTP 端口上（仅供本地测试/演示）。

用标准库 `http.server`（不起额外依赖），线程里跑，随机端口：
`DektClient(base="http://127.0.0.1:<port>")` 就能直接打它 —— 这样本仓库的
同步链路（拉活动 → 归一化 → 入库 → 推送）可以**完全离线**地端到端跑一遍。

⚠️ 只绑 `127.0.0.1`，不对外监听；没有鉴权以外的任何"保护"，不要拿它当服务端。
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .backend import Backend, Response

__all__ = ["serve_in_thread", "SimServer"]


def _make_handler(backend: Backend):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):      # 静音：测试输出别被刷屏
            pass

        def _dispatch(self, method: str) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            resp: Response = backend.handle(
                method, parsed.path, query=query, body=body,
                headers={k.lower(): v for k, v in self.headers.items()})
            raw = json.dumps(resp.payload, ensure_ascii=False).encode()
            self.send_response(resp.status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(raw)))
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


class SimServer:
    """`with SimServer() as srv: srv.base` —— 退出时自动关线程与端口。"""

    def __init__(self, backend: Optional[Backend] = None, *, port: int = 0):
        self.backend = backend or Backend()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port),
                                          _make_handler(self.backend))
        self._httpd.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "SimServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="dekt-sim", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "SimServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def serve_in_thread(backend: Optional[Backend] = None, *,
                    port: int = 0) -> SimServer:
    """起一个模拟服务并返回（已启动）。记得 `close()` / 用 with。"""
    return SimServer(backend, port=port).start()
