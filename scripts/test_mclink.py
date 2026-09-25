"""Unit checks for the mc-link crypto twin + credential helpers.

Run:  .venv-local/bin/python scripts/test_mclink.py
Hermetic: no network, no Appwrite, no config import needed (unlike smoke).
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.exceptions import InvalidTag  # noqa: E402

from cogs._mc_crypto import (  # noqa: E402
    CODE_ALPHABET, TEMP_ALPHABET, CipherBox, hash_otp, load_ips, mask_ip,
    new_link_code, new_temp_password, parse_iso, save_ips,
)

KEY = "47264f728c9c676b64868a7bd3078d949d30d09aca594b99f3f42e11f1c04654"
KEY_BYTES = bytes.fromhex(KEY)


class CipherBoxTests(unittest.TestCase):
    def test_key_is_32_bytes(self):
        self.assertEqual(len(KEY_BYTES), 32)

    def _box(self, key_hex: str = KEY) -> CipherBox:
        return CipherBox(key_hex)

    def test_round_trip(self):
        box = self._box()
        token = box.seal("SwirXwasTaken", "hunter2-secret")
        self.assertEqual(box.open("SwirXwasTaken", token), "hunter2-secret")

    def test_layout_is_iv_tag_ct_hex(self):
        box = self._box()
        token = box.seal("hatim", "abcd")
        raw = bytes.fromhex(token)
        # iv(12) + tag(16) + ciphertext.
        self.assertGreaterEqual(len(raw), 28)
        self.assertEqual(len(raw), 12 + 16 + len(b"abcd"))

    def test_aad_mismatch_fails(self):
        box = self._box()
        token = box.seal("imibellaa", "pw")
        with self.assertRaises(InvalidTag):
            box.open("Imibellaa", token)  # case is significant in AAD

    def test_tamper_fails(self):
        box = self._box()
        token = bytearray.fromhex(box.seal("Yasser_Yjj", "pw"))
        token[20] ^= 0x01  # flip a bit inside the tag/ciphertext
        with self.assertRaises(InvalidTag):
            box.open("Yasser_Yjj", bytes(token).hex())

    def test_wrong_key_fails(self):
        other = self._box("0" * 64)
        token = other.seal("Steve", "pw")
        with self.assertRaises(InvalidTag):
            self._box().open("Steve", token)

    def test_rejects_short_ciphertext(self):
        box = self._box()
        with self.assertRaises(ValueError):
            box.open("Steve", "00" * 10)

    def test_key_length_validation(self):
        with self.assertRaises(ValueError):
            CipherBox("0" * 2)  # not 32 bytes
        with self.assertRaises(ValueError):
            CipherBox("not-hex-at-all")

    def test_empty_aad_rejected(self):
        box = self._box()
        with self.assertRaises(ValueError):
            box.seal("", "pw")


class CredentialMintTests(unittest.TestCase):
    def test_link_code_shape(self):
        for _ in range(200):
            code = new_link_code()
            self.assertEqual(len(code), 8)  # contract §5.2: OTPs are ≥8 chars
            self.assertTrue(all(c in CODE_ALPHABET for c in code),
                            msg=f"{code!r} has chars outside the alphabet")

    def test_link_code_length_param(self):
        self.assertEqual(len(new_link_code(6)), 6)  # pair keys may be shorter
        self.assertEqual(len(new_link_code(12)), 12)

    def test_temp_password_shape(self):
        for _ in range(200):
            pw = new_temp_password()
            self.assertEqual(len(pw), 12)
            self.assertTrue(all(c in TEMP_ALPHABET for c in pw),
                            msg=f"{pw!r} has chars outside the alphabet")


class OtpHashTests(unittest.TestCase):
    """The bot side of the plugin's BCrypt.checkpw contract (cost 12, $2a$)."""

    def test_hash_prefix_and_cost(self):
        h = hash_otp("ABCD2349")
        self.assertTrue(h.startswith("$2a$12$"), msg=f"unexpected prefix: {h}")

    def test_round_trip(self):
        import bcrypt
        code = new_link_code()
        h = hash_otp(code)
        self.assertTrue(bcrypt.checkpw(code.encode(), h.encode()))

    def test_wrong_code_fails(self):
        import bcrypt
        h = hash_otp("ABCD2349")
        self.assertFalse(bcrypt.checkpw(b"ABCD2350", h.encode()))

    def test_hashes_differ_per_code(self):
        self.assertNotEqual(hash_otp("ABCD2349"), hash_otp("ABCD2349"),
                            "bcrypt salts must differ per hash")


