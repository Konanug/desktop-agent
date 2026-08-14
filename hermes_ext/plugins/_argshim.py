"""Read a tool argument whichever way Hermes chose to pass it.

THIS EXISTS BECAUSE GUESSING COST TWO BUGS IN A ROW. Hermes has been observed
calling a handler BOTH ways:

    handler({"text": "hi"})           # arguments in the positional dict
    handler({}, text="hi")            # empty dict, arguments as kwargs

Declaring `def f(text="")` broke the first (the whole dict bound to `text`, and
piper read the word "text" aloud). Declaring `def f(args: dict)` broke the
second (args was empty, so every reply became "nothing to say"). Both failures
were silent-ish and both reached the owner before being noticed.

So stop guessing: look in both places, prefer the explicit kwarg, and give
`args` a default so a kwargs-only call cannot raise TypeError. This is three
lines and it removes an entire class of bug that has now bitten twice.
"""

from __future__ import annotations


def arg(args, kwargs: dict, name: str, default=None):
    if isinstance(args, dict) and args.get(name) not in (None, ""):
        return args[name]
    got = kwargs.get(name)
    return default if got in (None, "") else got
