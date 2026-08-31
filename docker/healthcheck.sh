#!/usr/bin/env bash
set -euo pipefail

base_url="${COMFYUI_BASE_URL:-http://127.0.0.1:8188}"
curl --fail --silent --show-error --max-time 4 \
  "${base_url}/system_stats" >/dev/null
