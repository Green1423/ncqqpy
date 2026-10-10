"""
微信 iLink Bot 协议客户端（纯标准库实现）。

腾讯官方 iLink（智联）Bot 协议 —— 微信 ClawBot 功能的底层通道，接入域名
`ilinkai.weixin.qq.com`，HTTP/JSON，扫码登录后拿 `bot_token` 做 Bearer 鉴权。

这个模块**不依赖第三方包**（`urllib` + `json` 搞定），原因很实际：
机器人跑在只读的 `/` 上，`pip install` 会失败；SDK 可用时反而会变成新的部署风险。
只有「发图片」需要 AES-128-ECB（`cryptography`），走可选依赖，缺失时降级成纯文本 +
二维码链接（见 `available()` / `_aes_ecb_encrypt`）。

协议要点（对齐官方 `@tencent-weixin/openclaw-weixin` 2.1.8）：

| 现象 | 原因 |
|------|------|
| 每个 POST 都要 `X-WECHAT-UIN` | 随机 uint32 → base64，防重放 |
| `errcode=-14 session timeout` | 登录态过期，必须重新扫码 |
| 发送必须带 `context_token` | 该 token 只出现在**入站消息**里，进程重启后要重新收一条消息 |
| 图片走 CDN 且 AES-128-ECB 加密 | 先 `getuploadurl` 拿预签名地址，PUT 密文，回填 `x-encrypted-param` |
"""
from __future__ import annotations

import base64
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

# ---- 协议常量（对齐官方 2.1.8） ----
DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
ILINK_CLIENT_VERSION = 131336          # 0x020108 = "2.1.8"
CHANNEL_VERSION = "ncqq-watchdog/1.0.0"
BOT_TYPE = "3"

LONG_POLL_TIMEOUT = 35.0
API_TIMEOUT = 20.0
LOGIN_TIMEOUT = 480.0                  # 扫码总时限
QR_POLL_TIMEOUT = 35.0
MAX_QR_REFRESH = 3

MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

UPLOAD_IMAGE = 1
UPLOAD_VIDEO = 2
UPLOAD_FILE = 3
UPLOAD_VOICE = 4

ERR_SESSION_TIMEOUT = -14


class ILinkError(RuntimeError):
    """iLink 接口返回了非 0 的 errcode。"""

    def __init__(self, errcode: int, errmsg: str, endpoint: str = ""):
        self.errcode = errcode
        self.errmsg = errmsg
        self.endpoint = endpoint
        super().__init__(f"iLink {endpoint} errcode={errcode}: {errmsg}")

    @property
    def session_expired(self) -> bool:
        """登录态过期（需要重新扫码）。"""
        return self.errcode == ERR_SESSION_TIMEOUT


class ILinkHTTPError(RuntimeError):
    """传输层/HTTP 层错误（网络不通、5xx、非法 JSON）。"""


