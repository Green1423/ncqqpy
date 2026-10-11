# plugin_manager —— 基于 GitHub 的插件管理器

在 QQ 私聊里管理 ncqqpy 插件：**下载 / 更新 / 卸载**，全部基于 GitHub 仓库。
插件列表文件放在 `Green1423/ncqqpy-plugins` 的 `plugins.json`。

> 🔒 **仅私聊 · 仅超级用户**：所有子命令都声明 `permission="superuser"` +
> `scope="private"`，群聊里连命令名都不出现（框架按「命令不存在」处理）。
> 这是有意的：装插件等于在服务器上执行代码。
>
> 文档索引：[docs/README.md](../../docs/README.md)

---

## 目录

1. [命令](#一命令)
    - [1.1 安装与更新有什么区别](#11-安装与更新有什么区别)
    - [1.2 `--force=true` 的语义](#12---forcetrue-的语义)
    - [1.3 依赖](#13-依赖)
2. [它是怎么工作的](#二它是怎么工作的)
    - [2.1 列表文件（唯一可信来源）](#21-列表文件唯一可信来源)
    - [2.2 下载：源码包而不是 `git clone`](#22-下载源码包而不是-git-clone)
    - [2.3 更新：原子替换 + 回收站](#23-更新原子替换--回收站)
    - [2.4 记账：为什么能分辨「你改的」和「上游改的」](#24-记账为什么能分辨你改的和上游改的)
    - [2.5 卸载与回收站](#25-卸载与回收站)
3. [配置（`config.json` → `plugins.plugin_manager`）](#三配置configjson--pluginsplugin_manager)
4. [安全边界（为什么可以放心用）](#四安全边界为什么可以放心用)
5. [开发与测试](#五开发与测试)
6. [常见问题](#六常见问题)

---

## 一、命令

| 命令 | 作用 |
|------|------|
| `/plugin list [关键字] [installed]` | 列出列表文件里的插件与本地安装状态 |
| `/plugin info <插件名>` | 单个插件的详情（来源仓库、提交、本地改动、上游状态） |
| `/plugin check` | 检查全部已装插件的上游更新 |
| `/plugin install <插件名> [ref] [--force=true]` | 安装（已装过的用 `update`） |
| `/plugin update <插件名 …\|all> [--force=true]` | 更新（没装过时等价于安装） |
| `/plugin uninstall <插件名> [--purge=true]` | 卸载（进回收站，可找回） |
| `/plugin add <仓库> [name] [ref] [subdir] [--force=true]` | 装列表文件里没有的仓库 |
| `/plugin refresh` | 重新拉取列表文件（清缓存） |
| `/plugin trash [--purge=true] [--keep=N]` | 查看 / 清理回收站 |

`<名称>` 是必填、`[名称]` 是可选的；键值参数写 `--键=值`（例如 `--force=true`），
`key=value` 不带 `--` 会被当成位置参数。

常用流程：

```
/plugin list                     # 看有什么
/plugin install ai_chat          # 装
/plugin check                    # 看哪些能更新
/plugin update all               # 一起更新
/plugin uninstall ai_chat        # 卸载（data/ 留在原地）
```

### 1.1 安装与更新有什么区别

- `install` **不覆盖**：目录已存在（哪怕是手工装的）会明确拒绝，除非 `--force=true`；
- `update` 只管已装的：按上游与本地现状算出一份文件级计划，**内容没变就直接
  报「已是最新」**，不做无意义的重写；
- 两者装完都会调框架的 `PluginManager.reload()` 让新代码立刻生效（自己除外）。

### 1.2 `--force=true` 的语义

| 场景 | 默认行为 | 加 `--force=true` |
|------|---------|------------------|
| 你手改过的文件、上游这次也改了 | **拒绝更新**，列出冲突文件 | 用上游版本覆盖（旧目录仍在回收站） |
| 目录不是本管理器装的 | `install` 拒绝 | 接管并覆盖 |
| 仓库里声明的 `name` 与插件名不一致 | 拒绝（避免装错目录） | 按你给的名字装 |
| 你要删的文件本地也改过 | 保留本地版本 | 删除（以仓库为准） |

「本地多出来的文件」（你自己加的补丁、`data/` 里的数据）**永远不删**，
除非显式把 `keep_local_extra_files` 配成 `false`。

### 1.3 依赖

插件可以声明依赖别的插件（框架能力，见[插件开发文档 §1.3](../../docs/plugin-dev.md#13-声明依赖别的插件必需--可选)）。本管理器
在这件事上做三件事：

- **装完就告诉你还缺谁**：回执里列出 `依赖：store`、`⚠️ 还缺必需依赖：store`
  和补齐命令 `/plugin install store`；可选依赖缺失只作提示（不影响运行）。
- **依赖就位后自动加载**：装完依赖后会立刻重试那些「因缺依赖没起来」的插件，
  并补一条回执（不用手工 `/reload`）。
- **卸载保护**：还有已启用插件依赖它时，`/plugin uninstall <被依赖的>` 会被
  框架拒绝（回执里说明谁在依赖它）；先卸载使用者或显式 force 才动得了。

`/plugin list` 里带依赖的插件会标 `🔗需 store（缺 store）`，`/plugin info`
里展开完整依赖清单。

---

## 二、它是怎么工作的

### 2.1 列表文件（唯一可信来源）

`plugins.json` 决定「有哪些插件、从哪个仓库装、装哪条 ref」：

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
      "description": "群管理基础命令 + 入群问候",
      "tags": ["群管理"],
      "data": false
    }
  ]
}
```

- `name` 同时是本地目录名（`plugins/<name>/`），只允许字母数字与 `_ . -`；
- `repo` 支持 `owner/repo`、网页链接、`git@github.com:owner/repo.git`；
- `ref` 缺省 = 仓库默认分支；`data: true` 表示这插件有运行期数据，
  卸载时默认保留它的 `data/`；
- 解析失败会明确指出第几条错在哪，**绝不让半个列表生效**；
- 拉不到列表文件时用上一次成功拉取的本地副本，并在输出里标注「离线」——
  `/plugin list`、`uninstall`、`trash` 这些不依赖网络的功能照常可用。

> 依赖**不写在列表文件里**：`requires` / `optional_requires` 是插件自己的
> 声明（跟代码一起走），列表文件只负责「装哪个仓库」。这样依赖关系不会出现
> 「改了插件代码却忘了改列表文件」的漂移。

列表文件本身由框架仓库里的 `tools/publish_plugins_repo.py` 生成：

```bash
./venv-arm/bin/python -B tools/publish_plugins_repo.py --out ~/ncqqpy-plugins
./venv-arm/bin/python -B tools/publish_plugins_repo.py --out ~/ncqqpy-plugins \
    --remote git@github.com:Green1423/ncqqpy-plugins.git --push
```

### 2.2 下载：源码包而不是 `git clone`

用 GitHub 的 `codeload` tarball（`https://codeload.github.com/<repo>/tar.gz/<ref>`）：

- bot 机器**不需要装 git**，也不需要仓库历史；
- 解压前先整体检查成员名，含 `..`、绝对路径、符号链接、设备文件一律拒绝，
  再用 `tarfile` 的 `data` 过滤器兜一层；
- 解压后校验「确实是个插件」：有 `__init__.py` 或 `main.py`，且里面的
  `name` 声明与插件名一致；
- 只看 `__init__.py` / `main.py` 读版本号，全目录正则会误读示例代码。

### 2.3 更新：原子替换 + 回收站

```
下载解压到 plugins/plugin_manager/data/.staging/
  → 以「本地现状」为基线，把仓库里参与同步的文件覆盖上去
  → 老目录整体挪进 plugins/.trash/<插件名>-<时间戳>/
  → 新目录挪到 plugins/<插件名>/
  → 写记账（文件级 sha256）
```

任何一步失败都能挪回来：替换失败时自动把旧目录移回原位。
`plugins/.test-tmp/`、`tests/`、`docs/`、`.gitignore`、`LICENSE` 这些
开发文件不参与同步，不会被装进 `plugins/`。

### 2.4 记账：为什么能分辨「你改的」和「上游改的」

安装时把每个文件的 sha256 记进 `plugins/plugin_manager/data/installed.json`
（同时在插件目录里留一份 `.ncqqpy-plugin.json`，手工复制插件也带着来源信息）。
更新时比对三份哈希：

| 上游 | 本地 | 上次记录 | 处理 |
|------|------|---------|------|
| 改了 | 没动 | 一致 | 安全覆盖 |
| 改了 | 改了 | 不一致 | **冲突**（要 `--force=true`） |
| 删了 | 没动 | 一致 | 跟着删 |
| 删了 | 改过 | — | 保留本地版本 |
| 没有 | 有（你没记录） | — | 你的文件，永不删 |

「有没有更新」靠提交号比对：源码包响应头不一定带提交号，装完会调一次
GitHub API 补记；老记账缺提交号时，`/plugin check` 会顺手补上。

### 2.5 卸载与回收站

卸载**不删文件**：整个目录（或除了 `data/` 之外的部分）挪进
`plugins/.trash/<插件名>-<时间戳>/`，同时把插件从运行中的框架里
`disable → unload`。确认没问题后用 `/plugin trash --purge=true` 清掉，
要找回就直接从 `.trash/` 里拷回来。

---

## 三、配置（`config.json` → `plugins.plugin_manager`）

首次加载时框架会自动写进这些键：

| 键 | 默认 | 说明 |
|----|------|------|
| `manifest_url` | `https://raw.githubusercontent.com/Green1423/ncqqpy-plugins/main/plugins.json` | 插件列表文件地址 |
| `token` | `""` | GitHub token。留空 = 匿名访问（每小时 60 次 API，够用；`/plugin check` 一次消耗每个仓库 1 次）。⚠️ 填了就是明文存在 `config.json` 里，建议改用环境变量 `NCQQPY_GITHUB_TOKEN` / `GITHUB_TOKEN` |
| `timeout` | `20` | 网络超时（秒）；源码包较大时可以调大 |
| `proxy` | `""` | 代理，如 `http://127.0.0.1:7890` |
| `manifest_cache_seconds` | `300` | 列表文件缓存秒数（0 = 每次都重新拉） |
| `keep_local_extra_files` | `true` | 更新时保留你自己加进插件目录的文件 |
| `max_report_lines` | `12` | 一条回执最多列几行 |
| `plugin_dir` | `""` | 装卸动作落在哪个目录（留空 = 跟随框架的 `plugin_dir`；一般不用改） |

匿名配额用尽时命令会明确说「配额用尽、几点之后重试，或去配 token」，
不会把 403 说成「仓库不存在」。

---

## 四、安全边界（为什么可以放心用）

1. **装插件 = 执行代码**：所以命令只对私聊里的超级用户开放，且不接受
   「转发来的仓库」——除了 `/plugin add` 这条明确的逃生口，其它都只信列表文件；
2. **压缩包不可信**：拒绝路径穿越、绝对路径、链接、设备文件（`safe_extract`）；
3. **不覆盖你的改动**：手改过的文件默认拒绝被上游覆盖，`--force=true` 才动，
   且旧版本进回收站；
4. **不删数据**：`data/`、`.trash/`、你自己加的文件都不在删除范围内；
5. **不动自己**：`plugin_manager` 拒绝在运行中装卸自己（`SelfOperationError`），
   避免把自己从磁盘上抹掉；要更新它请 `git pull` 或手工替换目录后重启。

---

## 五、开发与测试

```bash
# 框架仓库根目录下
./venv-arm/bin/python -B plugins/plugin_manager/tests/test_plugin_manager.py
```

测试套件在临时目录里起一个**本地 HTTP 服务当 GitHub**（列表文件 + 源码包），
所以「解析 → 下载 → 安全解压 → 原子替换 → 记账 → 热重载 → 卸载」是全程真跑的；
另外覆盖解压安全（`..` / 绝对路径 / 链接）、冲突保护、权限（仅私聊 + 仅超管）、
忙锁与离线兜底。所有装卸都发生在临时 `plugins/` 里，**不会碰真实插件目录**。

---

## 六、常见问题

**Q：装到一半断网了怎么办？**
不会留下半旧半新的目录：下载/解压失败时 `plugins/` 一个字节都没动。
重新发一次命令即可。

**Q：`/plugin check` 说「无法比对」？**
说明记账里没有提交号（老的安装记录）或者 GitHub 配额用尽。前者再更新一次
就会记上；后者看提示里给的恢复时间，或配一个 token。

**Q：我改了插件代码，更新会覆盖吗？**
会拦下来并列出冲突文件，除非你加 `--force=true`。想长期保留自己的改动，
更好的做法是把补丁做成插件目录里**新增**的文件（比如 `local_patch.py`），
更新不会碰它。

**Q：卸载后 `plugins/<名字>/data` 还在？**
默认就在（`--purge=true` 才会连 `data/` 一起进回收站）。重装同一个插件时
它会续上，配置和数据都不用重配。

**Q：装完提示「还缺必需依赖」，它自己为什么不加载？**
那是对的：插件声明的必需依赖不在时，框架**不加载**它（避免一个半死的实例
在里面悄悄收事件）。按提示 `/plugin install <缺的依赖>`，装完会自动加载。

**Q：想装自己的私有插件？**
`/plugin add git@github.com:you/my-plugin.git`（私有仓库需要配 `token`），
或直接 `git clone` 到 `plugins/<名字>/` 后让框架热重载。
