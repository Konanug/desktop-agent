#!/usr/bin/env bash
# Give the physical screen back to a terminal, or return it to Hermes.
#
#     scripts/console-mode.sh on     # terminal on the HDMI screen
#     scripts/console-mode.sh off    # back to the Hermes panel
#     scripts/console-mode.sh status
#
# WHY THIS IS NEEDED AT ALL
# `hermes-fbcon-detach` unbinds the framebuffer console so the panel shows only
# the Hermes visual. That is correct in normal use -- a login prompt fighting
# the animation for the same pixels looks broken -- but it means the screen has
# no terminal when you need one, and "when you need one" is usually "the
# network is down and I cannot SSH in either". That is the exact situation this
# exists for, and it is why nothing here touches the network or the agent.
#
# Attaching fbcon is what actually puts a login prompt on the glass; stopping
# the renderer is only so the two are not drawing over each other.
set -u

case "${1:-status}" in
  on)
    systemctl --user stop hermes-display 2>/dev/null || true
    for v in /sys/class/vtconsole/vtcon*; do
        grep -qi "frame buffer" "$v/name" 2>/dev/null && echo 1 | sudo tee "$v/bind" >/dev/null
    done
    # A fresh clear so the last animation frame is not left underneath the
    # login prompt.
    sudo chvt 1 2>/dev/null || true
    echo "[console] terminal is on the screen. Log in as alanmyin."
    echo "[console] return with: $0 off"
    ;;
  off)
    for v in /sys/class/vtconsole/vtcon*; do
        grep -qi "frame buffer" "$v/name" 2>/dev/null && echo 0 | sudo tee "$v/bind" >/dev/null
    done
    systemctl --user start hermes-display
    echo "[console] Hermes panel restored."
    ;;
  status)
    b=0
    for v in /sys/class/vtconsole/vtcon*; do
        grep -qi "frame buffer" "$v/name" 2>/dev/null && b=$(cat "$v/bind")
    done
    echo "  fbcon bound : $b   (1 = terminal visible)"
    echo "  hermes-display: $(systemctl --user is-active hermes-display)"
    ;;
  *)
    echo "usage: $0 {on|off|status}"; exit 2 ;;
esac
