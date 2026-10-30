"""The bot must derive Minecraft levels from the same curve as the server.

``scripts/preview_progression.py`` is the single definition of the curve. This
test walks all 100 levels against it so the Discord bot, the Java server and the
documentation can never quote three different numbers for the same level.

It also pins the rule that levels are derived, never read: the server stopped
publishing ``lv_*`` because a published level is a snapshot of whichever curve
was live at sync time, and the curve is retunable while the server runs.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import preview_progression as canonical  # noqa: E402

from data import mc_curve  # noqa: E402

BASE = canonical.DEFAULT_BASE
BAND = canonical.DEFAULT_BAND
GROWTH = canonical.DEFAULT_GROWTH
TUTORIAL = canonical.DEFAULT_TUTORIAL


def canonical_cumulative(level: int, growth: float = GROWTH) -> int:
    return canonical.cumulative_xp(level, BASE, BAND, growth, TUTORIAL)


@pytest.mark.parametrize("level", range(1, 101))
def test_cumulative_matches_the_canonical_script(level):
    assert mc_curve.cumulative_xp(level) == canonical_cumulative(level)


@pytest.mark.parametrize("level", range(1, 101))
def test_level_inverts_cumulative_exactly(level):
    assert mc_curve.level_for_xp(canonical_cumulative(level)) == level


def test_level_one_is_free_and_is_the_floor():
    assert canonical.cumulative_xp(1, BASE, BAND, GROWTH, TUTORIAL) == 0
    assert mc_curve.level_for_xp(0) == 1
    assert mc_curve.level_for_xp(-50) == 1


def test_peak_cost():
    assert mc_curve.cumulative_xp(100) == 429_440


def test_levels_continue_past_the_reward_cap():
    assert mc_curve.level_for_xp(429_440) == 100
    assert mc_curve.level_for_xp(429_440 * 4) > 100


def test_a_retune_moves_bot_levels_too():
    """The bot reads the live document, so a retune must retune its display."""
    steep = dict(mc_curve.DEFAULT_CURVE, growth=1.50)
    xp = canonical_cumulative(30)
    assert mc_curve.level_for_xp(xp) == 30
    assert mc_curve.level_for_xp(xp, steep) == 29


def test_defaults_match_the_canonical_constants():
    defaults = mc_curve.DEFAULT_CURVE
    assert defaults["tutorial_base"] == canonical.DEFAULT_TUTORIAL
    assert defaults["base"] == canonical.DEFAULT_BASE
    assert defaults["band_size"] == canonical.DEFAULT_BAND
    assert defaults["growth"] == canonical.DEFAULT_GROWTH
    assert defaults["income_growth"] == canonical.INCOME_GROWTH
    assert defaults["base_hourly_xp"] == canonical.DEFAULT_HOURLY_XP
    assert defaults["max_reward_level"] == canonical.MAX_REWARD_LEVEL


def test_row_ids_are_appwrite_safe():
    assert mc_curve.row_id_for("rewards:perks") == "rewards-perks"
    assert mc_curve.row_id_for("tiers:miner") == "tiers-miner"
    assert mc_curve.row_id_for("curve") == "curve"


def test_progress_bar_is_bounded():
    for level, xp in ((1, 0), (1, 149), (10, 1350), (30, 20_000), (150, 10_000_000)):
        bar = mc_curve.progress_bar(xp, level)
        assert len(bar) == 10
        assert set(bar) <= {"█", "░"}


def test_profile_ignores_stale_persisted_levels():
    """Behavioural guard, not a text scan.

    The row is handed deliberately wrong lv_* values and a wrong power. A profile
    that still read them would show level 99; deriving from experience shows the
    real level. This fails loudly if the lv_* dependency is ever reintroduced.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from cogs.mc_economy import _Profile

    row = {
        "username": "stale",
        "gameplay_xp": 20_710,
        "level": 99,
        "power": 4242,
        "lv_miner": 99,
        "lv_crafter": 99,
        "xp_miner": canonical_cumulative(30),
        "xp_crafter": canonical_cumulative(5),
    }
    profile = _Profile(row, discord_xp=0)

    assert profile.level == 30, "must ignore the stale level column"
    assert profile.jobs["miner"][0] == 30, "must ignore the stale lv_miner column"
    assert profile.jobs["crafter"][0] == 5, "must ignore the stale lv_crafter column"
    assert profile.power == 30 + 5 + 6, "must derive power, not read the column"


