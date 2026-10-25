# 十三、热重载

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[完整插件示例](12-example.md) · 下一章：[插件包结构：注册与逻辑分离](14-layout.md)


开发时保存文件即可自动生效（2 秒检测间隔）。也可手动触发：

```text
/reload              # 重载全部插件（含新添加的）
/reload welcome      # 热重载指定插件
```

关闭自动监控：`client.plugins.stop_watch()`

## 13.0 热重载**只换插件代码**，不换框架

改了 `napcat/`（框架）里的任何东西 —— 新模块、`PluginBase` 的新方法、命令系统行为 ——
都要**重启进程**（`systemctl restart ncqqpy`）才生效。热重载只是重新 import
`plugins/<名字>/`。

这不是缺点，是边界：框架代码在进程启动时就被广泛引用了（类、注册表、事件循环），
热替换只会换出一半新一半旧的诡异状态。

> 依赖新框架 API 的插件**必须**自己兜底：`hasattr(self, "register_skill")` 之类，
> 没兜底的插件会在老进程里 `on_load` 直接失败。写插件时把「框架可能比插件旧」
> 当成正常状态（技能、`napcat.skills` 就是这么接入的）。

## 13.1 从 GitHub 装卸插件（`plugin_manager`）

框架自带 (../../plugins/plugin_manager)：在**私聊**里以超管身份把插件从 GitHub
装进来、更新、卸载。插件列表文件放在 `Green1423/ncqqpy-plugins` 的
`plugins.json`（"有哪些插件、从哪个仓库装、装哪条 ref"全由它决定）。

```text
/plugin list                  看有哪些、装没装
/plugin install <插件名>       安装（下载 → 校验 → 原子替换 → 热重载）
/plugin update all            全部更新（本地没动过的文件才覆盖）
/plugin uninstall <插件名>     卸载（进 plugins/.trash/，可找回）
```

对自己的插件而言，这意味着三件事：

1. **入口要自洽**：包目录里必须有 `__init__.py`（或单文件 `name.py`），
   且 `PluginBase` 子类的 `name` 要与列表文件里的插件名一致 ——
   不一致会被拒绝安装（避免把 ai_chat 装进 bupt_news 的目录）；
2. **`data/` 是你的运行时目录**：更新时它跟着走、卸载时默认留在原地，
   所以**配置与会话数据放 `data/` 才是安全的**（放别处会在更新时被当成
   仓库文件比对）；
3. **别改仓库里的文件当配置**：手改过的文件会让更新停下来要 `--force=true`。
   要留自己的改动就新增文件（新文件不在同步范围内，永不删除）。

命令与机制详见 (../../plugins/plugin_manager/README.md)；
列表文件的生成脚本是 (../../tools/publish_plugins_repo.py)。

---
