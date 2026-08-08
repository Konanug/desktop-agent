#!/usr/bin/env bash
# Google API libraries for the hermes_google plugin.
#
# INSTALLED WITH --target, NOT INTO HERMES' VENV. `hermes update` can recreate
# that venv, which would silently remove these and break the tools with a
# confusing ImportError long after the update. The plugin puts this directory
# on sys.path itself.
set -euo pipefail
LIBS="$HOME/.local/share/hermes-pi/google-libs"
mkdir -p "$LIBS"
python3 -m pip install -q --target "$LIBS" --break-system-packages --upgrade \
    google-api-python-client google-auth google-auth-oauthlib
"$HOME/.hermes/hermes-agent/venv/bin/python" - <<PY
import sys; sys.path.insert(0, "$LIBS")
from googleapiclient.discovery import build          # noqa: F401
from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
print("google libraries import cleanly from the gateway venv")
PY
echo
echo "Next: read the header of scripts/google_auth.py, then run it."
