"""plugin_manager —— 基于 GitHub 的插件管理器（下载 / 更新 / 卸载）。

入口在 `main.py`；本文件只做导出，方便 `from plugin_manager import ...`
与框架的包目录发现（`plugins/<name>/__init__.py`）。

命令（仅私聊 · 仅超管）：
    /plugin list | info | check | install | update | uninstall | add | refresh | trash
"""
from .main import PluginManagerPlugin

__all__ = ["PluginManagerPlugin"]
