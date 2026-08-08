# Voice

Built 2026-08-07. Say **"hey jarvis"** near the Pi, ask something, and Hermes
answers — in Discord, and out loud.

```
mic ─► openWakeWord ─► energy endpointer ─► faster-whisper ─► HMAC POST
       "hey jarvis"     (room-calibrated)      base.en        127.0.0.1:8644
       10% of a core                          2.5x realtime          │
                                                                     ▼
                    speak.txt ◄── hermes_voice plugin ◄──────── the agent
                        │                                    (NO terminal)
                        ▼                                          │
                 piper ─► ReSpeaker                                ▼
                                                                Discord
```

Everything runs offline on the Pi. No API key, nothing billed, no audio leaves
the box — the only thing that goes anywhere is the transcript, to an agent on
the same machine over loopback.

---

## Measured on this hardware

| stage | cost |
|---|---|
| Wake word, continuous | **10.0% of one core**, 355 MB RSS (bench said 7.8%; the service does a little more) |
| STT `base.en` | **2.5× realtime** — a 6 s utterance in 2.40 s |
| STT `tiny.en` | 3.6× realtime |
| STT `small.en` | **0.8× — slower than realtime, unusable** |
| TTS `piper` medium | 0.7× realtime (1.54 s of speech in 2.37 s) |

`base.en` is the knee. Anything larger cannot keep up with a person talking,
which disqualifies it however well it reads.

Wake detection is exact on this model: a synthesised **"Hey Jarvis"** scores
**0.997**, unrelated speech **0.000**.

Four services now sit at **2.0 GB of 8 GB**, gateway 163 MB · display 129 MB ·
camera 269 MB · voice 355 MB.

---

## The security position, stated plainly

**A microphone authenticates nobody.** Everything it hears becomes text in an
agent's prompt: a podcast, a television, a guest, a video call playing through
a speaker in the room. None of them are the owner, and the Discord allowlist
covers none of them.

Three things are done about that, in descending order of how much they help.

### 1. The lane is narrowed — verified, not assumed

`platform_toolsets.webhook` strips the agent for this lane:

```
clarify  memory  vision  web  hermes_camera  hermes_display  hermes_voice
```

**`terminal` and `code_execution` are absent.** This was proven before any
voice code was written, in both directions:

| config | `terminal`? |
|---|---|
| webhook default (nothing set) | **no** |
| `webhook: []` | **no** |
| `webhook: [terminal]` *(control)* | **YES** — so the resolver is consulted, not ignored |
| discord default *(control)* | YES, 20 toolsets |

The runtime path was checked too: `webhook` is a first-class `Platform`,
`_platform_config_key()` maps it to `"webhook"`, and `gateway/run.py:19265`
calls `_get_platform_tools()` for every inbound message. The ACP
counter-example in Hermes' docs — where `platform_toolsets` does *not* narrow —
does not apply here.

**Caveat:** `hermes_camera` and `hermes_display` are plugin toolsets and are
force-included whatever the list says. They cannot be removed this way. Both
are this project's own and benign, but "empty" does not mean zero.

**`web` is deliberately included and is the residual risk.** It is what makes
"what's the weather" work, and it is also an exfiltration path: text the mic
picked up could in principle steer a fetch. Remove it from
`platform_toolsets.webhook` if that trade is not worth it to you.

### 2. The transcript is fenced

The route prompt wraps it in delimiters and tells the agent to treat the
contents as data rather than instructions. This is a real mitigation **and the
weakest of the three**, because it is a request to a language model rather than
a mechanism. It is written down here so nobody later mistakes it for a
boundary.

### 3. The rate is bounded

Sliding windows, so they cannot wedge (trap 19): **3 s** minimum gap, **6 per
minute**, **60 per hour**. A television talking to itself all evening reaches
the hourly cap and stops. `tests/test_voice.py` proves each one recovers on its
own with no restart.

### The panel says when the mic is on

`MIC` in the header while listening, `MIC ((` while actually capturing or
transcribing, `MIC?` when it cannot tell. **Unknown fails toward ON**, exactly
like the camera light.

This indicator is **weaker evidence than `CAM`** and the difference is worth
knowing. The camera light reads the kernel's runtime power state for the
sensor, so a crashed or dishonest camera service cannot switch it off. There is
no equivalent kernel fact for a microphone — ALSA exposes nothing comparable —
so this trusts the voice service's own status file. A missing or unreadable
file reads as *unknown*, not *off*.

### What is still true of the transcript

