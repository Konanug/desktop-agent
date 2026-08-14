# Contributing

This is a personal hardware project, so the conventions below are mostly notes
to my future self.

## The rules that matter

**Measure, don't assume.** Every performance number in `docs/` came from a
counter on the real machine. Several plausible-sounding estimates in this
project's history turned out wrong by more than the thing they were estimating.

**The panel never invents state.** Anything shown must trace to a real event or
a direct observation. When a status file and the system disagree, the system
wins.

**Read `CLAUDE.md` first.** It is the list of mistakes already made here, with
the measurement that exposed each one. It will save you more time than the
architecture docs.

## Tests

```bash
for t in tests/test_*.py; do python3 "$t"; done
```

No pytest, no fixtures directory, no runner. Each module is standalone so it
works on a Pi with nothing installed. Hardware is stubbed, not skipped.

**A regression test must be verified to FAIL against the broken code** before
it counts. Several tests here were written, passed immediately, and turned out
to be asserting the wrong thing.

## Commits

Explain *why*, especially when a measurement changed the design. The diff shows
what changed; the message is the only place the reason survives.
