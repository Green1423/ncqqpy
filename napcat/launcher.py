"""
服务器拉起（launcher）— WebSocket 连不上时，把 NapCatQQ/QQ 服务端拉起来。

设计约束（都是实际踩出来的）：

1. **只在「连续失败」的第一次触发**。调用方 `WsHandler` 只在 `failures == 1`
   时回调；之后连续失败说明上一次拉起没成功，重复触发没有意义（还会刷屏 + 抢锁）。
2. **在后台 task 里执行**。拉起命令往往要等几秒到几十秒，绝不能阻塞 WebSocket
   重连循环 —— 不然连上了也没人收消息。
3. **不吞异常**。失败写日志 + 记状态，插件据此可以再发一条「拉起失败」的通知。
4. **执行结果 ≠ 拉起成功**。`systemctl restart` 返回后服务可能还在启动中，
   「是否真的起来了」由 WebSocket 重连成功来判定，不在这里猜。

两种启动方式（`cfg.restart_mode` / 构造参数 `restart_mode`）：

- `systemd`（默认）：`systemctl restart <unit>`。NapCatQQ 已经由 systemd 托管
  （`/etc/systemd/system/ncqq.service` → `bash /opt/ncqq/launcher.sh`），
  重启服务单元是唯一不会和 systemd 抢同一个进程的方式。
- `launcher`：把 `restart_command` 当命令行按 `shlex` 切分后直接 exec
  （默认 `bash /opt/ncqq/launcher.sh`）。**注意**：launcher.sh 是阻塞式前台
  进程，只有在该脚本自己会先清理旧实例时才适合用这个模式，否则会和 systemd
  托管的实例并存（两个 QQ）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Optional

DEFAULT_RESTART_COMMAND = "systemctl restart ncqq"
DEFAULT_RESTART_MODE = "systemd"

# 两次真实拉起之间的最小间隔（秒）：防止「失败 → 拉起 → 又失败」的循环把服务端
# 反复重启。默认 3 分钟 —— 拉起本身+QQ 启动要几十秒，太快的重复触发是纯抖动，
# 而且每次重启都要重新扫码，间隔太短只会刷屏。
DEFAULT_RESTART_COOLDOWN = 180.0

# 启动前清空的目录：NapCat 的 cache（登录二维码就在里面）。
# 清掉之后「能读到的 qrcode.png」必然是本次启动刚生成的 —— 上层不需要再判断新旧。
DEFAULT_CLEAR_CACHE_DIR = "/opt/ncqq/napcat/cache"

MODE_SYSTEMD = "systemd"
MODE_LAUNCHER = "launcher"
_MODES = (MODE_SYSTEMD, MODE_LAUNCHER)


class LaunchResult:
    """一次拉起尝试的结果。"""

    __slots__ = ("started", "reason", "returncode", "stdout", "stderr",
                 "duration", "command", "hint")

    def __init__(self, started: bool, reason: str = "", returncode: Optional[int] = None,
                 stdout: str = "", stderr: str = "", duration: float = 0.0,
                 command: str = "", hint: str = ""):
        self.started = started          # 命令是否成功执行（不代表服务已就绪）
        self.reason = reason            # 未执行/失败的原因（人类可读）
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.duration = duration
        self.command = command
        self.hint = hint                # 可操作的建议（权限类失败时给出）

    def __repr__(self) -> str:
        return (f"LaunchResult(started={self.started}, rc={self.returncode}, "
                f"reason={self.reason!r}, {self.duration:.2f}s)")


# 权限类失败的典型特征（systemctl / polkit / sudo 都在这里）
_PERMISSION_MARKERS = (
    "access denied", "interactive authentication",
    "permission denied", "not permitted", "authentication is required",
    "a password is required", "must be root", "operation not permitted",
)


def _permission_hint(stderr: str, stdout: str, argv: list) -> str:
    """认出「权限不足」并给出具体该怎么办（这类失败最容易卡住人）。"""
    blob = f"{stderr}\n{stdout}".lower()
    if not any(marker in blob for marker in _PERMISSION_MARKERS):
        return ""
    unit = ""
    if argv[:1] == ["systemctl"] and len(argv) >= 3:
        unit = argv[2]
    if not unit:
        # launcher 模式下命令行里没有服务名，从 systemctl 的报错里捞
        # （"Failed to restart ncqq.service: Access denied ..."）
        m = re.search(r"restart\s+([\w@.\-]+\.service)", f"{stderr}\n{stdout}")
        if m:
            unit = m.group(1)[: -len(".service")]
    unit = unit or "ncqq"
    return (
        f"权限不足：拿不到重启 {unit} 的授权。"
        f"跑 ncqqpy 的账号（{_current_user()}）默认无权操作 root 的 systemd 服务，"
        f"需要 root 执行一次以下任一方案：\n"
        f"  A) polkit（推荐）：写入 /etc/polkit-1/rules.d/49-ncqq-watchdog.rules，"
        f"内容见 docs/ncqq-watchdog.md\n"
        f"  B) sudoers：echo '{_current_user()} ALL=(root) NOPASSWD: "
        f"/usr/bin/systemctl restart {unit}' | "
        f"sudo tee /etc/sudoers.d/ncqq-watchdog，"
        f"并把 config.json 改为 "
        f"restart_mode=launcher / restart_command=\"sudo -n systemctl restart {unit}\""
    )


def _current_user() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return "ncqqpy 运行账号"


class ServerLauncher:
    """按配置拉起 NapCatQQ 服务端。

    用法：
        launcher = ServerLauncher(cfg=client.cfg, logger=client.logger)
        launcher.apply_config(restart_command=..., restart_mode=..., ...)
        result = await launcher.launch(reason="WebSocket 连接失败")
    """

    def __init__(self, *, command: str = DEFAULT_RESTART_COMMAND,
                 mode: str = DEFAULT_RESTART_MODE, enabled: bool = True,
                 timeout: float = 60.0, cooldown: float = DEFAULT_RESTART_COOLDOWN,
                 clear_cache_dir: str = DEFAULT_CLEAR_CACHE_DIR,
                 logger: logging.Logger = None):
        self.logger = logger or logging.getLogger("napcat.launcher")
        self.command = command or DEFAULT_RESTART_COMMAND
        self.clear_cache_dir = (clear_cache_dir or "").strip()
        self.mode = mode if mode in _MODES else DEFAULT_RESTART_MODE
        self.enabled = bool(enabled)
        self.timeout = max(1.0, float(timeout))
        self.cooldown = max(0.0, float(cooldown))
        # 运行状态（供插件读取）
        self.attempts = 0
        self.last_result: Optional[LaunchResult] = None
        self.last_attempt_at: float = 0.0
        self.running = False            # 是否有一次拉起正在执行
        self._lock = asyncio.Lock()
        # 「刚发生过一次拉起」的信号：外部（如看门狗插件）需要据此做事后处理，
        # 比如重启 QQ 后必须重新扫码 —— 每次拉起都要把新二维码递出去。
        self._event = asyncio.Event()
        self._waited_upto = 0            # 已被 wait_launch 取走的拉起序号

    # ---- 配置 ----

    def apply_config(self, *, command=None, mode=None, enabled=None,
                     timeout=None, cooldown=None, clear_cache_dir=None):
        """用配置值覆盖（None = 不改），返回 self 便于链式调用。"""
        if command is not None:
            self.command = str(command) or DEFAULT_RESTART_COMMAND
        if mode is not None:
            self.mode = mode if mode in _MODES else DEFAULT_RESTART_MODE
        if enabled is not None:
            self.enabled = bool(enabled)
        if timeout is not None:
            self.timeout = max(1.0, float(timeout))
        if cooldown is not None:
            self.cooldown = max(0.0, float(cooldown))
        if clear_cache_dir is not None:
            self.clear_cache_dir = str(clear_cache_dir).strip()
        return self

    @classmethod
    def from_config(cls, cfg, logger: logging.Logger = None) -> "ServerLauncher":
        """从 ConfigManager 读配置构造（键都在 config.json 顶层）。"""
        get = getattr(cfg, "get", lambda k, d=None: d)
        return cls(
            command=get("restart_command", DEFAULT_RESTART_COMMAND),
            mode=get("restart_mode", DEFAULT_RESTART_MODE),
            enabled=get("restart_enabled", True),
            timeout=get("restart_timeout", 60.0),
            cooldown=get("restart_cooldown", DEFAULT_RESTART_COOLDOWN),
            clear_cache_dir=get("restart_clear_cache_dir", DEFAULT_CLEAR_CACHE_DIR),
            logger=logger,
        )

    # ---- 执行 ----

    # ---- 启动前清 cache ----

    def _refuse_reason(self) -> str:
        """拒绝清理的路径（防手滑把重要目录删了）。返回空串表示允许。"""
        raw = (self.clear_cache_dir or "").strip()
        if not raw:
            return "未配置清理目录"
        path = Path(raw)
        try:
            resolved = path.resolve()
        except OSError:
            return f"路径无法解析: {raw}"
        # 必须是绝对路径、至少两层、且不是根/家目录/常见系统目录
        if not path.is_absolute():
            return f"不是绝对路径: {raw}"
        if len(resolved.parts) < 3:                 # 至少 /a/b
            return f"路径过浅（拒绝删除）: {resolved}"
        if resolved == Path.home():
            return f"拒绝删除家目录: {resolved}"
        if str(resolved) in ("/", "/etc", "/usr", "/var", "/opt", "/home", "/root"):
            return f"拒绝删除系统目录: {resolved}"
        return ""

    def clear_cache(self, reason: str = "") -> bool:
        """清空 `clear_cache_dir`（登录二维码就在里面）。

        为什么放在这里：清掉之后「能读到的 qrcode.png」必然是本次启动刚生成的，
        上层就不用再判断新旧了。必须在拉起**之前**执行。

        需要 root（`/opt/ncqq` 属 root）。清不掉时不阻塞拉起 —— 记一条 warning，
        上层会通过「码比拉起还老」的告警暴露出来。
        """
        refuse = self._refuse_reason()
        if refuse:
            self.logger.info(f"跳过清理 cache（{refuse}）")
            return False
        target = Path(self.clear_cache_dir)
        if not target.exists():
            self.logger.info(f"cache 目录不存在，无需清理: {target}")
            return True
        started = time.monotonic()
        try:
            shutil.rmtree(target)
        except Exception as e:
            self.logger.warning(
                f"清理 cache 失败（{type(e).__name__}: {e}）: {target}"
                f"{'（' + reason + '）' if reason else ''}"
                f" —— 拉起继续，但二维码可能是上一轮遗留的")
            return False
        self.logger.info(f"已清空 cache: {target}"
                         f"（{time.monotonic() - started:.2f}s）")
        return True

    @property
    def argv(self) -> list[str]:
        """实际执行的命令（列表形式，shell=False，不做字符串拼接）。

        `systemd` 模式下这里要的只是**服务名**，但很容易被写成完整命令
        （`"systemctl restart ncqq"`）—— 那样会拼成
        `systemctl restart 'systemctl restart ncqq'`，报「Unit not found」。
        所以这里做一次容错归一化。
        """
        if self.mode == MODE_SYSTEMD:
            unit = self.command.strip() or "ncqq"
            parts = shlex.split(unit)
            if parts[:1] == ["systemctl"]:
                # 已经是完整命令 → 剥掉 systemctl [--user] <verb...>
                rest = [p for p in parts[1:] if not p.startswith("-")]
                if rest and rest[0] in ("restart", "start", "stop", "reload"):
                    rest = rest[1:]
                unit = rest[-1] if rest else "ncqq"
            if unit.endswith(".service"):
                unit = unit[: -len(".service")]
            return ["systemctl", "restart", unit]
        return shlex.split(self.command)

    def _cooldown_remaining(self) -> float:
        if not self.last_attempt_at or self.cooldown <= 0:
            return 0.0
        elapsed = time.monotonic() - self.last_attempt_at
        return max(0.0, self.cooldown - elapsed)

    async def launch(self, reason: str = "", force: bool = False) -> LaunchResult:
        """执行一次拉起。并发调用会被串行化；冷却期内的重复调用直接跳过。"""
        if not self.enabled:
            return LaunchResult(False, reason="拉起已关闭（restart_enabled=false）")

        async with self._lock:
            remaining = self._cooldown_remaining()
            if remaining > 0 and not force:
                self.logger.info(
                    f"跳过拉起（冷却中，还需 {remaining:.0f}s）：{reason}")
                return LaunchResult(False, reason=f"冷却中（{remaining:.0f}s）")

            argv = self.argv
            if not argv:
                return LaunchResult(False, reason="启动命令为空")
            command = " ".join(shlex.quote(a) for a in argv)

            # 先清 cache 再拉起：二维码在 cache 里，清掉即保证「重启后出现的码是新的」
            self.clear_cache(reason=reason)

            self.attempts += 1
            self.last_attempt_at = time.monotonic()
            self.running = True
            started = time.monotonic()
            self.logger.warning(f"正在拉起服务器（第 {self.attempts} 次）: {command}"
                                + (f" —— {reason}" if reason else ""))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    out, err = await asyncio.wait_for(proc.communicate(), self.timeout)
                except asyncio.TimeoutError:
                    with contextlib.suppress(Exception):
                        proc.kill()
                    with contextlib.suppress(Exception):
                        await proc.wait()
                    duration = time.monotonic() - started
                    result = LaunchResult(
                        False, reason=f"启动命令超时（{self.timeout:.0f}s）",
                        duration=duration, command=command)
                    self.logger.error(f"拉起超时: {command}")
                    self.last_result = result
                    return result
                duration = time.monotonic() - started
                stdout = (out or b"").decode("utf-8", "replace").strip()
                stderr = (err or b"").decode("utf-8", "replace").strip()
                if proc.returncode == 0:
                    result = LaunchResult(True, returncode=0, stdout=stdout,
                                          stderr=stderr, duration=duration,
                                          command=command)
                    self.logger.warning(
                        f"拉起命令执行完成（{duration:.1f}s），等待 WebSocket 重连确认")
                else:
                    detail = stderr or stdout or "无输出"
                    hint = _permission_hint(stderr, stdout, argv)
                    result = LaunchResult(
                        False, reason=f"启动命令退出码 {proc.returncode}: {detail}",
                        returncode=proc.returncode, stdout=stdout, stderr=stderr,
                        duration=duration, command=command, hint=hint)
                    self.logger.error(
                        f"拉起失败（退出码 {proc.returncode}）: {detail}")
                    if hint:
                        self.logger.error(f"拉起失败原因（权限）: {hint}")
                self.last_result = result
                return result
            except FileNotFoundError as e:
                result = LaunchResult(
                    False, reason=f"找不到命令 {argv[0]!r}: {e}",
                    duration=time.monotonic() - started, command=command)
                self.logger.error(f"拉起失败: {result.reason}")
                self.last_result = result
                return result
            except Exception as e:
                result = LaunchResult(
                    False, reason=f"{type(e).__name__}: {e}",
                    duration=time.monotonic() - started, command=command)
                self.logger.error(f"拉起失败: {result.reason}")
                self.last_result = result
                return result
            finally:
                self.running = False
                # 无论成功失败都发信号：失败时往往也需要人工扫码介入。
                # 注意这里只 set，不更新「已取走」水位 —— 那是消费者的事，
                # 生产者顺手写掉会让信号在 set 之后立刻「看起来已被消费」。
                self._event.set()

    async def wait_launch(self, timeout: float = None) -> Optional[int]:
        """等下一次拉起发生，返回**拉起序号**（从 1 开始）；超时返回 None。

        用序号而不是布尔量：调用方据此知道「这是第几次拉起」，也能防止
        把信号之前就已经处理过的拉起重复算一遍。
        """
        if self.attempts > self._waited_upto:
            self._waited_upto = self.attempts
            return self.attempts
        self._event.clear()
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        if self.attempts <= self._waited_upto:
            return None
        self._waited_upto = self.attempts
        return self.attempts

    def status(self) -> dict:
        """运行状态快照（给 /watchdog status 用）。"""
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "command": self.command,
            "argv": self.argv,
            "clear_cache_dir": self.clear_cache_dir,
            "attempts": self.attempts,
            "running": self.running,
            "last_attempt_at": self.last_attempt_at,
            "last_result": (None if self.last_result is None else {
                "started": self.last_result.started,
                "reason": self.last_result.reason,
                "returncode": self.last_result.returncode,
                "duration": round(self.last_result.duration, 2),
                "stderr": self.last_result.stderr[:400],
                "hint": self.last_result.hint,
            }),
        }
