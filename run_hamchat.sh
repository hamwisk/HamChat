#!/usr/bin/env bash
set -euo pipefail

# Always run from the directory this script lives in
cd "$(dirname "$0")"

# Activate the venv
if [ -d ".venv" ]; then
  # shellcheck disable=SC1090
  source .venv/bin/activate
else
  echo "ERROR: .venv not found. Run ./setup_venv.sh first."
  exit 1
fi

# Run HamChat via main.py with logging
exec python3.10 main.py --log-level DEBUG "$@"