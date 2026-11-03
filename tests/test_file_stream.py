"""流式文件接口：`ApiClient` / `FileManager` 对接 NapCat 流式文件 API。

NapCat 的流式文件协议（源码 `packages/napcat-onebot/action/stream/`）：

- 下载：一条请求返回多条同 `echo` 的 WS 消息，`data.type` 为
  `stream`（`data_type` = `file_info` / `file_chunk`）或 `response`
  （`file_complete`）；分片是 Base64。
- 上传：客户端用固定 `stream_id` 多次调用 `upload_file_stream`，每次带一个
  Base64 分片；服务端依次返回 `file_created` / `chunk_received` / `file_complete`。

本套件覆盖：分块上传、流式下载、图片/语音下载、错误包、WsHandler 的多包路由。
"""
from __future__ import annotations

import asyncio
import base64
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from napcat.api import ApiClient, ApiError                     # noqa: E402
from napcat.client import WsHandler                             # noqa: E402
from napcat.file_manager import FileManager                     # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


class FakeWs:
    def __init__(self, *, events=None, upload_results=None):
        self.calls: list[dict] = []
        self.stream_calls: list[dict] = []
        self.events = list(events or [])
        self.upload_results = list(upload_results or [])
        self.throttles: list[bool] = []

    async def request(self, payload, timeout=30.0, *, throttle=True):
        self.calls.append(payload)
        self.throttles.append(throttle)
        data = (self.upload_results.pop(0) if self.upload_results
                else {"status": "chunk_received"})
        return SimpleNamespace(ok=True, retcode=0, data=data,
                               message="", wording="")

    async def stream_request(self, payload, timeout=60.0):
        self.stream_calls.append(payload)
        for event in self.events:
            yield {"status": "ok", "retcode": 0, "echo": payload["echo"],
                   "data": event}


async def test_upload():
    print("\n[1] 分块上传 upload_file_stream")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "f.bin"
        path.write_bytes(b"0123456789")
        ws = FakeWs(upload_results=[
            {"status": "chunk_received"},
            {"status": "chunk_received"},
            {"status": "file_complete", "file_path": "/srv/f.bin",
             "sha256": "abc"},
        ])
        api = ApiClient(ws)
        progress = []
        result = await api.upload_file_stream(
            str(path), chunk_size=4, retention=1000,
            on_progress=lambda sent, total: progress.append((sent, total)))
        check("上传完成返回服务端结果",
              result.get("status") == "file_complete"
              and result.get("file_path") == "/srv/f.bin", str(result))
        check("按 4 字节切成 3 块", len(ws.calls) == 3, str(len(ws.calls)))
        params = [c["params"] for c in ws.calls]
        check("stream_id 全程一致",
              len({p["stream_id"] for p in params}) == 1)
        check("分块索引递增且总数正确",
              [p["chunk_index"] for p in params] == [0, 1, 2]
              and all(p["total_chunks"] == 3 for p in params))
        check("携带文件大小/文件名/保留时间",
              all(p["file_size"] == 10 and p["filename"] == "f.bin"
                  and p["file_retention"] == 1000 for p in params))
        merged = b"".join(base64.b64decode(p["chunk_data"]) for p in params)
        check("分块内容拼回原文件", merged == b"0123456789", repr(merged))
        check("进度回调累计到总大小",
              progress == [(4, 10), (8, 10), (10, 10)], str(progress))
        check("分块上传不走 api_delay 节流",
              all(t is False for t in ws.throttles), str(ws.throttles))

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "g.bin"
        path.write_bytes(b"abcdefgh")
        ws = FakeWs(upload_results=[
            {"status": "chunk_received"}, {"status": "chunk_received"},
            {"status": "file_complete", "file_path": "/srv/g.bin"},
        ])
        api = ApiClient(ws)
        await api.upload_file_stream(str(path), chunk_size=4,
                                     expected_sha256="deadbeef")
        first = ws.calls[0]["params"]
        check("expected_sha256 透传", first.get("expected_sha256") == "deadbeef",
              str(first))

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "h.bin"
        path.write_bytes(b"abcdef")
        ws = FakeWs(upload_results=[
            {"status": "chunk_received"}, {"status": "chunk_received"},
            {"status": "chunk_received"},
            {"status": "file_complete", "file_path": "/srv/h.bin"},
        ])
        api = ApiClient(ws)
        result = await api.upload_file_stream(str(path), chunk_size=2)
        # 6 字节 / 2 = 3 块；末块只回 chunk_received → 再补一次 is_complete
        check("服务端未在末块完成时补发 is_complete",
              len(ws.calls) == 4 and ws.calls[-1]["params"].get("is_complete") is True,
              str([c["params"].get("is_complete") for c in ws.calls]))
        check("补发后拿到完成结果",
              result.get("status") == "file_complete", str(result))


