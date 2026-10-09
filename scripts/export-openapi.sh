#!/usr/bin/env bash
# Regenerate frontend/types/openapi.json and openapi.d.ts from the FastAPI app.
# Dev routes are included so the typed client covers /api/dev/* (used by the e2e tests).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/backend/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
(cd "$ROOT/backend" && ENABLE_DEV_ENDPOINTS=true "$PY" -c \
  "import json; from app.main import app; print(json.dumps(app.openapi(), indent=1))") \
  > "$ROOT/frontend/types/openapi.json"
(cd "$ROOT/frontend" && npm run --silent gen:api)
