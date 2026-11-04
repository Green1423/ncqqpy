"""命令行入口：`python -m tools.dekt_sim [scenarios|serve]`。

* `scenarios`（默认）：跑全部场景，打印结果；
* `serve`：起一个本地假后端（只监听 127.0.0.1），把 `plugins.bupt_news.afterclass.base`
  指过去就能让**真实插件**在离线环境里跑一遍同步。
"""
from __future__ import annotations

import argparse
import sys
import time

from .server import SimServer
from .scenarios import run_all


def _cmd_scenarios() -> int:
    results = run_all()
    width = max(len(name) for name, _p, _d in results)
    for name, passed, detail in results:
        mark = "✅" if passed else "❌"
        print(f"{mark} {name:{width}}  {detail[:80]}")
    ok = sum(1 for _n, p, _d in results if p)
    print(f"\n{ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


def _cmd_serve(host_port: str) -> int:      # noqa: ARG001 - 只绑本地
    srv = SimServer()
    srv.start()
    print(f"模拟后端已启动：{srv.base}")
    print("把 config.json 里的 plugins.bupt_news.afterclass.base 指过去即可：")
    print(f'  "afterclass": {{"enabled": true, "base": "{srv.base}"}}')
    print("（token 随便给一个模拟器签发的 JWT；Ctrl+C 退出）")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.dekt_sim")
    parser.add_argument("action", nargs="?", default="scenarios",
                        choices=["scenarios", "serve"])
    parser.add_argument("--port", default="", help="serve 时的端口（默认随机）")
    args = parser.parse_args(argv)
    if args.action == "serve":
        return _cmd_serve(args.port)
    return _cmd_scenarios()


if __name__ == "__main__":
    sys.exit(main())
