#!/bin/zsh
set -e

APP_ROOT="${0:A:h}"
cd "$APP_ROOT"

# VS Code 같은 Electron 앱에서 실행하면 이 값 때문에 Electron이 Node로 동작할 수 있다.
unset ELECTRON_RUN_AS_NODE

if [[ ! -x "$APP_ROOT/.venv/bin/python" || ! -x "$APP_ROOT/.venv/bin/music-engine" ]]; then
  print "처음 한 번만 엔진 Python 환경을 준비합니다..."
  uv sync --python 3.12.12 --group dev
fi

if [[ ! -x "$APP_ROOT/node_modules/.bin/electron" || ! -x "$APP_ROOT/node_modules/.bin/esbuild" ]]; then
  print "처음 한 번만 Electron 실행 환경을 준비합니다..."
  npm install --no-audit --no-fund
fi

print "최신 앱 소스를 빌드합니다..."
npm run build:app

exec "$APP_ROOT/node_modules/.bin/electron" "$APP_ROOT"
