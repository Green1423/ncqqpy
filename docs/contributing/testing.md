# 测试与发布

## 测试

全部套件都是**独立脚本**（不依赖 pytest），在仓库根用 venv-arm 跑：

```bash
./venv-arm/bin/python -B tests/test_watchdog.py        # 框架回归（最大）
./venv-arm/bin/python -B tests/test_command_parser.py  # 命令解析
./venv-arm/bin/python -B tests/test_help_structure.py  # /help 三级入口
./venv-arm/bin/python -B tests/test_command_permissions.py
./venv-arm/bin/python -B tests/test_command_alias.py   # /alias（父子命令）
./venv-arm/bin/python -B tests/test_userperm.py        # /userperm
./venv-arm/bin/python -B tests/test_unicode_names.py   # 非 ASCII 命令名
./venv-arm/bin/python -B tests/test_bool_short_flags.py
./venv-arm/bin/python -B tests/test_docs.py            # 文档链接/索引
```

完整清单与用途见 [tests/README.md](../../tests/README.md)。插件验收套件在各自插件仓的
`tests/` 下跑：[ai_chat](https://github.com/Green1423/ncqqpy-plugin-ai-chat) · [bupt_news](https://github.com/Green1423/ncqqpy-plugin-bupt-news) ·
[byrdocs](https://github.com/Green1423/ncqqpy-plugin-byrdocs) · [oj_bank](https://github.com/Green1423/ncqqpy-oj-bank)。

## 文档检查

`tests/test_docs.py` 检查：

- 所有相对链接能解析（跳过代码块与 http 链接）；
- 锚点能在目标文件里找到对应标题；
- `docs/` 下每个 Markdown 都能从 `docs/README.md` 到达；
- 单个文档行数不超过上限（大文档必须拆章）。

## 发布

```bash
# 生成 dist/ 七个仓库（框架 + 5 插件 + 插件索引）
./venv-arm/bin/python -B tests/build_repos.py --check

# 提交并推送（SSH；脚本自带 ssh -F /dev/null 兜底）
./venv-arm/bin/python -B tests/publish_repos.py -m "变更说明" --push
```

发布前自查会扫描**将要提交的文件**里的私有信息（真实群号、QQ、学号、token），
命中就中止 —— 示例值统一用 `100000001` 这类明显假的号码。
