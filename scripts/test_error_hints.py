"""Checks for the slash-command error hint in BOT.py.

The behaviour under test is what a user is told when the bot cannot read
something they typed. This matters more than it looks: the alternative is the
catch-all "Something went wrong", which is what the whole ``/meeting create``
bug reported as. A regression here is silent — the command still fails, the
user just stops being able to tell why — so the "returns None for unrelated
errors" cases are asserted as explicitly as the happy path.

Importing BOT for real would start the KeepAlive Flask server on :8080 from a
daemon thread, so that one module is replaced before the import.
"""

import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("APPWRITE_API_KEY", "y")

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

sys.modules.setdefault("KeepAlive", types.SimpleNamespace(keep_alive=lambda: None))
import BOT  # noqa: E402


def check(label, got, want):
    if got != want:
        print(f"  ✗ {label} = {got!r} (want {want!r})")
        return 1
    print(f"  ✓ {label} = {got!r}")
    return 0


def wrapped(inner, *, via_cause=True):
    """An invocation error the way discord.py raises one.

    ``ext.commands`` does ``raise CommandInvokeError(exc) from exc`` while
    ``app_commands`` does ``raise CommandInvokeError(cmd, e) from e`` and keeps
    the cause on ``.original``. Both shapes have to work.
    """
    if via_cause:
        try:
            try:
                raise inner
            except type(inner) as e:
                raise commands.CommandInvokeError(e) from e
        except commands.CommandInvokeError as outer:
            return outer
    return commands.CommandInvokeError(inner)


def main() -> int:
    failures = 0
    hint = BOT._argument_hint

    print("the reported failure: a member name the bot cannot resolve")
    bad = commands.BadArgument('Member "alice" not found.')
    failures += check("text-command wrapper", hint(wrapped(bad)),
                      'Member "alice" not found.')
    failures += check("app_command wrapper (cause + .original)",
                      hint(wrapped(bad, via_cause=False)),
                      'Member "alice" not found.')

    print("\nconversion errors nested deeper are still found")
    deep = wrapped(wrapped(bad))
    failures += check("two wrappers deep", hint(deep),
                      'Member "alice" not found.')

    print("\nunrelated failures stay generic, so internals stay private")
    failures += check("internal error", hint(wrapped(RuntimeError("db down"))),
                      None)
    failures += check("app command's own error",
                      hint(discord.app_commands.CommandInvokeError(
                          types.SimpleNamespace(name="meeting create"),
                          ValueError("bad column"))),
                      None)
    failures += check("plain non-command error", hint(RuntimeError("boom")),
                      None)
    failures += check("nothing at all", hint(None), None)

    print("\nother argument errors are surfaced too")
    for exc, want in (
        (commands.BadArgument(""), "I couldn't read one of the values you entered."),
        (commands.RangeError(1, 10, 50),
         "value must be between 10 and 50 but received 1"),
        (commands.BadArgument('Option "audiance" is unknown.'),
         'Option "audiance" is unknown.'),
    ):
        failures += check(f"{type(exc).__name__}", hint(wrapped(exc)), want)

    print("\na self-referential cause chain terminates instead of hanging")
    loop = commands.CommandInvokeError(RuntimeError("x"))
    loop.original = loop  # type: ignore[attr-defined]
    failures += check("cycle returns promptly", hint(loop), None)

    if failures:
        print(f"\n✗ {failures} FAILURE(S)")
        return 1
    print("\nALL ERROR HINT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())