# 十九、渲染：Markdown 与 HTML 出图

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[工具](18-tools.md) · 下一章：[服务范围](20-scope.md) · 相关：[消息](08-messages.md)

QQ 不渲染 Markdown，也不会把 HTML 表格排好。想在聊天里给出**有排版**的内容
（公式、表格、代码块、题卡），只有一条路：**在服务端把 HTML 渲染成图片再发**。

这套东西以前散在三个插件里各写一遍（`ai_chat/render.py`、`oj_bank/render.py`、
`bupt_news/table_image.py`）：候选可执行名、`--no-sandbox` 那串启动参数、
超时杀进程、`file://` 拼装。三份的差别只有几个 flag —— 于是任何一份改了，
另外两份不会跟着改。2026-10-29 起统一到框架。

## 19.1 `napcat.markdown` —— Markdown → HTML / 纯文本

```python
from napcat.markdown import (markdown_to_html, to_plain_text,
                             build_markdown_html, katex_head)
```

| 函数 | 用途 |
|------|------|
| `markdown_to_html(text)` | 受支持子集 → HTML **片段**（标题/粗斜体/行内代码/围栏代码/列表/引用/表格/分隔线/链接） |
| `to_plain_text(text)` | 降噪成纯文本：去 `**`、`> `、反引号，`[文字](url)` → `文字（url）`；公式整段保护、一字不改 |
| `build_markdown_html(text, width=720)` | 完整页面（含 CSS 与 KaTeX 脚本），直接喂 Chromium |
| `wrap_bare_latex` / `split_inline_math` / `find_rich_blocks` | 公式相关的解析工具 |
| `katex_assets()` | 本机自带优先、缺失才退 CDN 的 `(css, js)` URI |

**转义是安全边界**：模型的输出、网页抓回来的内容都会进模板，不转义等于自己开
`<img src=file:///etc/...>` 的口子（Chromium 会真去读）。所以只认白名单语法，
所有文本先转义再拼标签。处理顺序也是边界：先把行内代码/公式摘成占位符，
再转义，最后还原 —— 顺序颠倒任何一个都会出注入或错排版。

KaTeX 资源查找顺序：`register_katex_dir()` 追加的目录 → `napcat/katex/` →
`plugins/ai_chat/katex/`（过渡期兼容）→ CDN。

## 19.2 `napcat.render` —— Chromium 无头渲染

```python
from napcat.render import find_chromium, run_chromium, file_uri, RenderError
```

| 成员 | 说明 |
|------|------|
| `find_chromium(hint="")` | 找浏览器：`hint`（配置里显式指定）→ PATH 常见名 → 常见绝对路径 |
| `run_chromium(chrome, args, *, profile, timeout, headless, stderr_to_null)` | 跑一次，返回 `(rc, stdout, stderr)`；超时会杀掉进程并抛 `RenderError` |
| `file_uri(path)` | 本地路径 → `file://`（中文/空格必须转义，否则 Chromium 打不开） |
| `RenderError` | 渲染失败；调用方据此降级成纯文本 |

固定带上的参数：`--no-sandbox --disable-gpu --disable-dev-shm-usage
--no-first-run --no-default-browser-check --disable-extensions
--disable-background-networking`，默认 `--headless=new`（要精确像素可用老的
`--headless`）。

## 19.3 什么时候用

- 内容里有 **公式/表格/代码块**，纯文本会难读 → `build_markdown_html()` 出图；
- 表格来自网页 HTML（通知正文） → 自己的页面模板 + `run_chromium` 截图；
- 只要"能读"、不要求排版 → 用 `to_plain_text()`，别出图（慢、还要装 Chromium）。

**没有 Chromium 也要能跑**：`find_chromium()` 返回 `None` 时降级成纯文本，
不要抛到用户脸上。机器上没装浏览器是常态，不是异常。

## 19.4 常见坑

- **视口太矮会整张白图**：Chromium 在高度小于约 200px 时可能画空白，所以统一
  按较大的高度渲染，再按像素 bbox 裁掉留白（`ai_chat/png.py` 的 `crop_*`）；
- **必须等 `load`**：`<script src="katex.min.js">` 是异步下载的，页面内联脚本
  若在 `DOMContentLoaded` 就渲染，`typeof katex === 'undefined'` 会静默 return，
  图里只剩 LaTeX 源码 —— 而且看起来像"正常文字"，很难发现；
- **不要拼 shell**：所有参数走 argv（`create_subprocess_exec`），页面路径里
  带空格或中文才不会被拆开；
- **超时一定要杀进程**：漏掉一次就是一堆 Chromium 僵尸把内存吃光。

## 19.5 框架与插件的版本错位

`napcat.markdown` / `napcat.render` 是框架模块，**只在进程启动时加载**；
插件是热重载的。新加框架模块后、进程还没重启时，插件直接 `import` 会失败
（插件会被判为"启用失败"，比渲染不对严重得多）。所以调用方要这样写：

```python
try:
    from napcat.render import find_chromium, run_chromium
except ImportError:                 # 老进程：按文件路径加载同一份实现
    ...                             # 见 plugins/ai_chat/render.py 里的 8 行样板
```

实现仍然只有一份，只是绕过了 import 缓存。重启之后自动走正常路径。