def test_profile_derives_job_levels_from_experience():
    """The profile object itself, not just the source text."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from cogs.mc_economy import _Profile

    row = {"username": "tester", "gameplay_xp": 20_710, "power": 999,
           "xp_miner": canonical_cumulative(30), "xp_crafter": canonical_cumulative(5)}
    profile = _Profile(row, discord_xp=9400)

    assert profile.level == 30
    assert profile.jobs["miner"][0] == 30
    assert profile.jobs["crafter"][0] == 5
    # Power is derived, not read: the row claimed 999.
    assert profile.power == 30 + 5 + 6
    # The two curves are unrelated and must never be summed.
    assert profile.discord_level == 9
    assert profile.level != profile.discord_level



def test_profile_level_follows_total_job_xp_not_afk():
    """The ladder follows job XP; gameplay_xp is the AFK counter, not a route."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from cogs.mc_economy import _Profile

    job_xp = canonical_cumulative(30)
    row = {"username": "miner", "gameplay_xp": 999_999, "xp_miner": job_xp}
    profile = _Profile(row, discord_xp=0)
    assert profile.level == 30, "level must come from job XP"
    assert profile.minecraft_xp == 999_999, "AFK XP is still reported"


def test_reward_formula_matches_the_documented_budget():
    """The seeded formula must reconcile with the 57,750 R faucet budget."""
    rows_path = (Path(__file__).resolve().parent.parent / "scripts" / "preview_progression.py")
    assert rows_path.exists()
    total = sum(
        canonical.level_reward(level)
        + (canonical.milestone_reward(level) if level % 10 == 0 else 0)
        for level in range(1, 101)
    )
    assert total == 57_750


class TestRewardCadences:
    """The cosmetic cadence is every 5th, the milestone every 10th.

    Cosmetic levels are the odd multiples of 5 (5, 15, 25 ...), which never
    coincide with a milestone. Keying the row off the milestone alone dropped
    all nine of them and the gap was invisible until the Java side asked for
    them.
    """

    @staticmethod
    def _milestones():
        rows = canonical.rows()
        by_level = rows["rewards:milestones"]["byLevel"]
        return {int(level): entry for level, entry in by_level.items()}

    def test_both_cadences_land_at_every_fifth_level(self):
        levels = set(self._milestones())
        expected = set(range(5, 101, 5))
        assert expected <= levels, f"missing levels {sorted(expected - levels)}"

    def test_cosmetics_survive_where_there_is_no_milestone(self):
        milestones = self._milestones()
        for level, cosmetic in canonical.COSMETICS.items():
            entry = milestones[level]
            assert entry["cosmetic"] == cosmetic, f"level {level} lost its cosmetic"
            assert entry["r"] == 0 or entry["r"] > 0

    def test_every_cosmetic_level_has_a_milestone_entry_and_vice_versa(self):
        milestones = self._milestones()
        for level in range(5, 101, 5):
            entry = milestones[level]
            has_reward = bool(entry["cosmetic"]) or bool(entry["unlock"])
            assert has_reward, f"level {level} grants nothing"

    def test_milestone_currency_still_only_on_every_tenth(self):
        milestones = self._milestones()
        for level, entry in milestones.items():
            if entry["r"]:
                assert level % 10 == 0, f"level {level} pays milestone R off-cadence"

    def test_ladder_total_is_unaffected_by_cosmetic_rows(self):
        """Adding cosmetic rows must not change the 57,750 R budget."""
        total = sum(
            canonical.level_reward(level) + canonical.milestone_reward(level)
            for level in range(1, 101)
        )
        assert total == 57_750
