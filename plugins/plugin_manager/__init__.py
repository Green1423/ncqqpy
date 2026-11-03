"""plugin_manager —— 基于 GitHub 的插件管理器（下载 / 更新 / 卸载）。

入口在 `main.py`；本文件只做导出与 `PLUGIN` 声明（框架优先读它）。

命令（仅私聊 · 仅超管）：
    /plugin list | info | check | install | update | uninstall | add | refresh | trash
"""
from .main import PluginManagerPlugin

PLUGIN = PluginManagerPlugin
__all__ = ["PluginManagerPlugin"]
