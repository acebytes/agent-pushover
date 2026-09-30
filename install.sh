#!/bin/sh
# Local, dependency-free install. No curl | sh and no elevated privileges required.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  printf '%s\n' 'Python 3.11+ is required. Install it, then run this script again.' >&2
  exit 1
fi
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else "Python 3.11+ is required")'
exec "$PYTHON" -I "$ROOT/src/entry.py" install --source-dir "$ROOT" "$@"
