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
- Follow-up `fix(meetings): reclaim scheduled voice after restart` rechecks
  a saved preparation marker after the bot reconnects.
- Follow-up `fix(meetings): preserve partial grants for rollback` prevents
  permission leaks when Discord rejects a member overwrite mid-start.
- Keep the branch available for review; integration into `nightly` follows
  deployment testing against Discord and Appwrite.

## Verification (2026-10-30)
- Meeting permission, helper and cog standalone checks: passed.
- Lifecycle unittest (ten scenarios): passed.
- Other offline `scripts/test_*.py` runners: passed after fixing the lifecycle
  script's direct-run import path.
- Changed production files and new test lint: passed.
- Repository-wide Ruff has 168 existing findings. `scripts/smoke_test.py` is
  blocked by an existing `/mc` command registration conflict in `cogs.minecraft`.
- Live Discord gateway and Appwrite table integration: not verified locally.

## Release follow-up (2026-10-31)
- Remove the duplicate Minecraft menu `/mc` command while retaining `/minecraft`
  and the economy `/mc` group. Update its command docs and verify cog loading.
- Preserve the three uncommitted files on the OCI host before updating its
  checkout. Run the smoke test before and after restarting `bot-cr.service`.
- History blueprint: commit the duplicate fix on
  `fix/sx-mc-command-collision`, forked from the meeting feature. Merge current
  `origin/master` into `nightly` with `--no-ff`, then merge the fix branch into
  `nightly` with `--no-ff`. Push `nightly` and deploy that integration head.
  Promote `nightly` into `master` with `--no-ff` only after deployment checks.

## Meeting command repair (2026-10-31)
- Remove the unnecessary Move Members preflight from private room creation and
  tracked meeting start; moving occupants remains optional.
- Remove the visible Default audience choice while preserving omission behavior:
  everyone for bureau, own cell plus bureau for a chief.
- Add a bot-developer-only Test audience containing only the invoker as a human
  attendee. The bot retains voice access. No other member receives an invite.
- Verify command registration, audience isolation, permissions and lifecycle;
  deploy to the existing OCI service and check startup logs.
- History blueprint: `fix/sx-meeting-create-permission` forks from `master` and
  contains the permission repair. `feat/sx-meeting-test-audience` forks from
  the repair branch and contains the audience changes and focused checks.
  Merge the repair branch into `nightly` first, then the audience branch, both
  with `--no-ff`; promote tested `nightly` to `master` with `--no-ff`.
- Follow-up `fix/sx-meeting-audience-suggestions` forks from integrated
  `nightly`: use role-aware autocomplete so the Test choice is shown only to
  the Bot Developer, then merge it with `--no-ff` before promotion.
- Follow-up `fix/sx-meeting-optional-voice-move` forks from the release head:
  remove Move Members from the bot's channel overwrites too. The live bot lacks
  that permission and moving private-room occupants is optional. Merge to
  `nightly` and then `master` with `--no-ff` after focused checks.

## Meeting placement and start repair (2026-10-31)
- Live log at 02:41:13 UTC: `/meeting start` was rejected by a bot-wide Connect
  preflight even though a meeting room grants bot Connect on that channel.
- The guild has a dedicated `[🔴] Team Meeting` category (ID
  `1333113500318433397`). Place all bot-created meeting rooms there; fail
  clearly if the category is unavailable rather than creating a root room.
- Make `also` an optional single-member slash picker. A role or wider audience
  can include that guest; Custom remains the multi-member picker.
- History blueprint: `fix/sx-meeting-category-connect` from `master` for
  category resolution and Connect repair, followed by
  `fix/sx-meeting-also-picker` from that branch for the option contract.
  Merge each into `nightly` with `--no-ff`, test, deploy, then merge `nightly`
  into `master` with `--no-ff`.

## DM name form repair (2026-11-08)
- Goal: A name submitted through a DM updates the member's server nickname and saves the name.
- Constraint: Keep existing onboarding roles, birthday prompt, and record-sync data fields.
- Acceptance: DM submissions resolve a guild member, acknowledge promptly, and report unavailable members or nickname permissions without crashing.
- Steps: Add shared DM member resolution; update both name modals; verify with focused fake interactions and existing smoke checks; deploy and inspect service startup.
- Risk: The bot may not have the member cached; fetch from the configured guild and give a useful error if Discord cannot find them.
- History: `fix/sx-dm-name-form` forks from `nightly` for one focused fix commit, then merges into `nightly` with `--no-ff`. The integrated head is pushed and deployed to the existing OCI service.
- Verification: 16 focused name/record-sync tests pass; all cogs load in the smoke test. Full pytest: 286 passed, one unrelated existing music-source tuple/list assertion failed.