The service **never logs what was said**. Journald here is persistent, and a
permanent record of everything spoken near this microphone is not something to
create by accident. The journal gets length and timing only:

```
[voice] wake (0.99) -- listening
[voice] 3.4s audio -> 47 chars in 1180ms
[voice] delivered to Hermes (HTTP 202)
```

`status.json` carries **state, never content** — same rule as the camera's, and
`tests/test_voice.py` pins it.

---

## The mic is not connected until you connect it

`Input Mixer Boost` on the WM8960 comes up **off**, and with it off the ADC
returns a flat ~1.0 RMS whatever else is set. Every other control reads
correct: LINPUT1/RINPUT1 routed on, `Capture` at +30 dB and unmuted, ADC
unmuted, ALC off.

| | ambient RMS |
|---|---|
| as shipped | **0.98** — silence |
| `+ Input Mixer Boost on` | **146** — real audio |
| `+ LINPUT1 boost +20 dB` | 4471 — clips on speech |

`scripts/audio-setup.sh` sets it, along with `Capture 63` and a high-pass
filter, giving ambient RMS ~178 with 67× headroom.

**How this was missed at first, because the same mistake is easy to repeat:** a
3-second `arecord` showed peaks around 250, which looks exactly like a working
microphone. Reading the stream frame by frame showed all of it in the **first
80 ms** and silence after — the stream-start transient, not sound. A short
recording summarised by its peak cannot tell those apart. Watch the level over
time:

```bash
watch -n1 "python3 -c \"import json;d=json.load(open('/run/user/1000/hermes-voice/status.json'));print(d['level'],d['level_peak'],d['speech_threshold'])\""
```

Silence reads ~250 here. Speaking should push it well past the threshold.

## When the microphone is actually recording

Worth being exact about, because "always listening" and "always recording" are
different things and only one of them is true.

**The mic stream is open the whole time the service runs.** A wake word cannot
work otherwise — something has to be listening to notice the phrase. Audio is
read in 80 ms frames, fed to the detector, held in a 0.5 s ring so the first
syllable after the wake is not clipped, and then discarded. **Nothing is
written to disk, ever.**

A *capture* — the part that becomes a transcript — starts only on a wake and
ends on the FIRST of:

| | |
|---|---|
| `SILENCE_END` | 0.8 s of continuous quiet, **whether or not anything was said** |
| `LEAD_SILENCE` | 2 s with no speech at all — a false wake gives up fast |
| `MAX_UTTERANCE` | 10 s hard ceiling, logged loudly when hit |

`status.json` publishes `capturing_s` while a turn is in progress, so how long
the mic has actually been recording is answerable from outside at any moment.

### One wake, one utterance

There is **no follow-up window.** Once Hermes has answered, the turn is closed
and the wake word is required again. That is deliberate: a window where it
keeps listening for more is common in assistants and is exactly what makes
people unsure whether the microphone is still on.

Closing a turn does three things together, and all three matter:

1. **Resets the detector** — otherwise the same utterance keeps scoring above
   threshold and fires again immediately.
2. **Empties the pre-roll ring** so nothing from this turn leaks into the next.
3. **Discards everything ALSA buffered while the service was busy.** This is
   the one that is easy to miss. Between the end of a capture and the return to
   listening the service is transcribing, posting and possibly speaking, and it
   is not reading frames through any of it — so the driver quietly buffers the
   lot. Without draining, the next wake's pre-roll begins with whatever was
   said while Hermes was answering, **including its own reply out of the
   speaker**, presented as if it had just been spoken.

The journal says how much was thrown away, which is a direct measure of how
long the microphone went unattended:

```
[voice] turn closed; discarded 4.3s captured while busy -- say the wake word again
```

`tests/test_voice.py` pins that every exit from a turn goes through the same
door — nothing captured, empty transcript, rate limited, delivered, and
finishing speaking. A path that skipped it would leave the detector primed and
the buffer filling.

### Two bugs that made captures run long

Both were real and both are pinned by `tests/test_voice.py`, verified to fail
against the old code.

1. **A false wake with nobody talking recorded to the ceiling.** The loop only
   ended on silence once speech had *already* been heard (`quiet_for >=
   SILENCE_END and spoke_for > 0`), so a wake firing on a television in an
   empty room could not end early — the cheapest case was accidentally the most
   expensive. Now silence ends a capture regardless, plus the `LEAD_SILENCE`
   bail.
2. **The speech threshold was measured once at startup and never updated.** A
   room that later got louder read as continuous speech, the silence timer
   never filled, and every capture ran to the ceiling. The floor is now a
   rolling median of the last few seconds, fed only while listening — folding
   a capture's own speech back in would make the endpointer progressively
   deafer.

