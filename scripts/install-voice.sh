#!/usr/bin/env bash
# Voice pipeline dependencies, in their own venv.
#
# SEPARATE FROM cv-venv ON PURPOSE. That one exists for mediapipe and is
# --system-site-packages so picamera2 stays visible; it is also verified not to
# shadow the system numpy, which picamera2 breaks against. Voice needs
# ctranslate2 and onnxruntime, which have their own numpy opinions, and the
# camera service must not inherit them. Two venvs, no shared blast radius.
#
# The renderer's no-dependencies property is untouched either way -- only
# hermes-voice runs from this one.
set -euo pipefail
VENV="$HOME/.local/share/hermes-pi/voice-venv"
MODELS="$HOME/.local/share/hermes-pi/models"

echo "==> venv at $VENV"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
# openwakeword ships its pretrained wake words INSIDE the wheel (unlike
# mediapipe, whose hand model had to be fetched separately). faster-whisper
# fetches its model from HuggingFace on first use, so it is warmed below.
"$VENV/bin/pip" install -q openwakeword faster-whisper piper-tts

echo "==> checking wake-word models shipped in the wheel"
"$VENV/bin/python" - <<'PY'
import pathlib, openwakeword
d = pathlib.Path(openwakeword.__file__).parent / "resources" / "models"
names = sorted(p.stem for p in d.glob("*.onnx")
               if not any(k in p.stem for k in ("melspec", "embedding", "silero",
                                                "logreg", "mul_", "sigmoid")))
print("   available:", ", ".join(names))
assert any("hey_jarvis" in n for n in names), "hey_jarvis model missing"
PY

echo "==> warming the STT model (first use downloads it; ~1 min)"
mkdir -p "$MODELS/whisper"
HF_HUB_DISABLE_TELEMETRY=1 "$VENV/bin/python" - "$MODELS/whisper" <<'PY'
import sys, time
from faster_whisper import WhisperModel
t0 = time.time()
WhisperModel("base.en", device="cpu", compute_type="int8", download_root=sys.argv[1])
print(f"   base.en ready in {time.time()-t0:.0f}s")
PY

echo "==> text to speech"
# NOT `apt install piper`. Debian's `piper` package is the PIPER MOUSE
# CONFIGURATION GUI -- it installs ratbagd and has nothing to do with speech.
# Installing it and then running `piper --model ...` fails with "Unknown option
# --model", which reads like a version skew rather than the wrong program
# entirely. The TTS engine is piper-tts on PyPI, installed into the venv above.
"$VENV/bin/python" -c "import piper" 2>/dev/null || {
    echo "   piper-tts did not install"; exit 1; }
VOICE_DIR="$MODELS/piper"; mkdir -p "$VOICE_DIR"
V="$VOICE_DIR/en_US-lessac-medium.onnx"
if [ ! -f "$V" ]; then
    B="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"
    echo "   fetching voice model"
    curl -fsSL "$B/en_US-lessac-medium.onnx"      -o "$V"
    curl -fsSL "$B/en_US-lessac-medium.onnx.json" -o "$V.json"
fi

echo "==> verifying the ReSpeaker is reachable and NOT held by wireplumber"
"$VENV/bin/python" - <<'PY'
import subprocess, sys
r = subprocess.run(["arecord", "-D", "plughw:wm8960soundcard", "-f", "S16_LE",
                    "-r", "16000", "-c", "1", "-d", "1", "/dev/null"],
                   capture_output=True)
print("   mic capture:", "OK" if r.returncode == 0 else
      f"FAILED -- {r.stderr.decode()[:120]}")
sys.exit(0 if r.returncode == 0 else 1)
PY

echo
echo "Done. Next: scripts/install-audio.sh if you have not run it, then"
echo "  systemctl --user enable --now hermes-voice"
