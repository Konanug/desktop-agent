#!/usr/bin/env bash
# Put the ReSpeaker 2-Mic HAT into a known state. Idempotent; safe to re-run.
#
# WHY THIS EXISTS RATHER THAN `alsactl store`
# The saved state IS correct -- `alsactl restore wm8960soundcard` applies it
# perfectly by hand. What fails is the boot-time restore, which loses a race
# with card registration: alsa-restore.service ran at 4.70 s while the WM8960
# codec only probed at 3.42 s and the ALSA card binds later still, so the card
# is not there to restore onto and the failure is SILENT. Observed after an
# unclean power cut: the routing switches had survived, both volumes were back
# at driver defaults (Headphone 0%, Speaker 82%) -- i.e. no sound at all from
# the headphone jack, which is exactly what someone would plug in first.
#
# Making the settings CODE in the repo rather than machine state under
# /var/lib/alsa also means they are reviewable, versioned, and survive a
# reinstall. There is one place to look and it is this file.
#
# THE WM8960 DOES NOT CONNECT ITS DAC TO THE OUTPUT MIXER BY DEFAULT. That is
# the single most likely cause of "the card exists and there is no sound", and
# it is the first two settings below.
set -u

CARD="${HERMES_AUDIO_CARD:-wm8960soundcard}"

# Wait for the card: simple-card binding is not instant, and this unit is
# deliberately allowed to start early rather than guess an ordering.
for _ in $(seq 1 30); do
    amixer -c "$CARD" scontrols >/dev/null 2>&1 && break
    sleep 0.5
done
if ! amixer -c "$CARD" scontrols >/dev/null 2>&1; then
    echo "[audio] card '$CARD' never appeared -- nothing to configure" >&2
    exit 0        # not fatal: the HAT may simply not be fitted
fi

set_() {
    if amixer -c "$CARD" sset "$1" "$2" >/dev/null 2>&1; then
        printf '[audio] %-26s -> %s\n' "$1" "$2"
    else
        echo "[audio] no control '$1' on $CARD (skipped)" >&2
    fi
}

# Routing. Without these the DAC is not wired to either output and the card is
# silent however loud the volumes are.
set_ 'Left Output Mixer PCM'  on
set_ 'Right Output Mixer PCM' on

# Outputs. Both are driven because the HAT has a 3.5 mm jack (Headphone) and a
# JST speaker connector (Speaker), and which is in use is not knowable from
# here -- neither reports whether anything is plugged in.
set_ 'Headphone' 110
set_ 'Speaker'   110
set_ 'Playback'  230        # the DAC's own level, ahead of both outputs

# Capture. The two mics are the point of this HAT.
set_ 'Capture'   150

echo "[audio] $CARD configured"