async def test_download():
    print("\n[2] 流式下载 download_file_stream")
    events = [
        {"type": "stream", "data_type": "file_info",
         "file_name": "x.bin", "file_size": 10, "chunk_size": 4},
        {"type": "stream", "data_type": "file_chunk", "index": 0,
         "data": base64.b64encode(b"hello").decode(), "size": 5},
        {"type": "stream", "data_type": "file_chunk", "index": 1,
         "data": base64.b64encode(b"world").decode(), "size": 5},
        {"type": "response", "data_type": "file_complete",
         "total_chunks": 2, "total_bytes": 10},
    ]
    ws = FakeWs(events=events)
    api = ApiClient(ws)
    chunks = [c async for c in api.download_file_stream(
        file="http://example.com/x.bin", chunk_size=4096)]
    check("按分片产出字节", chunks == [b"hello", b"world"], repr(chunks))
    sent = ws.stream_calls[0]
    check("action 与参数正确",
          sent["action"] == "download_file_stream"
          and sent["params"] == {"chunk_size": 4096,
                                 "file": "http://example.com/x.bin"}, str(sent))

    ws2 = FakeWs(events=events)
    api2 = ApiClient(ws2)
    got = [e async for e in api2.iter_file_stream(file_id="fid")]
    check("iter_file_stream 产出全部事件", len(got) == 4
          and got[0]["data_type"] == "file_info", str([e.get("data_type") for e in got]))

    ws3 = FakeWs(events=events)
    api3 = ApiClient(ws3)
    await anext(api3.download_file_record_stream(file="v.amr", out_format="amr"))
    check("语音流带 out_format",
          ws3.stream_calls[0]["action"] == "download_file_record_stream"
          and ws3.stream_calls[0]["params"].get("out_format") == "amr",
          str(ws3.stream_calls[0]))

    ws4 = FakeWs(events=events)
    api4 = ApiClient(ws4)
    await anext(api4.download_file_image_stream(file="p.png"))
    check("图片流走 download_file_image_stream",
          ws4.stream_calls[0]["action"] == "download_file_image_stream",
          str(ws4.stream_calls[0]))

    ws5 = FakeWs(events=[{"type": "error", "data_type": "error"}])
    api5 = ApiClient(ws5)
    try:
        [c async for c in api5.download_file_stream(file="x")]
    except ApiError:
        check("错误包抛 ApiError", True)
    else:
        check("错误包抛 ApiError", False)


async def test_file_manager():
    print("\n[3] FileManager 走流式接口")
    body = b"hello world"
    events = [
        {"type": "stream", "data_type": "file_info",
         "file_name": "y.bin", "file_size": len(body)},
        {"type": "stream", "data_type": "file_chunk", "index": 0,
         "data": base64.b64encode(body).decode()},
        {"type": "response", "data_type": "file_complete",
         "total_chunks": 1, "total_bytes": len(body)},
    ]
    ws = FakeWs(events=events, upload_results=[
        {"status": "file_complete", "file_path": "/srv/z.bin"}])
    api = ApiClient(ws)
    fm = FileManager(api)

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "sub" / "y.bin"
        progress = []
        got = await fm.download_to_file("http://example.com/y.bin", str(dest),
                                        on_progress=lambda a, b: progress.append((a, b)))
        check("写入本地文件", Path(got).read_bytes() == body, got)
        check("进度回调带总大小", progress[-1] == (len(body), len(body)),
              str(progress))

        bytes_out = await fm.download_bytes("http://example.com/y.bin")
        check("download_bytes 拼接正确", bytes_out == body, repr(bytes_out))

        upfile = Path(td) / "z.bin"
        upfile.write_bytes(b"xyz")
        info = await fm.upload_stream(str(upfile))
        check("FileManager.upload_stream 走流式上传",
              info.get("status") == "file_complete", str(info))
        check("上传参数使用文件名",
              ws.calls[-1]["params"].get("filename") == "z.bin",
              str(ws.calls[-1]))

        await fm.clean_temp()
        check("clean_temp 调用 clean_stream_temp_file",
              ws.calls[-1]["action"] == "clean_stream_temp_file",
              str(ws.calls[-1]))


async def test_ws_routing():
    print("\n[4] WsHandler 多包路由")
    ws = WsHandler("ws://127.0.0.1:1")
    ws._running = True
    queue: asyncio.Queue = asyncio.Queue()
    ws._streams["e1"] = queue
    task = asyncio.create_task(ws._process_loop())
    try:
        await ws._msg_queue.put({"echo": "e1", "status": "ok",
                                 "data": {"type": "stream", "data_type": "file_info"}})
        await ws._msg_queue.put({"echo": "e1", "status": "ok",
                                 "data": {"type": "stream", "data_type": "file_chunk"}})
        await ws._msg_queue.put({"echo": "e1", "status": "ok",
                                 "data": {"type": "response", "data_type": "file_complete"}})
        got = [await asyncio.wait_for(queue.get(), 2) for _ in range(3)]
        check("三条同 echo 消息都进了流队列",
              [g["data"]["type"] for g in got] == ["stream", "stream", "response"],
              str([g["data"]["type"] for g in got]))
    finally:
        task.cancel()
        with __import__("contextlib").suppress(BaseException):
            await task
        ws._running = False

    ws._streams["e2"] = asyncio.Queue()
    ws._fail_pending(ConnectionError("断开"))
    check("断线时向流队列推送结束哨兵",
          ws._streams["e2"].get_nowait() is None)


async def main() -> int:
    await test_upload()
    await test_download()
    await test_file_manager()
    await test_ws_routing()
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
