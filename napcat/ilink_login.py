"""
iLink 扫码登录 CLI。

    cd /ncqqpy && ./venv-arm/bin/python -m napcat.ilink_login

扫码成功后凭据写到 `plugins/ncqq_watchdog/data/ilink_credentials.json`（权限 600），
插件的通知器会自动读取。登录只需要做一次；之后如果服务端返回
`errcode=-14 session timeout`，说明登录态过期，重新跑一次这个命令即可。

二维码是**在终端里现画**的（iLink 返回的 `qrcode_img_content` 是个 H5 页面链接，
不是图片，没法直接转发）—— 用微信扫屏幕上的码即可。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

if __package__ in (None, ""):                     # 支持直接 python napcat/ilink_login.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from napcat.ilink import login_with_qr
    from napcat.ilink.client import DEFAULT_BASE_URL
    from napcat.qr_text import make_qr
    from napcat.watchdog import DATA_DIR_ENV
else:
    from .ilink import login_with_qr
    from .ilink.client import DEFAULT_BASE_URL
    from .qr_text import make_qr
    from .watchdog import DATA_DIR_ENV

# 凭据与看门狗共用同一个数据目录（可用 NCQQ_WATCHDOG_DATA_DIR 覆盖）
DEFAULT_CRED_PATH = (Path(os.environ[DATA_DIR_ENV]) if os.environ.get(DATA_DIR_ENV)
                     else Path(__file__).resolve().parent / "data") / "ilink_credentials.json"

_STATUS_TEXT = {
    "waiting": "等待扫码…",
    "scanned": "已扫码，请在手机上确认",
    "refreshing": "二维码过期，正在刷新…",
    "redirected": "已切换到就近接入点，继续等待…",
    "confirmed": "登录成功",
}


def render_qr(url: str):
    """在终端画二维码（纯标准库；失败时退化为打印链接）。"""
    try:
        qr = make_qr(url, border=2)
        print()
        print(qr.to_ascii())
        print()
    except Exception as e:                          # 画不出来也别挡住登录
        print(f"[warn] 终端二维码渲染失败（{type(e).__name__}: {e}），请手动打开链接")
    print(f"二维码链接（也可以在微信里打开）：\n{url}\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="微信 iLink Bot 扫码登录")
    parser.add_argument("--save-to", default=str(DEFAULT_CRED_PATH),
                        help=f"凭据保存路径（默认 {DEFAULT_CRED_PATH}）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="iLink 服务地址")
    parser.add_argument("--timeout", type=float, default=480.0,
                        help="扫码总超时秒数（默认 480）")
    parser.add_argument("--no-ascii", action="store_true",
                        help="只打印链接，不在终端画二维码")
    args = parser.parse_args(argv)

    def on_qr(url: str):
        if args.no_ascii:
            print(f"二维码链接：{url}")
        else:
            render_qr(url)

    last_status = [""]

    def on_status(status: str):
        text = _STATUS_TEXT.get(status, status)
        if status != last_status[0]:
            last_status[0] = status
            print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)

    print("正在向微信 iLink 申请登录二维码…")
    try:
        creds = login_with_qr(on_qrcode=on_qr, on_status=on_status,
                              base_url=args.base_url, timeout=args.timeout)
    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except Exception as e:
        print(f"\n登录失败：{type(e).__name__}: {e}")
        return 1

    path = creds.save(args.save_to)
    print(f"✅ 登录成功")
    print(f"   account   : {creds.account_id}")
    print(f"   扫码用户   : {creds.user_id or '(未返回)'}")
    print(f"   base_url  : {creds.base_url}")
    print(f"   凭据已保存 : {path}")
    if creds.user_id:
        print(f"\n下一步：把 config.json 里 plugins.ncqq_watchdog.target 设为\n"
              f"  \"wechat\": {{ \"target\": \"{creds.user_id}\" }}\n"
              f"或者登录后在微信里给 bot 发一条消息，插件会自动缓存该用户。")
    print("\n提示：用微信给 bot 发一条消息，插件的通知器即可拿到 context_token，"
          "之后才能主动推送。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
