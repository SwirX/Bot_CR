## Goal
Make tracked meetings private, role aware, scheduled when requested, and automatically closed after ten empty minutes while retaining durable attendance reports.

## Constraints
- Keep the existing Appwrite meeting/session tables and existing attendance data readable.
- Keep `/meeting start|end|lock|unlock|list|last` available.
- Never expose meeting history or attendance to ordinary members.

## Acceptance criteria
- Archon, President, Vice President, Manager, and cell chiefs can start meetings; only the first four and IT chief can read reports.
- Default audience is all club members; bureau organizers can choose bureau, bureau plus chiefs, bureau plus cell members, or everyone. Chiefs default to their cell and senior roles.
- Meetings use a private, bot-created voice channel, DM invited members, and record every join/leave/rejoin.
- A scheduled meeting starts at its specified time, survives a restart, and claims the bot's voice connection ten minutes ahead of time.
- A live meeting closes and deletes its channel after ten minutes without human occupants.
- Existing tests and lint pass; new behavior has focused tests.

## Steps
1. Update role and audience policy in `cogs/_perms.py` and `cogs/_meetings.py`; verify policy tests.
2. Add scheduling and lifecycle helpers around the existing meeting store and cog; verify focused tests.
3. Restrict reports, send invitations, and document commands; run test suite and lint.

## Risks
- Discord voice connections and DMs can fail independently of attendance; failures must be logged without corrupting the meeting.
- Appwrite has one live meeting at a time; scheduling must not create another live row until activation.

## Out of scope
- Recording audio, which requires a separate consent and storage design.

## History blueprint
- Branch `feat/sx-meeting-schedule-privacy` from `master`.
- Commit `feat(meetings): enforce meeting roles and private audiences` for policy,
  audience resolution, and their focused checks.
- Commit `feat(meetings): schedule and close tracked voice rooms` for lifecycle,
  invitations, report gating, and their checks.
- Commit `fix(meetings): keep scheduler alive after voice timeouts` for the
  timeout failure path found during review.
- Commit `docs(meetings): explain scheduling and access` for the command guide,
  repository notes, and this plan.
- Follow-up `fix(meetings): validate scheduled organizers and audiences` closes
  the role-revocation and custom-role size cases found in final review.
- Follow-up `fix(meetings): restrict legacy private rooms to organizers` closes
  the untracked `/meeting create` permission gap and hides rooms at creation.
- Follow-up `fix(meetings): wake scheduler at meeting start` removes the
  healthy-path 30-second start delay.
- Keep the branch available for review; integration into `nightly` follows
  deployment testing against Discord and Appwrite.

## Verification (2026-10-30)
- Meeting permission, helper and cog standalone checks: passed.
- Lifecycle unittest (eight scenarios): passed.
- Other offline `scripts/test_*.py` runners: passed after fixing the lifecycle
  script's direct-run import path.
- Changed production files and new test lint: passed.
- Repository-wide Ruff has 168 existing findings. `scripts/smoke_test.py` is
  blocked by an existing `/mc` command registration conflict in `cogs.minecraft`.
- Live Discord gateway and Appwrite table integration: not verified locally.
