#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -d ".venv" ]]; then
  echo "未找到 .venv，请先创建虚拟环境并安装依赖。" >&2
  exit 1
fi

source .venv/bin/activate

python -m pip install -U pip
python -m pip install -U py2app

rm -rf dist build
python setup_app.py py2app

echo "✅ 已生成：$ROOT/dist/MirroringKeymap.app"

