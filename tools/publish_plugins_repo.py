#!/usr/bin/env python3
"""生成 / 发布插件列表仓库（`Green1423/ncqqpy-plugins`）的内容。

插件列表文件是插件管理器的**唯一可信来源**：`plugins/plugin_manager` 按它
决定「有哪些插件、从哪个仓库装、装哪条 ref」。这个脚本负责把那份内容
（`plugins.json` + `README.md`）生成出来，避免手写 JSON 写错一个逗号。

用法：
    ./venv-arm/bin/python -B tools/publish_plugins_repo.py            # 只生成
    ./venv-arm/bin/python -B tools/publish_plugins_repo.py --out ~/ncqqpy-plugins
    ./venv-arm/bin/python -B tools/publish_plugins_repo.py --out ~/ncqqpy-plugins --push

`--push` 会真的推远端（`git init` / `commit` / `push`），不加就是纯本地生成。
默认输出到 `dist/ncqqpy-plugins/`（与 `tests/build_repos.py` 的产物放在一起）。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from string import Template
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 列表里的插件（顺序即 `/plugin list` 的展示顺序）
#: name → (GitHub 仓库, ref, 介绍, 标签, 是否带运行期 data/)
PLUGINS: tuple[tuple[str, str, str, str, tuple[str, ...], bool], ...] = (
    ("essential", "Green1423/ncqqpy-plugin-essential", "main",
     "群管理基础命令（禁言/踢人/解禁/群信息）+ 入群问候与定时撤回",
     ("群管理",), False),
    ("bupt_news", "Green1423/ncqqpy-plugin-bupt-news", "main",
     "北邮校内通知定时同步与推送（门户登录 + AI 摘要 + 表格出图）",
     ("推送", "北邮"), True),
    ("byrdocs", "Green1423/ncqqpy-plugin-byrdocs", "main",
     "BYR Docs 资料检索与下载（按编号搜索 / 翻页 / 取文件 / 白名单）",
     ("检索",), True),
    ("ai_chat", "Green1423/ncqqpy-plugin-ai-chat", "main",
     "多会话 AI 对话（上下文压缩 + 内置工具 + 联网搜索 + Markdown 出图）",
     ("AI",), True),
    ("oj_bank", "Green1423/ncqqpy-oj-bank", "main",
     "本地题库与判题（洛谷抓题 / AI 搜题入库 / 题卡出图 / 10 组测试点 / 题组）",
     ("题库", "判题"), True),
)

README = """# ncqqpy-plugins

ncqqpy 机器人的**插件列表仓库**：`plugins.json` 是插件管理器
（框架里的 `plugins/plugin_manager`）的唯一可信来源。

## 怎么用

在机器人**私聊**里（仅超管可用）：

```
/plugin list                 看有哪些插件、装没装
/plugin install <插件名>      安装
/plugin update all           全部更新
/plugin uninstall <插件名>    卸载（进回收站，可找回）
```

插件管理器默认就从这个仓库的 `main` 分支读 `plugins.json`；换地址改
`config.json` 里的 `plugins.plugin_manager.manifest_url` 即可。

## 列表文件格式

```json
{
  "version": 1,
  "name": "ncqqpy 官方插件列表",
  "updated": "2026-10-10",
  "plugins": [
    {
      "name": "essential",
      "repo": "Green1423/ncqqpy-plugin-essential",
      "ref": "main",
      "version": "1.2.0",
      "description": "一句话介绍（/plugin list 里显示）",
      "tags": ["群管理"],
      "data": false
    }
  ]
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | ✅ | 插件标识，同时是本地目录名 `plugins/<name>/`；只允许字母数字与 `_ . -` |
| `repo` | ✅ | GitHub 仓库（`owner/repo`、网页链接、`git@github.com:...` 都认） |
| `ref` | | 分支 / tag / commit，缺省 = 仓库默认分支 |
| `version` | | 展示用版本号（实际版本以仓库里 `PluginBase.version` 为准） |
| `description` | | 一句话介绍 |
| `tags` | | 标签数组（或单个字符串） |
| `data` | | `true` 表示这插件有运行期 `data/`，卸载时保留 |

`version` 是列表文件的**格式**版本，当前只支持 `1`：填更大的数字时，
插件管理器会明确拒绝并要求先升级自己（而不是按老格式猜着解析）。

## 发布流程

```bash
# 在框架仓库里生成（含 README）到 dist/ncqqpy-plugins/
./venv-arm/bin/python -B tools/publish_plugins_repo.py

