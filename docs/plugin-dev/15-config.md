# 十五、配置管理

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[插件包结构：注册与逻辑分离](14-layout.md) · 下一章：[错误处理与用户反馈](16-errors.md)


## 15.1 config.json

首次启动自动生成 (../../config.json)，所有静态配置集中在此：

```json
{
  "ws_url": "ws://127.0.0.1:3001",
  "plugin_dir": "./plugins",
  "superusers": [123456789],
  "command_prefixes": ["/", "!"],
  "bound_groups": [100200300],
  "all_groups": false,
  "plugins": {}
}
```

构造参数覆盖配置文件值：

```python
client = NapCatClient()                           # 全走 config.json
client = NapCatClient(superusers=[111])           # 部分覆盖
```

## 15.2 bind / unbind 自动持久化

绑定/解绑群时自动同步 `config.json`，重启后保持。

## 15.3 插件配置

`self.config` 返回插件专属命名空间代理，读写自动隔离到 `plugins.<插件名>.*`：

```python
class MyPlugin(PluginBase):
    name = "my_plugin"

    async def on_load(self):
        # 配置项已经在加载前按 config_template() 补齐（见 15.3.1），
        # 这里直接读就行，不用再 setdefault
        self.threshold = self.config.get("threshold")

    async def on_disable(self):
        self.config.set("last_run", time.time())
```

### 15.3.1 首次运行即初始化全部配置项（`config_template()`）

**契约**：每个插件都要实现 `config_template()`，返回自己的**完整**默认配置；
插件首次加载时会把缺失的配置项补进
`config.json` 的 `plugins.<插件名>` 段。运维打开配置文件就能看到这个插件有哪些
开关可改，而不是只能翻代码。

> 放在构造期而不是加载期，是为了让保证对**任何**构造路径都成立：框架加载、
> 测试里直接 `MyPlugin(...)`、脚本里手动 new —— 都拿得到完整配置，不依赖调用方
> 记得补这一步（(../../tests/test_watchdog.py) 里就有直接 new 插件的用例）。

```python
class MyPlugin(PluginBase):
    name = "my_plugin"

    def config_template(self) -> dict:
        """出厂默认值（框架负责写进 config.json）。"""
        return {
            "enabled": True,
            "threshold": 100,
            "tags": ["a", "b"],
            "llm": {"model": "", "timeout": 60},     # 嵌套段逐键补
        }
```

四条语义（由 `PluginConfig.fill_defaults()` 实现）：

| 语义 | 说明 |
|------|------|
| **只补缺键** | 已有值（含运维手改的）**一个都不覆盖**；插件升级新增配置项时老部署自动补齐 |
| **递归补全** | 父键已存在也照样补里面缺的子键 —— `setdefault("llm", {...})` 在用户已配 `llm` 时整段不生效 |
| **幂等** | 没有缺键就完全不写盘；有缺键也只写一次（不是逐键重写整个文件） |
| **类型冲突只报告** | 模板要 dict、用户写成了别的类型 → 保留用户值，启动日志里 warning |

其它约定：

- 模板放**出厂默认值**，不要放运行期算出来的值；返回的字典会被深拷贝后再写入；
- **凭据留空占位**（`api_key: ""` 之类），真实密钥建议只放环境变量或由运维填；
- 模板要在**构造期**就能算出来（别依赖 `on_load` 里才建立的实例状态）；
- 补配置前框架会先按磁盘重读 `config.json`，所以手工编辑过的内容不会被启动时的
  旧快照覆盖；
- 模板写错（`config_template()` 抛异常）**不会**让插件起不来：插件照常可用，
  错误私聊超管；
- 不想写任何配置项就返回 `{}`（默认实现），框架不会创建空的 `plugins.<插件名>` 段。

> 运行期想主动补一次（例如运维删了键之后）用
> `self.config.fill_defaults(self.config_template())`，它返回
> `ConfigFill(filled, conflicts)`；`/aichat config` 就是这么做的。

## 15.4 ConfigManager API

| 方法 | 说明 |
|------|------|
| `cfg.get(key, default)` | 读（支持 `a.b.c` 点号路径） |
| `cfg.set(key, value)` | 写（自动保存） |
| `cfg.delete(key)` | 删 |
| `cfg.has(key)` | 是否存在 |
| `cfg.setdefault(key, default)` | 不存在时写入默认值 |
| `cfg.keys(prefix)` | 列出前缀下所有 key |
| `cfg.plugin(name)` | 返回 `PluginConfig` 代理 |

---
