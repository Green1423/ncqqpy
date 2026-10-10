"""iLink Bot 协议客户端（纯标准库）—— 扫码登录、长轮询、发文本/图片。"""

from .client import (
    ILinkClient,
    ILinkError,
    ILinkHTTPError,
    ClientOptions,
    Credentials,
    DEFAULT_BASE_URL,
    DEFAULT_CDN_BASE_URL,
    ERR_SESSION_TIMEOUT,
    crypto_available,
    crypto_hint,
)
from .login import login_with_qr, fetch_qr, poll_qr_status, LoginSession
from .notifier import Notifier

__all__ = [
    "ILinkClient", "ILinkError", "ILinkHTTPError", "ClientOptions", "Credentials",
    "DEFAULT_BASE_URL", "DEFAULT_CDN_BASE_URL", "ERR_SESSION_TIMEOUT",
    "crypto_available", "crypto_hint",
    "login_with_qr", "fetch_qr", "poll_qr_status", "LoginSession",
    "Notifier",
]