class IpHelperTests(unittest.TestCase):
    def test_mask_ip_ipv4(self):
        self.assertEqual(mask_ip("203.0.113.42"), "203.0.113.***")

    def test_mask_ip_non_ipv4_passthrough(self):
        self.assertEqual(mask_ip("2001:db8::1"), "2001:db8::1")
        self.assertEqual(mask_ip("nonsense"), "nonsense")

    def test_load_save_round_trip(self):
        rows = [{"ip": "1.2.3.4", "seen_at": "2026-09-19T02:00:00"},
                {"ip": "5.6.7.8", "seen_at": "2026-09-18T10:00:00"}]
        raw = save_ips(rows)
        self.assertEqual(load_ips(raw), rows)

    def test_load_ips_tolerates_garbage(self):
        self.assertEqual(load_ips(None), [])
        self.assertEqual(load_ips(""), [])
        self.assertEqual(load_ips("not json"), [])
        self.assertEqual(load_ips('{"a": 1}'), [])  # object, not a list


class ParseIsoTests(unittest.TestCase):
    def test_z_and_offset_equivalent(self):
        from datetime import datetime, timezone

        a = parse_iso("2026-09-19T02:00:00.000Z")
        b = parse_iso("2026-09-19T02:00:00.000+00:00")
        self.assertIsNotNone(a)
        self.assertEqual(a, b)
        self.assertEqual(
            a.timestamp(),
            datetime(2026, 9, 19, 2, tzinfo=timezone.utc).timestamp())

    def test_python_marker_parses(self):
        self.assertIsNotNone(parse_iso("2026-09-19T02:00:00.123456+00:00"))

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_iso(None))
        self.assertIsNone(parse_iso(""))
        self.assertIsNone(parse_iso("yesterday"))


# ── /mclink DM-failure fallback (ephemeral vs prefix) ──────────────────
from unittest import mock  # noqa: E402

from cogs.mclink import McLink  # noqa: E402


class _FakeAuthor:
    def __init__(self, user_id: int):
        self.id = user_id
        self.display_name = "Steve_Test"
        self.name = "Steve_Test"


class _FakeInteraction:
    def __init__(self, locale: str = "en-US"):
        self.locale = locale


class _FakeCtx:
    """Records ctx.send(invoker-visible vs ephemeral) like the real one."""

    def __init__(self, *, interaction):
        self.author = _FakeAuthor(420)
        self.interaction = interaction
        self.sent: list[tuple[str, dict]] = []

    async def send(self, content=None, *, ephemeral=False, **kwargs):
        self.sent.append((content or "", {"ephemeral": ephemeral, **kwargs}))


class McLinkFlowTests(unittest.TestCase):
    def _cog(self) -> McLink:
        cog = McLink(None)
        cog._configured = lambda: True
        cog._dm = mock.AsyncMock(return_value=False)  # DMs closed
        return cog

    def _run(self, ctx: _FakeCtx) -> None:
        async def go():
            from cogs.mclink import resolve_member_lang, store

            cog = self._cog()
            with mock.patch("cogs.mclink.resolve_member_lang",
                            new=mock.AsyncMock(return_value="en")), \
                 mock.patch.object(store, "mc_user_linked",
                                   new=mock.AsyncMock(return_value=False)), \
                 mock.patch.object(store, "mc_account_linked",
                                   new=mock.AsyncMock(return_value=False)), \
                 mock.patch.object(store, "mc_create_pair",
                                   new=mock.AsyncMock(return_value="pair-1")) as create, \
                 mock.patch.object(store, "mc_delete_pair",
                                   new=mock.AsyncMock()) as delete:
                await cog.mclink.callback(cog, ctx, "Steve_Test")
                self.create, self.delete = create, delete
        asyncio.run(go())

    def test_slash_dm_failure_falls_back_to_ephemeral(self):
        ctx = _FakeCtx(interaction=_FakeInteraction())  # slash invocation
        self._run(ctx)
        # The code must still be delivered privately — ephemeral reply with
        # the pairing text, and the pair row kept alive (no delete).
        self.assertEqual(len(ctx.sent), 1)
        content, kwargs = ctx.sent[0]
        self.assertTrue(kwargs["ephemeral"])
        self.assertIn("mcverify", content)
        self.delete.assert_not_awaited()

    def test_prefix_dm_failure_drops_pair_row(self):
        ctx = _FakeCtx(interaction=None)  # legacy !mclink, no private channel
        self._run(ctx)
        # No private surface → the code can't be delivered safely; expire the
        # pending pair and tell the member what to do instead.
        self.assertEqual(len(ctx.sent), 1)
        content, kwargs = ctx.sent[0]
        self.assertFalse(kwargs["ephemeral"])
        self.assertNotIn("mcverify", content)
        self.delete.assert_awaited_once_with("pair-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)