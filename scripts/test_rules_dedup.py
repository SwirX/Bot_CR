#!/usr/bin/env python3
"""Tests for the rules board dedup (cogs.rules).

Drives ``Rules.send_rules_image`` against stubbed channels to prove:
- a rules post that is already current is left alone (no repost, no churn),
- an outdated one (pre-edit footer, older rules text, ...) is edited in place
  so it keeps its position,
- stale duplicates from older boots are swept, and
- an empty channel just gets the fresh post.

No network, no Discord — messages/channels are local stubs.

Usage:
    BOT_TOKEN=x APPWRITE_API_KEY=y python scripts/test_rules_dedup.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import asyncio  # noqa: E402
import types  # noqa: E402

import discord  # noqa: E402

import config  # noqa: E402
from cogs.rules import Rules  # noqa: E402


class _Msg:
    def __init__(self):
        self.author = _Msg  # patched by the caller against the bot user
        self.embeds: list = []
        self.edited = False
        self.deleted = False

    async def edit(self, **kw):
        self.edited = True
        if "embed" in kw:
            self.embeds = [kw["embed"]]

    async def delete(self):
        self.deleted = True


class _Chan:
    def __init__(self, messages, user):
        self.messages = list(messages)  # newest-first, like Discord history
        self.name = config.CHANNEL_RULES
        self.sent = []
        self.user = user

    def history(self, limit=100):
        async def gen():
            for m in self.messages:
                yield m
        return gen()

    async def send(self, embed=None, **_kw):
        msg = _Msg()
        msg.author = self.user
        msg.embeds = [embed]
        self.sent.append(msg)
        self.messages = [msg] + self.messages
        return msg


def _bot_for(chan):
    user = _Msg()  # the bot's own "identity"; messages must reference it
    for m in list(chan.messages) + chan.sent:
        m.author = user
    return types.SimpleNamespace(
        guilds=[types.SimpleNamespace(text_channels=[chan])], user=user)


def _current_msg():
    """A post whose embed serialises identically to the current builder."""
    m = _Msg()
    m.embeds = [discord.Embed.from_dict(Rules._build_embed().to_dict())]
    return m


def _outdated_msg():
    """A post with the pre-edit footer — must count as outdated."""
    m = _Msg()
    embed = Rules._build_embed()
    embed.set_footer(text="Have a Fun asahbi✨🖤")
    m.embeds = [discord.Embed.from_dict(embed.to_dict())]
    return m


def scenario(name, messages, want_send, want_edit, want_delete):
    chan = _Chan(messages, user=None)
    asyncio.run(Rules(_bot_for(chan)).send_rules_image())
    got_send, got_edit = len(chan.sent), sum(m.edited for m in messages)
    got_delete = sum(m.deleted for m in messages)
    assert got_send == want_send, f"{name}: sent {got_send} != {want_send}"
    assert got_edit == want_edit, f"{name}: edits {got_edit} != {want_edit}"
    assert got_delete == want_delete, f"{name}: deletes {got_delete} != {want_delete}"
    print(f"  ✓ {name}: sent={got_send} edits={got_edit} deletes={got_delete}")


def check(label, got, want):
    assert got == want, f"{label}: got {got!r}, want {want!r}"
    print(f"  ✓ {label}")


def main():
    check("embed == its own from_dict round-trip  # equality detector",
          discord.Embed.from_dict(Rules._build_embed().to_dict()).to_dict(),
          Rules._build_embed().to_dict())

    scenario("up-to-date post → untouched", [_current_msg()], 0, 0, 0)
    scenario("outdated post → edited in place", [_outdated_msg()], 0, 1, 0)
    scenario("empty channel → posted once", [], 1, 0, 0)
    scenario("two outposts → newest edited, stray swept",
             [_outdated_msg(), _outdated_msg()], 0, 1, 1)
    scenario("current post + stray old duplicate → current kept, stray swept",
             [_current_msg(), _outdated_msg()], 0, 0, 1)

    print("ALL RULES DEDUP CHECKS PASSED")


if __name__ == "__main__":
    main()
