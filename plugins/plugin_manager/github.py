"""GitHub 侧的一切网络访问：插件列表文件、仓库 HEAD、源码包下载。

三件事**只用标准库 + aiohttp**，不引入 git 依赖（bot 机器上未必有 git，
而 GitHub 的 codeload tarball 已经把「克隆 + 取子目录」这件事做完了）：

1. `fetch_json`   —— 拉插件列表文件（`plugins.json`）或 GitHub API 的 JSON；
2. `resolve_head` —— 问「这个仓库现在最新提交是什么」（更新检查的唯一依据）；
3. `fetch_archive`—— 下载仓库源码包（tarball）到本地临时文件。

所有失败都抛 `GithubError`（带人话原因），由上层翻译成给超管的提示 ——
这里**不**自己拼用户文案，也不吞异常。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import aiohttp

from napcat.errors import PluginExternalError

DEFAULT_TIMEOUT = 20.0
UA = "ncqqpy-plugin-manager"

#: 仓库源码包地址（tag / 分支 / commit 都能填进 `{ref}`）
CODELOAD = "https://codeload.github.com/{repo}/tar.gz/{ref}"
#: GitHub REST：查某条 ref 的最新提交
API_COMMIT = "https://api.github.com/repos/{repo}/commits/{ref}"


class GithubError(PluginExternalError):
    """GitHub 侧失败（网络、404、限流、格式不对）。

    归到「上游错误」家族（`retryable=True` → 文案自带「可以稍后再试」），
    但 `kind` 换成更具体的分类名，用户一眼能看出是 GitHub 那边的问题。
    """

    kind = "GitHub 访问失败"


@dataclass
class HeadInfo:
    """某个仓库某条 ref 的 HEAD 快照。"""

    repo: str
    ref: str
    sha: str = ""
    date: str = ""
    #: 拿不到 SHA（API 限流等）时为真：此时**不能**断言「已是最新」
    unknown: bool = False
    note: str = ""


@dataclass
class ArchiveInfo:
    """下载到本地的源码包。"""

    path: Path
    repo: str
    ref: str
    #: 下载时实际生效的提交（响应头里带的，缺省为空）
    sha: str = ""


def _auth_headers(token: str = "") -> dict[str, str]:
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class GithubClient:
    """带重试与 ETag 缓存的极简 GitHub 客户端。

    只用得到三个接口，所以不引第三方 SDK；`session` 可注入（测试用假会话）。

    ETag 有两处用处：
    - **插件列表文件**：5 分钟缓存 + 条件请求，反复敲 `/plugin list` 不会把
      配额打光（匿名 GitHub API 每小时只有 60 次）；
    - **仓库 HEAD**：响应体积很小，省的是配额而不是带宽。
    """

    def __init__(self, *, token: str = "", timeout: float = DEFAULT_TIMEOUT,
                 proxy: str = "", session: Optional[aiohttp.ClientSession] = None,
                 logger=None, api_base: str = "https://api.github.com",
                 codeload: str = CODELOAD):
        self.token = (token or "").strip()
        self.timeout = max(3.0, float(timeout or DEFAULT_TIMEOUT))
        self.proxy = (proxy or "").strip() or None
        self.api_base = (api_base or "https://api.github.com").rstrip("/")
        self.codeload = codeload or CODELOAD
        self.logger = logger
        self._session: Optional[aiohttp.ClientSession] = session
        self._own_session = session is None
        #: url → (取到的时间, ETag, 解析后的 JSON)
        self._cache: dict[str, tuple[float, str, Any]] = {}
        #: 匿名配额耗尽后的提示只打一次，避免刷日志
        self._rate_limit_noted = False
        self._lock = asyncio.Lock()

    # ---- 会话管理 ----

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    headers={"User-Agent": UA},
                )
                self._own_session = True
            return self._session

    def clear_cache(self) -> None:
        """丢掉全部已缓存的响应（`/plugin refresh` 与测试用）。

        `fetch_json` 的 TTL 缓存是**客户端级**的，`Plugin.load_manifest` 的
        `refresh=True` 只绕过插件自己的内存缓存 —— 客户端那层不清掉，
        「强制刷新」在网络恢复前仍然拿旧数据。
        """
        self._cache.clear()

    async def close(self) -> None:
        """关闭自建会话（注入的会话不由我们关闭）。"""
        if self._session is not None and self._own_session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ---- 通用请求 ----

    async def _request(self, method: str, url: str, *, headers: dict | None = None,
                       retries: int = 2, accept: str = "") -> tuple[int, dict, bytes]:
        """发一次请求，返回 (状态码, 响应头, 响应体)。

        只对**网络层**失败（连接不上、超时）重试；HTTP 状态码原样返回给调用方，
        由它决定 404 / 403 的语义 —— 在这里重试 404 只会白等。
        """
        session = await self._get_session()
        hdrs = _auth_headers(self.token)
        if accept:
            hdrs["Accept"] = accept
        hdrs.update(headers or {})
        last_exc: Optional[BaseException] = None
        for attempt in range(retries + 1):
            try:
                async with session.request(method, url, headers=hdrs,
                                           proxy=self.proxy) as resp:
                    body = await resp.read()
                    return resp.status, dict(resp.headers), body
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络层的什么都可能
                last_exc = exc
                if attempt >= retries:
                    break
                await asyncio.sleep(0.8 * (2 ** attempt))
        raise GithubError(f"网络请求失败：{url}（{type(last_exc).__name__}: {last_exc}）",
                          cause=last_exc)

    async def fetch_json(self, url: str, *, cache_ttl: float = 0.0,
                         retries: int = 2) -> Any:
        """拉 JSON（TTL 缓存 + ETag 条件请求）。

        `cache_ttl > 0` 且缓存未过期时**不产生任何请求**；`cache_ttl <= 0`
        表示「一定要问服务器」，但**仍然带上 ETag** —— 没变时只回 304、
        不计入正文配额。两件事分开：TTL 管「要不要问」，ETag 管「问得多便宜」。
        """
        cached = self._cache.get(url)
        if cache_ttl > 0 and cached and (time.monotonic() - cached[0]) < cache_ttl:
            return cached[2]

        headers = {"If-None-Match": cached[1]} if cached and cached[1] else {}
        status, resp_headers, body = await self._request("GET", url, headers=headers,
                                                         retries=retries)
        if status == 304:
            if cached:
                self._cache[url] = (time.monotonic(), cached[1], cached[2])
                return cached[2]
            raise GithubError(f"缓存失效但服务器返回 304：{url}")
        self._raise_for_status(status, url, body, resp_headers)

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GithubError(f"返回的不是合法 JSON：{url}") from exc

        # 无论 TTL 是否为 0 都留一份：下一次请求要靠它的 ETag 做条件请求
        self._cache[url] = (time.monotonic(), resp_headers.get("ETag", ""), data)
        return data

    def _raise_for_status(self, status: int, url: str, body: bytes,
                          headers: dict) -> None:
        """把非 2xx 翻译成人话（限流要能被认出来，否则用户只看到 403）。"""
        if 200 <= status < 300:
            return
        if status == 404:
            raise GithubError(f"找不到（404）：{url}")
        if status in (403, 429) and headers.get("X-RateLimit-Remaining") == "0":
            reset = headers.get("X-RateLimit-Reset", "")
            when = ""
            try:
                when = time.strftime("%H:%M", time.localtime(int(reset)))
            except (TypeError, ValueError):
                when = "稍后"
            raise GithubError(
                "GitHub API 匿名配额用尽（每小时 60 次）——"
                f"{when} 之后重试，或在 config.json 的 "
                "plugins.plugin_manager.token 里填一个 GitHub token")
        detail = body[:200].decode("utf-8", "replace").strip()
        raise GithubError(f"GitHub 返回 {status}：{detail or url}")

    # ---- 具体接口 ----

    async def fetch_manifest(self, url: str, *, cache_ttl: float = 300.0) -> Any:
        """拉插件列表文件。列表文件是静态 JSON，允许缓存久一点。"""
        return await self.fetch_json(url, cache_ttl=cache_ttl)

    async def resolve_head(self, repo: str, ref: str = "", *,
                           cache_ttl: float = 120.0) -> HeadInfo:
        """查仓库 HEAD（`ref` 空则用默认分支）。

        **拿不到不算失败**：限流 / 无权限时返回 `unknown=True`，
        调用方据此说「无法确认是否最新」，绝不谎报「已是最新」。

        `cache_ttl=0` 用于「装完立刻记下当时那个提交」这种必须拿新值的场景。
        """
        ref = (ref or "").strip()
        # 空 ref → 用仓库默认分支（GitHub 的 /commits 不带 ref 就是这个语义）
        url = f"{self.api_base}/repos/{repo}/commits" + (f"/{ref}" if ref else "")
        try:
            data = await self.fetch_json(url, cache_ttl=cache_ttl)
        except GithubError as exc:
            return HeadInfo(repo=repo, ref=ref or "(default)", unknown=True,
                            note=str(exc))
        if not isinstance(data, dict):
            return HeadInfo(repo=repo, ref=ref or "(default)", unknown=True,
                            note="返回结构不是对象")
        commit = data.get("commit") or {}
        committer = commit.get("committer") or commit.get("author") or {}
        return HeadInfo(
            repo=repo,
            ref=ref or "(default)",
            sha=str(data.get("sha") or ""),
            date=str(committer.get("date") or ""),
        )

    async def fetch_archive(self, repo: str, ref: str, dest_dir: Path,
                            *, progress=None) -> ArchiveInfo:
        """把仓库源码包（tarball）下到 `dest_dir`，返回本地路径。

        `progress(done, total)` 可选：给「正在下载」这类长任务一个进度回调。
        """
        ref = (ref or "").strip() or "HEAD"
        url = self.codeload.format(repo=repo, ref=ref)
        session = await self._get_session()
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"{repo.replace('/', '__')}-{_safe_ref(ref)}.tar.gz"

        hdrs = _auth_headers(self.token)
        try:
            async with session.get(url, headers=hdrs, proxy=self.proxy,
                                   allow_redirects=True) as resp:
                if resp.status == 404:
                    raise GithubError(f"仓库或 ref 不存在（404）：{repo}@{ref}")
                if resp.status >= 400:
                    raise GithubError(f"下载源码包失败（HTTP {resp.status}）：{repo}@{ref}")
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                with open(target, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        fh.write(chunk)
                        done += len(chunk)
                        if progress and total:
                            progress(done, total)
                sha = (resp.headers.get("X-GitHub-Commit-Sha")
                       or resp.headers.get("X-Archive-Original-Commit-Sha") or "")
        except GithubError:
            target.unlink(missing_ok=True)
            raise
        except asyncio.CancelledError:
            target.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001 - 落盘/网络都可能炸
            target.unlink(missing_ok=True)
            raise GithubError(f"下载源码包失败：{repo}@{ref}（{exc}）", cause=exc)

        if target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise GithubError(f"源码包是空的：{repo}@{ref}")
        return ArchiveInfo(path=target, repo=repo, ref=ref, sha=sha)


def _safe_ref(ref: str) -> str:
    """把 ref 变成能当文件名的样子（分支名里可能有 `/`）。"""
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in ref)[:60]


def guess_repo(url: str) -> str:
    """从各种写法里抠出 `owner/repo`（`git@github.com:a/b.git`、网页链接等）。"""
    text = (url or "").strip()
    for prefix in ("https://github.com/", "http://github.com/",
                   "git@github.com:", "ssh://git@github.com/",
                   "https://www.github.com/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.removesuffix(".git").strip("/")
    parts = [p for p in text.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return ""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def env_token() -> str:
    """环境变量里的 token（`GITHUB_TOKEN` / `GH_TOKEN`）—— 配置项留空时的兜底。"""
    return (os.environ.get("NCQQPY_GITHUB_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN") or "").strip()