The ceiling is also counted in `status.json` as `capped`. If that number
climbs, the threshold is wrong for the room rather than the person having
talked for a long time.

### Bounded on audio, not on wall clock

The capture limit counts *sound captured*, not seconds elapsed. They are nearly
identical while the pipe delivers in real time, and they diverge exactly when
it matters: under load `arecord` returns a burst of buffered frames, and a
wall-clock ceiling would let far more than 10 s of audio through while
believing it had stopped in time.

### If you would rather it not listen at all

```bash
systemctl --user stop hermes-voice && systemctl --user disable hermes-voice
```

That closes the stream, ends the `arecord` process, and the panel's MIC light
goes to a positively-observed off. It will not come back on a reboot.

The alternative that removes always-on listening entirely is **push-to-talk on
the HAT's button** (GPIO 17, free and verified readable). That drops the wake
word — a physical press replaces it — and the mic stays closed at rest. Not
built; it is a real option if the always-open stream is not acceptable.

## Setup

```bash
./scripts/install-audio.sh      # mixer + WirePlumber exclusion (once)
./scripts/install-voice.sh      # venv, models, ~5 min
systemctl --user enable --now hermes-voice
```

`install-voice.sh` builds a **separate venv** from `cv-venv`. That one is
`--system-site-packages` for picamera2 and is verified not to shadow the system
numpy; voice needs ctranslate2 and onnxruntime, which have their own opinions,
and the camera service must not inherit them. Two venvs, no shared blast
radius. The renderer's no-dependencies property is untouched.

**Do not `apt install piper`.** Debian's `piper` package is the **Piper mouse
configuration GUI** — it installs `ratbagd` and has nothing to do with speech.
Running `piper --model ...` then fails with `Unknown option --model`, which
reads like version skew rather than the wrong program entirely. The TTS engine
is `piper-tts` on PyPI, in the venv, and `voice/speak.py` resolves the binary
from `sys.executable` rather than `PATH` for exactly this reason.

---

## Configuration

| variable | default | |
|---|---|---|
| `HERMES_VOICE_WAKE` | `hey_jarvis` | also `alexa`, `hey_mycroft`, `hey_marvin` |
| `HERMES_VOICE_WAKE_THRESHOLD` | `0.5` | raise if it triggers on the television |
| `HERMES_VOICE_STT` | `base.en` | `tiny.en` is faster and worse |
| `HERMES_VOICE_CARD` | `plughw:wm8960soundcard` | |

**There is no pretrained "hey hermes."** openWakeWord ships `alexa`,
`hey_jarvis`, `hey_marvin`, `hey_mycroft`. A custom phrase needs a training run
on synthetic speech, best done off-Pi; the resulting `.onnx` drops straight in
via `HERMES_VOICE_WAKE`.

### Kill switches, ascending enforceability

```bash
touch ~/.config/hermes-pi/voice.disabled   # owner mute, survives reboot
systemctl --user stop hermes-voice         # real off switch
systemctl --user mask hermes-voice
```

Same honest caveat as the camera's: **none of these are enforceable against an
agent that has a shell.** The voice lane does not have one, but the Discord
lane does. They are conveniences for the owner, not security controls. The real
controls are the Discord allowlist and physical access.

---

## Operating it

```bash
systemctl --user status hermes-voice
journalctl --user -u hermes-voice -f
python3 -c "import json;print(json.dumps(json.load(open('/run/user/1000/hermes-voice/status.json')),indent=2))"

# test the output path without saying anything
~/.local/share/hermes-pi/voice-venv/bin/python -m voice --say "testing"
```

| Symptom | Cause |
|---|---|
| `wake_error` set | wrong venv — the unit must use `voice-venv/bin/python` |
| `stt_error` set | model not fetched; re-run `scripts/install-voice.sh` |
| `tts_error` set | `piper-tts` missing, or the voice model was not downloaded |
| Never wakes | say it as one phrase; check `wake_ready`; lower the threshold |
| Wakes constantly | raise `HERMES_VOICE_WAKE_THRESHOLD` |
| Cuts you off mid-sentence | `SILENCE_END` (0.8 s) or a noise floor measured while the room was loud — restart it in a quiet room |
| Delivered but no reply | check the gateway; Discord may be down independently |
| Hears itself | it should not — the wake detector is skipped while speaking |

**Nothing spoken is recoverable after the fact.** Audio is never written to
disk; the utterance lives in memory for the seconds it takes to transcribe and
is then gone.