# 生成到指定目录，并推送到远端
./venv-arm/bin/python -B tools/publish_plugins_repo.py --out ~/ncqqpy-plugins \\
    --remote git@github.com:Green1423/ncqqpy-plugins.git --push
```

当前列表里的插件：

${list}

## 许可

MIT
"""


def build(out: Path, *, updated: str) -> dict:
    entries = []
    for name, repo, ref, description, tags, keep_data in PLUGINS:
        local = ROOT / "plugins" / name / "main.py"
        version = ""
        if local.is_file():
            # 与 installer.sniff_plugin_identity 同一条规则：类属性缩进也在内
            found = re.search(r"^\s*version\s*[:=]\s*['\"]([^'\"]+)['\"]",
                              local.read_text(encoding="utf-8"), re.M)
            if found:
                version = found.group(1)
        entries.append({
            "name": name,
            "repo": repo,
            "ref": ref,
            "version": version,
            "description": description,
            "tags": list(tags),
            "data": keep_data,
        })
    manifest = {"version": 1, "name": "ncqqpy 官方插件列表",
                "updated": updated, "plugins": entries}

    out.mkdir(parents=True, exist_ok=True)
    (out / "plugins.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    listing = "\n".join(
        f"- **{e['name']}** v{e['version']} — {e['description']}"
        f"（[{e['repo']}](https://github.com/{e['repo']})）" for e in entries)
    # 用 Template 而不是 str.format：README 里有 JSON 示例，
    # `{` `}` 会被 format 当成占位符（踩过：KeyError: '"version"'）
    (out / "README.md").write_text(
        Template(README).substitute(list=listing), encoding="utf-8")
    return manifest


def git(out: Path, *, remote: str, message: str, push: bool) -> None:
    def run(*args: str, check: bool = True):
        print("+ git", " ".join(args))
        return subprocess.run(["git", *args], cwd=out, check=check,
                              capture_output=True, text=True)

    if not (out / ".git").exists():
        run("init", "-b", "main")
        run("config", "user.name", "ncqqpy-bot")
        run("config", "user.email", "ncqqpy@users.noreply.github.com")
    run("add", "-A")
    status = run("status", "--porcelain").stdout.strip()
    if not status:
        print("没有改动，跳过提交")
    else:
        run("commit", "-m", message)
    if remote:
        run("remote", "remove", "origin", check=False)
        run("remote", "add", "origin", remote)
    if push:
        run("push", "-u", "origin", "main")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成/发布插件列表仓库内容")
    parser.add_argument("--out", default=str(ROOT / "dist" / "ncqqpy-plugins"),
                        help="输出目录（默认 dist/ncqqpy-plugins）")
    parser.add_argument("--remote", default="",
                        help="git 远端（如 git@github.com:Green1423/ncqqpy-plugins.git）")
    parser.add_argument("--push", action="store_true", help="真的推送（默认只本地提交）")
    parser.add_argument("--message", default="更新插件列表", help="提交信息")
    parser.add_argument("--updated", default="", help="写入 updated 字段的日期")
    parser.add_argument("--no-git", action="store_true", help="只写文件，不碰 git")
    args = parser.parse_args()

    import time
    updated = args.updated or time.strftime("%Y-%m-%d")
    out = Path(args.out).expanduser()
    manifest = build(out, updated=updated)
    print(f"已生成 {out}/plugins.json（{len(manifest['plugins'])} 个插件）")
    print(f"已生成 {out}/README.md")
    if not args.no_git:
        git(out, remote=args.remote, message=args.message, push=args.push)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
