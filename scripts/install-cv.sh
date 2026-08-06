#!/usr/bin/env bash
# Set up hand tracking for hermes-camera: a venv with mediapipe, and the model.
#
# WHY A VENV, when CLAUDE.md says not to add one
# The renderer's "no installed dependencies" property is worth protecting and
# is untouched by this -- hermes-display still runs on system Pillow and numpy.
# Only the camera service uses this venv, and only for hand tracking. It is
# created with --system-site-packages because picamera2 is an apt package that
# cannot be pip-installed, and it was VERIFIED not to shadow the system numpy
# (2.2.4): pip installs its own numpy readily, and picamera2 breaks against a
# different one.
#
# The model is NOT in the wheel. mediapipe 1.0.0 ships zero .tflite files -- the
# hand_landmark directory contains only handedness.txt -- so it is fetched here.
#
# Idempotent. Safe to re-run.
set -euo pipefail

VENV="${HERMES_CV_VENV:-$HOME/.local/share/hermes-pi/cv-venv}"
MODELS="${HERMES_CV_MODELS:-$HOME/.local/share/hermes-pi/models}"
MODEL_URL="https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
MODEL="$MODELS/hand_landmarker.task"

echo "==> venv: $VENV"
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet mediapipe

echo "==> model: $MODEL"
mkdir -p "$MODELS"
if [ ! -s "$MODEL" ]; then
    curl -fL --progress-bar -o "$MODEL.tmp" "$MODEL_URL"
    mv "$MODEL.tmp" "$MODEL"
fi

echo "==> verifying"
# Checks the two things that actually break: that the system numpy is still the
# one in use (picamera2 depends on it), and that the model loads.
"$VENV/bin/python" - <<'PY'
import sys
import numpy
assert numpy.__file__.startswith("/usr/lib/"), (
    f"venv shadowed the system numpy ({numpy.__file__}) -- picamera2 will "
    f"break. Remove the venv's numpy and re-run.")
print(f"  numpy      {numpy.__version__}  (system, as required)")
import picamera2                                            # noqa: F401
print("  picamera2  imports")
import mediapipe as mp
print(f"  mediapipe  {mp.__version__}")

import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1])
                if "__file__" in dir() else os.getcwd())
PY

"$VENV/bin/python" -c "
import sys; sys.path.insert(0, '$(cd "$(dirname "$0")/.." && pwd)')
from camera.hands import HandTracker
t = HandTracker()
assert t.load(), t.error
print('  hand model loads')
"

echo
echo "Done. Point the service at this interpreter:"
echo "    ExecStart=$VENV/bin/python -m camera"
echo "(systemd/hermes-camera.service already does; reinstall it with)"
echo "    cp systemd/hermes-camera.service ~/.config/systemd/user/"
echo "    systemctl --user daemon-reload && systemctl --user restart hermes-camera"
