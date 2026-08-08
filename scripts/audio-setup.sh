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
set_ 'Headphone' 120
set_ 'Speaker'   127        # max
set_ 'Playback'  255        # max; the DAC's own level, ahead of both outputs

# CLASS-D BOOST, and the reason the JST speaker was too quiet. `Speaker DC` and
# `Speaker AC` are the WM8960's own speaker-driver gain and they come up at
# 0 of 5 -- so the volume control was already near maximum while the amplifier
# behind it was doing nothing. Small passive speakers on the JST header need
# this; the headphone jack does not, which is why the jack sounded fine and the
# JST did not.
#
# At 5/5 into a small driver this WILL clip on loud passages. That is the
# owner's explicit choice ("crank it to the maximum"); drop both to 3 if it
# sounds harsh rather than merely loud.
set_ 'Speaker DC' 5
set_ 'Speaker AC' 5

# CAPTURE. The two mics are the point of this HAT, and this block is the
# difference between them working and not.
#
# `Input Mixer Boost` IS THE SWITCH THAT CONNECTS THE MICS AT ALL. It comes up
# OFF, and with it off the ADC produces a flat ~1.0 RMS -- digital silence --
# no matter what every other capture control says. Every one of them looked
# correct: LINPUT1/RINPUT1 routed on, Capture at +30 dB and unmuted, ADC
# unmuted, ALC off. MEASURED, same room, back to back:
#
#     as shipped                 ambient rms   0.98    (silence)
#     + Input Mixer Boost on     ambient rms 146       (real audio)
#     + LINPUT1 boost +20 dB     ambient rms 4471      (far too hot)
#
# This is why "the mics are live" was reported wrongly earlier. A 3 s arecord
# showed peaks around 250, which looked like a working microphone; reading the
# stream frame by frame showed all of it in the FIRST 80 ms and silence after.
# That burst is the stream-start transient, not sound. A short recording
# summarised by its peak cannot tell those apart -- look at the level over
# time, or a dead mic will pass.
#
# Gain staging, measured: LINPUT1 boost at 0 dB with Capture at 63 gives
# ambient rms 178 and 67x headroom before clipping, which leaves plenty of room
# for speech. Raising the boost one step costs 6x the headroom for signal that
# is already ample.
set_ 'Left Input Mixer Boost'    on
set_ 'Right Input Mixer Boost'   on
set_ 'Left Input Boost Mixer LINPUT1'  0
set_ 'Right Input Boost Mixer RINPUT1' 0
set_ 'Capture'   63
# A high-pass filter costs nothing and removes the DC/rumble that otherwise
# sits under everything the wake word sees.
set_ 'ADC High Pass Filter' on

echo "[audio] $CARD configured"
