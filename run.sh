#!/usr/bin/env bash
# 启动 ncqqpy 机器人（自动使用 aarch64 venv，从 .env 读取门户密码）
#
#   cp .env.example .env    # 填入门户密码
#   ./run.sh
#
cd "$(dirname "$0")"
set -euo pipefail

# 载入 .env（若存在）：BUPT_PORTAL_PASSWORD / BUPT_PORTAL_USERNAME
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

VENV=venv-arm
if [ ! -x "$VENV/bin/python" ]; then
  echo "找不到 $VENV，请先创建运行环境：" >&2
  echo "  python3 -m venv --without-pip $VENV" >&2
  echo "  $VENV/bin/python get-pip.py" >&2
  echo "  $VENV/bin/pip install websockets pydantic aiohttp aiofiles" >&2
  exit 1
fi

exec "$VENV/bin/python" main.py
