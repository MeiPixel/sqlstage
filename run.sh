#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")"
case "${1:-backend}" in
  frontend)
    cd frontend
    exec npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
    ;;
  backend)
    exec .venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port 8020
    ;;
  check)
    exec .venv/bin/python -c "import sqlstage, fastapi, uvicorn, yaml, socksio; from sqlstage import ModelConfig; c = ModelConfig(); print('SQLStage OK; provider=' + c.provider + '; thinking=' + c.thinking)"
    ;;
  *) echo 'Usage: bash run.sh [backend|frontend|check]'; exit 1 ;;
esac
