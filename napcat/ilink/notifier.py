"""
微信（iLink）通知器 —— 把「服务器掉线 / 拉起完成 / QQ 二维码」发到微信。

为什么单独一层：QQ 掉线时 **QQ 这条通道本身已经不可用**，通知必须走另一条链路，
而且这条链路失败也不能反过来影响机器人主流程。所以这里所有方法都**不抛异常**，
只返回 bool 并记录 `last_error`，由插件决定是否只写日志。

iLink 的一个硬约束：`sendmessage` 必须带 `context_token`，而这个 token 只在
**入站消息**里出现。所以：
- 插件后台常驻长轮询，把最近一次收到的 `context_token` 落盘缓存；
- 主动推送时优先用显式 token，其次用缓存；
- 都没有（用户从没给微信 bot 发过消息）→ 返回 False，日志里给出操作指引。

用法：
    n = Notifier(logger=..., data_dir=..., config=self.config)
    await n.start()                  # 后台线程起长轮询
    ok = await n.send_text("……")
    ok = await n.send_image("/path/qrcode.png", caption="…")
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Optional

from . import (
    Credentials,
    ILinkClient,
    ILinkError,
    ILinkHTTPError,
    crypto_available,
    crypto_hint,
)

CRED_FILE = "ilink_credentials.json"
CTX_FILE = "ilink_context_tokens.json"


class Notifier:
    """iLink 微信通知通道。"""

    def __init__(self, *, logger, data_dir: str | Path, config: dict = None):
        self.logger = logger
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config: dict = dict(config or {})

        self.credentials: Optional[Credentials] = None
        self.client: Optional[ILinkClient] = None
        self.enabled: bool = True
        self.target: str = ""                 # 收通知的微信用户 id
        self.auto_context: bool = True        # 是否后台长轮询缓存 context_token

        self.last_error: str = ""
        self.last_send_at: float = 0.0
        self.sends_ok: int = 0
        self.sends_failed: int = 0
        self._ctx_cache: dict[str, str] = {}

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # 配置 / 载入
    # ------------------------------------------------------------------
    def apply_config(self, wechat_cfg: dict = None) -> "Notifier":
        """从 `plugins.ncqq_watchdog.wechat` 读设置（缺省全用默认值）。"""
        if wechat_cfg is not None:
            self.config = dict(wechat_cfg or {})

        def get(key, default=None):
            value = self.config.get(key, default)
            return default if value is None else value

        self.enabled = bool(get("enabled", True))
        self.target = str(get("target", "") or "")
        self.auto_context = bool(get("auto_context", True))
        cred_path = get("credentials_file", "") or str(self.data_dir / CRED_FILE)
        self._cred_path = Path(cred_path)
        self._ctx_path = Path(get("context_file", "") or str(self.data_dir / CTX_FILE))
        self._load_credentials()
        self._load_contexts()
        return self

    def _load_credentials(self):
        try:
            if self._cred_path.exists():
                self.credentials = Credentials.load(self._cred_path)
                self.client = ILinkClient(self.credentials.options())
                self.logger.info(
                    f"[watchdog] 已载入微信 iLink 凭据（account={self.credentials.account_id}）")
            else:
                self.logger.warning(
                    f"[watchdog] 未找到微信 iLink 凭据（{self._cred_path}）："
                    f"微信通知不可用，运行 `python -m plugins.ncqq_watchdog.login` 扫码登录")
        except Exception as e:
            self.logger.error(f"[watchdog] 载入微信凭据失败: {type(e).__name__}: {e}")

    def _load_contexts(self):
        try:
            if self._ctx_path.exists():
                self._ctx_cache = json.loads(self._ctx_path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            self.logger.warning(f"[watchdog] 读取 context_token 缓存失败: {e}")
            self._ctx_cache = {}

    def _save_contexts(self):
        try:
            tmp = self._ctx_path.with_suffix(self._ctx_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._ctx_cache, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self._ctx_path)
        except Exception as e:
            self.logger.warning(f"[watchdog] 写入 context_token 缓存失败: {e}")

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.credentials and self.enabled)

    @property
    def has_context(self) -> bool:
        return bool(self.target and self._ctx_cache.get(self.target))

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "target": self.target,
            "account": self.credentials.account_id if self.credentials else "",
            "has_context": self.has_context,
            "crypto": crypto_available(),
            "listener": bool(self._thread and self._thread.is_alive()),
            "sends_ok": self.sends_ok,
            "sends_failed": self.sends_failed,
            "last_send_at": self.last_send_at,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------
    # context_token 缓存
    # ------------------------------------------------------------------
    def remember_context(self, user_id: str, token: str):
        if user_id and token:
            self._ctx_cache[user_id] = token
            self._save_contexts()

    def context_for(self, user_id: str) -> str:
        return self._ctx_cache.get(user_id, "")

    # ------------------------------------------------------------------
    # 后台长轮询（只为拿 context_token）
    # ------------------------------------------------------------------
    def start(self):
        """后台线程跑长轮询；没有凭据或关闭 auto_context 时什么都不做。"""
        if not self.configured:
            return False
        if not self.auto_context:
            self.logger.info("[watchdog] auto_context=false，不启动微信长轮询")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="ilink-poll",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None

    def _poll_loop(self):
        """长轮询：收消息 → 缓存 context_token（顺带记录用户 id 方便配置 target）。"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        backoff = 2.0
        while not self._stop.is_set():
            try:
                resp = self.client.poll()
                backoff = 2.0
                for msg in resp.get("msgs") or []:
                    if msg.get("message_type") not in (1, None):
                        continue              # 只关心用户发来的消息
                    from_user = msg.get("from_user_id") or ""
                    token = msg.get("context_token") or ""
                    if from_user and token:
                        if self._ctx_cache.get(from_user) != token:
                            self.logger.info(
                                f"[watchdog] 收到微信消息（{from_user}），已缓存 "
                                f"context_token"
                                + ("" if self.target == from_user
                                   else f"；如需收通知请在配置里把 target 设为 {from_user}"))
                        self.remember_context(from_user, token)
                    text = ILinkClient.extract_text(msg)
                    if text:
                        self.logger.info(f"[watchdog] 微信消息 [{from_user}]: {text[:120]}")
            except ILinkError as e:
                if e.session_expired:
                    self.last_error = "微信登录态过期（errcode=-14），需要重新扫码登录"
                    self.logger.error(f"[watchdog] {self.last_error}")
                    self._stop.wait(3600)
                else:
                    self.last_error = str(e)
                    self.logger.warning(f"[watchdog] 微信长轮询业务错误: {e}")
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 60.0)
            except ILinkHTTPError as e:
                self.last_error = str(e)
                self.logger.warning(f"[watchdog] 微信长轮询网络错误: {e}")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self.logger.warning(f"[watchdog] 微信长轮询异常: {self.last_error}")
                self._stop.wait(backoff)
        self.logger.info("[watchdog] 微信长轮询已退出")

    # ------------------------------------------------------------------
    # 发送（全部不抛异常）
    # ------------------------------------------------------------------
    def _guard(self) -> str:
        """发送前检查，返回错误说明（空字符串 = 可以发）。

        这些是**配置类**问题，重试没有意义（`_retryable()` 据此判断）。
        """
        if not self.enabled:
            return "微信通知已关闭（enabled=false）"
        if not self.credentials:
            return ("未配置微信 iLink 凭据"
                    "（先跑 python -m napcat.ilink_login 扫码）")
        if not (self.config.get("target") or self.target):
            return "未配置 target（收通知的微信用户 id）"
        return ""

    def _retryable(self) -> bool:
        """当前失败是否值得重试（配置没配好就别空转）。"""
        return self._guard() == ""

    @staticmethod
    def _cooldown_hint(exc: Exception) -> float:
        """服务端限流时给出退避时长（识别不出来就返回 0）。"""
        text = str(exc).lower()
        for marker in ("frequency", "too fast", "rate limit", "frequent",
                       "频繁", "限流", "稍后"):
            if marker in text:
                return 30.0
        return 0.0

    async def send_text_retry(self, text: str, *, target: str = "",
                              timeout: float = 300.0,
                              interval: float = 5.0) -> bool:
        """带重试的发送：**失败就每 `interval` 秒重试，直到成功或超时**。

        只有传输类失败才重试；配置类问题（没凭据/没 target）立即返回，
        否则会白等满 timeout。
        """
        import time as _t
        deadline = _t.monotonic() + max(0.0, timeout)
        attempt = 0
        while True:
            attempt += 1
            if await self.send_text(text, target=target):
                if attempt > 1:
                    self.logger.info(f"[watchdog] 微信通知第 {attempt} 次尝试成功")
                return True
            if not self._retryable():
                return False
            if _t.monotonic() >= deadline:
                self.logger.error(
                    f"[watchdog] 微信通知重试 {attempt} 次仍未成功，放弃")
                return False
            self.logger.warning(
                f"[watchdog] 微信通知发送失败（第 {attempt} 次），"
                f"{interval:.0f}s 后重试：{self.last_error}")
            await asyncio.sleep(interval)

    async def send_image_retry(self, image_path, *, caption: str = "",
                               target: str = "", timeout: float = 300.0,
                               interval: float = 5.0) -> bool:
        """带重试的图片发送（同上）。"""
        import time as _t
        deadline = _t.monotonic() + max(0.0, timeout)
        attempt = 0
        while True:
            attempt += 1
            if await self.send_image(image_path, caption=caption, target=target):
                if attempt > 1:
                    self.logger.info(f"[watchdog] 微信图片第 {attempt} 次尝试成功")
                return True
            if not self._retryable():
                return False
            if _t.monotonic() >= deadline:
                self.logger.error(
                    f"[watchdog] 微信图片重试 {attempt} 次仍未成功，放弃")
                return False
            self.logger.warning(
                f"[watchdog] 微信图片发送失败（第 {attempt} 次），"
                f"{interval:.0f}s 后重试：{self.last_error}")
            await asyncio.sleep(interval)

    async def _run(self, fn, *args, **kwargs) -> bool:
        """把阻塞的 HTTP 调用丢到线程里，避免卡住事件循环。"""
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: fn(*args, **kwargs))

    async def send_text(self, text: str, *, target: str = "") -> bool:
        problem = self._guard()
        if problem:
            self.last_error = problem
            self.logger.warning(f"[watchdog] 微信通知未发送：{problem}")
            return False
        to = target or self.target
        token = self.context_for(to)
        if not token:
            self.last_error = (f"没有 {to} 的 context_token：请先用微信给 bot 发一条消息"
                               f"（token 只在入站消息里出现）")
            self.logger.warning(f"[watchdog] 微信通知未发送：{self.last_error}")
            return False
        try:
            await self._run(self.client.send_text, to, text, token)
            self._mark_ok()
            self.logger.info(f"[watchdog] 微信通知已发送（{len(text)} 字）")
            return True
        except Exception as e:
            self._mark_failed(e)
            return False

    async def send_image(self, image_path: str | Path, *, caption: str = "",
                         target: str = "") -> bool:
        problem = self._guard()
        if problem:
            self.last_error = problem
            self.logger.warning(f"[watchdog] 微信图片未发送：{problem}")
            return False
        if not crypto_available():
            self.last_error = crypto_hint()
            self.logger.warning(f"[watchdog] 微信图片未发送：{self.last_error}")
            return False
        path = Path(image_path)
        if not path.exists():
            self.last_error = f"图片不存在: {path}"
            self.logger.warning(f"[watchdog] 微信图片未发送：{self.last_error}")
            return False
        to = target or self.target
        token = self.context_for(to)
        if not token:
            self.last_error = (f"没有 {to} 的 context_token：请先用微信给 bot 发一条消息")
            self.logger.warning(f"[watchdog] 微信图片未发送：{self.last_error}")
            return False
        try:
            await self._run(self.client.send_image, to, str(path), token, caption)
            self._mark_ok()
            self.logger.info(f"[watchdog] 微信图片已发送: {path.name}")
            return True
        except Exception as e:
            self._mark_failed(e)
            return False

    def _mark_ok(self):
        self.sends_ok += 1
        self.last_send_at = time.time()
        self.last_error = ""

    def _mark_failed(self, exc: Exception):
        self.sends_failed += 1
        if isinstance(exc, ILinkError) and exc.session_expired:
            self.last_error = "微信登录态过期（errcode=-14），需要重新扫码登录"
        else:
            self.last_error = f"{type(exc).__name__}: {exc}"
        self.logger.error(f"[watchdog] 微信通知发送失败：{self.last_error}")
