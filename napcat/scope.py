"""兼容别名：`napcat.scope` 已改名为 `napcat.subscribe`（新代码请用新名）。

保留本文件只为**已安装的旧插件**（它们 `from napcat.scope import Scope`）在升级
前继续可用。等所有插件都改用 `napcat.subscribe.Subscription` 后删除。
"""
from __future__ import annotations

from .subscribe import Subscription as Scope, parse_ids

__all__ = ["Scope", "parse_ids"]
