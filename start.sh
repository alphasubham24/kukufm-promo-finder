#!/bin/sh
# Start Promo Finder. No exports needed: settings are read from ./.env
# (real environment variables, e.g. Railway variables, take priority).
#
#   ./start.sh            -> web app on $PORT (default 8000)
#   ./start.sh cli ...    -> CLI, e.g. ./start.sh cli --data data/broken-soldier-duke --mode llm
set -e
cd "$(dirname "$0")"

# use the project virtualenv when present (local); the Docker image installs globally
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python; fi
if [ ! -f .env ] && [ -f .env.example ] && [ -z "$RAILWAY_ENVIRONMENT" ]; then
  echo "==> No .env found. Copy .env.example to .env and fill in your keys."
fi

if [ "$1" = "cli" ]; then
  shift
  exec $PY -m promo_finder "$@"
fi

PORT="${PORT:-8000}"
echo "==> Promo Finder container starting"
$PY - <<'PYEOF' || true
from promo_finder.llm import LLM
from pathlib import Path
try:
    d = LLM().describe()
    print(f"    endpoint: {d['provider']}")
    print(f"    model:    {d['model']}")
    print(f"    config:   {'OK' if d['key_configured'] else 'INCOMPLETE (need LLM_BASE_URL, LLM_API_KEY, LLM_MODEL) -> rules-only mode'}")
    print(f"    parallel: {d['concurrency']} call(s), throttle {str(d['tpm_budget']) + ' tokens/min' if d['tpm_budget'] else 'off'}")
except Exception as e:
    print(f"    LLM config error: {e}  (the app will still start in rules-only mode)")
print(f"    data:     {len(list(Path('data').rglob('*.txt')))} screenplay files in ./data")
PYEOF
echo "    port:     ${PORT}"
exec $PY -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --proxy-headers --forwarded-allow-ips='*'
