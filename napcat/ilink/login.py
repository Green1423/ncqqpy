"""
iLink Bot 扫码登录。

流程（对齐官方实现）：

    GET  /ilink/bot/get_bot_qrcode?bot_type=3   → {qrcode, qrcode_img_content}
    GET  /ilink/bot/get_qrcode_status?qrcode=…  → 长轮询（服务端最多 hold 35s）
         status: wait / scaned / scaned_but_redirect / expired / confirmed
    confirmed → {ilink_bot_id, bot_token, baseurl, ilink_user_id}

二维码本身是个 URL（`qrcode_img_content`），除了终端 ASCII 渲染，调用方也可以
直接把它当链接发出去 —— 手机点开就能扫，这比传图片更省事。
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Callable, Optional

from .client import (
    BOT_TYPE,
    DEFAULT_BASE_URL,
    LOGIN_TIMEOUT,
    MAX_QR_REFRESH,
    QR_POLL_TIMEOUT,
    Credentials,
    ILinkHTTPError,
    _get,
    ClientOptions,
)

__all__ = ["fetch_qr", "poll_qr_status", "login_with_qr", "LoginSession"]


def fetch_qr(base_url: str = DEFAULT_BASE_URL, bot_type: str = BOT_TYPE) -> dict:
    """取一张新的登录二维码。返回 {qrcode, qrcode_img_content}。"""
    opts = ClientOptions(base_url=base_url)
    return _get(opts, "ilink/bot/get_bot_qrcode", {"bot_type": bot_type}, timeout=20.0)


def poll_qr_status(qrcode: str, base_url: str = DEFAULT_BASE_URL,
                   timeout: float = QR_POLL_TIMEOUT) -> dict:
    """查一次扫码状态。网络抖动一律返回 `{"status": "wait"}`（继续等）。"""
    opts = ClientOptions(base_url=base_url)
    try:
        return _get(opts, "ilink/bot/get_qrcode_status", {"qrcode": qrcode},
                    timeout=timeout)
    except ILinkHTTPError:
        return {"status": "wait"}


class LoginSession:
    """一次扫码登录的会话状态（便于外部渲染二维码 / 展示进度）。"""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, bot_type: str = BOT_TYPE):
        self.base_url = base_url
        self.bot_type = bot_type
        self.qrcode: str = ""
        self.qrcode_url: str = ""
        self.status: str = "init"
        self.refreshes: int = 0

    def refresh(self) -> str:
        """（重新）取二维码，返回二维码 URL（内容）。"""
        qr = fetch_qr(self.base_url, self.bot_type)
        self.qrcode = qr.get("qrcode", "")
        self.qrcode_url = qr.get("qrcode_img_content", "")
        self.refreshes += 1
        self.status = "waiting"
        return self.qrcode_url

    def poll(self) -> str:
        """查一次状态并更新 `self.status`，返回状态字符串。"""
        body = poll_qr_status(self.qrcode, self.base_url)
        self.status = body.get("status", "wait")
        self._last_body = body
        return self.status

    @property
    def last_body(self) -> dict:
        return getattr(self, "_last_body", {})


def login_with_qr(
    on_qrcode: Callable[[str], None],
    on_status: Optional[Callable[[str], None]] = None,
    *,
    base_url: str = DEFAULT_BASE_URL,
    bot_type: str = BOT_TYPE,
    timeout: float = LOGIN_TIMEOUT,
    session: LoginSession = None,
) -> Credentials:
    """阻塞式扫码登录，成功返回 `Credentials`。

    - `on_qrcode(url)`：拿到（或刷新出）二维码时回调 —— 终端渲染 / 转发链接都在这里
    - `on_status(status)`：状态变化回调（waiting/scanned/refreshing/redirected）
    - `scaned_but_redirect`：微信会把人导向最近的 IDC，需要换 host 继续轮询
    """
    sess = session or LoginSession(base_url=base_url, bot_type=bot_type)
    state_cb = on_status or (lambda _s: None)

    on_qrcode(sess.refresh())
    state_cb("waiting")

    polling_base = sess.base_url
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = sess.poll()
        body = sess.last_body

        if status == "scaned":
            state_cb("scanned")
        elif status == "expired":
            if sess.refreshes >= MAX_QR_REFRESH:
                raise TimeoutError(f"二维码过期次数过多（{sess.refreshes} 次）")
            state_cb("refreshing")
            polling_base = sess.base_url
            on_qrcode(sess.refresh())
            state_cb("waiting")
        elif status == "scaned_but_redirect":
            host = body.get("redirect_host")
            if host:
                polling_base = f"https://{host}"
                sess.base_url = polling_base
                state_cb("redirected")
        elif status == "confirmed":
            token = body.get("bot_token")
            account = body.get("ilink_bot_id")
            if not token or not account:
                raise RuntimeError(f"登录失败：服务端未返回凭据（{body}）")
            state_cb("confirmed")
            return Credentials(
                bot_token=token,
                account_id=account,
                base_url=body.get("baseurl") or sess.base_url,
                user_id=body.get("ilink_user_id", ""),
            )
        time.sleep(1.0)

    raise TimeoutError(f"扫码登录超时（{timeout:.0f}s）")