# ---------------------------------------------------------------------------
# 可选依赖：AES-128-ECB（只有发图片需要）
# ---------------------------------------------------------------------------
def _load_crypto():
    """加载 cryptography；优先 venv，缺了再试系统 dist-packages（同版本解释器可直接用）。"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        return Cipher, algorithms, modes, PKCS7
    except ImportError:
        pass
    import sys
    import sysconfig
    for extra in ("/usr/lib/python3/dist-packages", "/usr/local/lib/python3/dist-packages"):
        if extra not in sys.path and Path(extra).is_dir():
            sys.path.append(extra)
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        return Cipher, algorithms, modes, PKCS7
    except ImportError:
        return None


_crypto = _load_crypto()


def crypto_available() -> bool:
    """是否支持发图片/文件（需要 AES-128-ECB）。"""
    return _crypto is not None


def crypto_hint() -> str:
    return ("缺少 cryptography，无法加密上传媒体（二维码只能发文本链接）。"
            "安装：pip install cryptography")


def _aes_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    if _crypto is None:
        raise ILinkError(0, crypto_hint(), "aes")
    Cipher, algorithms, modes, PKCS7 = _crypto
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(padded) + enc.finalize()


def aes_ecb_padded_size(plaintext_size: int) -> int:
    """PKCS7 填充后的密文长度（一定比原文多 1–16 字节）。"""
    return ((plaintext_size // 16) + 1) * 16


# ---------------------------------------------------------------------------
# HTTP 原语
# ---------------------------------------------------------------------------
def _random_wechat_uin() -> str:
    return base64.b64encode(str(secrets.randbits(32)).encode("utf-8")).decode("ascii")


def _common_headers() -> dict[str, str]:
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_CLIENT_VERSION),
    }


def _request(url: str, *, data: Optional[bytes] = None, headers: dict = None,
             timeout: float = API_TIMEOUT, method: str = None) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return e.code, body, dict(e.headers or {})


def _post(opts: "ClientOptions", endpoint: str, payload: dict,
          timeout: float = API_TIMEOUT) -> dict:
    body = json.dumps(
        {**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
        **_common_headers(),
    }
    if opts.token:
        headers["Authorization"] = f"Bearer {opts.token}"
    url = opts.url(endpoint)
    try:
        status, raw, _ = _request(url, data=body, headers=headers, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ILinkHTTPError(f"POST {endpoint} 网络错误: {type(e).__name__}: {e}") from e
    if status != 200:
        raise ILinkHTTPError(f"POST {endpoint} HTTP {status}: {raw[:200]!r}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ILinkHTTPError(f"POST {endpoint} 响应不是 JSON: {raw[:200]!r}") from e
    errcode = data.get("errcode", data.get("ret", 0)) or 0
    if errcode not in (0, 200):
        raise ILinkError(int(errcode), str(data.get("errmsg", "")), endpoint)
    return data


def _get(opts: "ClientOptions", endpoint: str, params: dict = None,
         timeout: float = API_TIMEOUT, base_url: str = None) -> dict:
    url = (base_url or opts.base_url).rstrip("/") + "/" + endpoint.lstrip("/")
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        status, raw, _ = _request(url, headers=_common_headers(), timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ILinkHTTPError(f"GET {endpoint} 网络错误: {type(e).__name__}: {e}") from e
    if status != 200:
        raise ILinkHTTPError(f"GET {endpoint} HTTP {status}: {raw[:200]!r}")
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise ILinkHTTPError(f"GET {endpoint} 响应不是 JSON: {raw[:200]!r}") from e


class ClientOptions:
    """一次会话的连接参数。"""

    __slots__ = ("base_url", "token", "cdn_base_url")

    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str = "",
                 cdn_base_url: str = None):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = token or ""
        self.cdn_base_url = (cdn_base_url or DEFAULT_CDN_BASE_URL).rstrip("/")

    def url(self, endpoint: str) -> str:
        return f"{self.base_url}/{endpoint.lstrip('/')}"


# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------
class Credentials:
    """扫码登录得到的凭据，可直接 JSON 落盘。"""

    __slots__ = ("bot_token", "account_id", "base_url", "user_id", "saved_at",
                 "cdn_base_url")

    def __init__(self, bot_token: str, account_id: str, base_url: str = DEFAULT_BASE_URL,
                 user_id: str = "", cdn_base_url: str = "", saved_at: float = 0.0):
        self.bot_token = bot_token
        self.account_id = account_id
        self.base_url = base_url or DEFAULT_BASE_URL
        self.user_id = user_id or ""
        self.cdn_base_url = cdn_base_url or ""
        self.saved_at = saved_at or time.time()

    def to_dict(self) -> dict:
        return {
            "bot_token": self.bot_token,
            "account_id": self.account_id,
            "base_url": self.base_url,
            "user_id": self.user_id,
            "cdn_base_url": self.cdn_base_url,
            "saved_at": self.saved_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Credentials":
        return cls(
            bot_token=data.get("bot_token", ""),
            account_id=data.get("account_id", ""),
            base_url=data.get("base_url", DEFAULT_BASE_URL),
            user_id=data.get("user_id", ""),
            cdn_base_url=data.get("cdn_base_url", ""),
            saved_at=float(data.get("saved_at") or 0.0),
        )

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(p)
        p.chmod(0o600)                 # token 等同密码
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Credentials":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    def options(self, cdn_base_url: str = None) -> ClientOptions:
        return ClientOptions(base_url=self.base_url, token=self.bot_token,
                             cdn_base_url=cdn_base_url or self.cdn_base_url or None)


# ---------------------------------------------------------------------------
# 长轮询 / 发送
# ---------------------------------------------------------------------------
class ILinkClient:
    """iLink Bot 的最小可用客户端：收游标、发文本、发图片。"""

    def __init__(self, opts: ClientOptions):
        self.opts = opts
        self.cursor: str = ""

    # ---- 入站（只为拿 context_token / 看 bot 是否在线） ----
    def poll(self) -> dict:
        try:
            resp = _post(self.opts, "ilink/bot/getupdates",
                         {"get_updates_buf": self.cursor or ""},
                         timeout=LONG_POLL_TIMEOUT)
        except ILinkHTTPError as e:
            # 长轮询超时是常态（服务端 hold 35s），别当故障
            if "timed out" in str(e).lower() or "timeout" in str(e).lower():
                return {"msgs": [], "get_updates_buf": self.cursor}
            raise
        if resp.get("get_updates_buf"):
            self.cursor = resp["get_updates_buf"]
        return resp

    @staticmethod
    def extract_text(msg: dict) -> str:
        for item in msg.get("item_list") or []:
            t = item.get("type")
            if t == ITEM_TEXT:
                text = (item.get("text_item") or {}).get("text")
                if text:
                    return text
            elif t == ITEM_VOICE:
                text = (item.get("voice_item") or {}).get("text")
                if text:
                    return text
        return ""

    # ---- 出站 ----
    @staticmethod
    def _client_id() -> str:
        return f"ncqq-{int(time.time() * 1000)}-{secrets.token_hex(6)}"

    def _build_msg(self, to_user_id: str, context_token: str,
                   items: list[dict]) -> dict:
        return {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": self._client_id(),
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "context_token": context_token or "",
            "item_list": items,
        }

    def send_message(self, to_user_id: str, context_token: str,
                     items: list[dict]) -> dict:
        return _post(self.opts, "ilink/bot/sendmessage",
                     {"msg": self._build_msg(to_user_id, context_token, items)})

    def send_text(self, to_user_id: str, text: str, context_token: str = "",
                  max_length: int = 3500) -> int:
        """发纯文本（超长自动分片）。返回发送条数。"""
        text = text or ""
        chunks = ([text] if len(text) <= max_length
                  else [text[i:i + max_length] for i in range(0, len(text), max_length)])
        for chunk in chunks:
            self.send_message(to_user_id, context_token,
                              [{"type": ITEM_TEXT, "text_item": {"text": chunk}}])
        return len(chunks)

    # ---- 媒体上传（图片/文件） ----
    def _upload(self, data: bytes, to_user_id: str, media_type: int) -> dict:
        """上传并加密到微信 CDN，返回可直接塞进 item 的 media 字段。"""
        import hashlib
        rawsize = len(data)
        filesize = aes_ecb_padded_size(rawsize)
        filekey = secrets.token_hex(16)
        aeskey = secrets.token_bytes(16)
        resp = _post(self.opts, "ilink/bot/getuploadurl", {
            "filekey": filekey,
            "media_type": int(media_type),
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": hashlib.md5(data).hexdigest(),
            "filesize": filesize,
            "no_need_thumb": True,
            "aeskey": aeskey.hex(),
        })
        upload_url = (resp.get("upload_full_url") or "").strip()
        if not upload_url and resp.get("upload_param"):
            upload_url = (f"{self.opts.cdn_base_url}/upload"
                          f"?encrypted_query_param="
                          f"{urllib.parse.quote(resp['upload_param'], safe='')}"
                          f"&filekey={urllib.parse.quote(filekey, safe='')}")
        if not upload_url:
            raise ILinkError(0, "getuploadurl 未返回上传地址", "ilink/bot/getuploadurl")

        ciphertext = _aes_ecb_encrypt(data, aeskey)
        status, raw, headers = _request(
            upload_url, data=ciphertext, method="POST",
            headers={"Content-Type": "application/octet-stream"}, timeout=60.0)
        if status != 200:
            raise ILinkHTTPError(
                f"CDN 上传失败 HTTP {status}: "
                f"{headers.get('x-error-message') or raw[:200]!r}")
        dl = headers.get("x-encrypted-param") or headers.get("X-Encrypted-Param")
        if not dl:
            raise ILinkHTTPError("CDN 上传响应缺少 x-encrypted-param")
        return {
            "encrypt_query_param": dl,
            # 官方约定：media.aes_key = base64(aeskey 的 hex 字符串)
            "aes_key": base64.b64encode(aeskey.hex().encode("ascii")).decode("ascii"),
            "encrypt_type": 1,
        }, filesize

    def send_image(self, to_user_id: str, image_path: str | Path,
                   context_token: str = "", caption: str = "") -> None:
        """发图片（AES-128-ECB 加密后走 CDN）。"""
        data = Path(image_path).read_bytes()
        media, cipher_size = self._upload(data, to_user_id, UPLOAD_IMAGE)
        if caption:
            self.send_text(to_user_id, caption, context_token)
        self.send_message(to_user_id, context_token, [{
            "type": ITEM_IMAGE,
            "image_item": {"media": media, "mid_size": cipher_size},
        }])

    # ---- 会话检测 ----
    def check_alive(self, probe_user_id: str = "probe@im.bot") -> bool:
        """探测登录态是否还有效（errcode=-14 表示要重新扫码）。"""
        try:
            _post(self.opts, "ilink/bot/getconfig",
                  {"ilink_user_id": probe_user_id, "context_token": ""},
                  timeout=10.0)
            return True
        except ILinkError as e:
            if e.session_expired:
                return False
            return True                 # 其它业务错误说明鉴权是通的
        except ILinkHTTPError:
            return True                 # 网络问题不算掉登录
