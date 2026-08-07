#!/usr/bin/env bash
# Install the ReSpeaker HAT support: mixer unit + the WirePlumber exclusion.
# Idempotent. Run after a fresh clone or an OS reinstall.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

install -Dm644 "$HERE/systemd/hermes-audio.service" \
    "$HOME/.config/systemd/user/hermes-audio.service"
install -Dm644 "$HERE/systemd/wireplumber/51-hermes-respeaker.conf" \
    "$HOME/.config/wireplumber/wireplumber.conf.d/51-hermes-respeaker.conf"

systemctl --user daemon-reload
systemctl --user enable --now hermes-audio.service
systemctl --user restart wireplumber 2>/dev/null || true

echo
echo "Installed. Verify:"
echo "  amixer -c wm8960soundcard sget Headphone     # expect ~87%, not 0%"
echo "  wpctl status | grep 'Built-in Audio Stereo'  # expect NOTHING (excluded)"
