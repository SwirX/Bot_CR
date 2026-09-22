#!/usr/bin/env bash
# deploy_mc_stack.sh — bring the live Robotics CMC Minecraft-login stack onto
# the code that actually works.
#
# Two halves, each idempotent and safe to re-run:
#
#   1. BOT    — puts Bot_CR on `nightly` (hub-display fallback + OTP watcher)
#               and restarts the bot process.
#   2. PLUGIN — swaps MCLink-0.1.0-SNAPSHOT.jar into the Paper server's
#               plugins/ dir, backing up whatever MCLink jar was there.
#
# Design notes:
#   * Nothing is overwritten without a timestamped backup.
#   * The bot is never killed blindly: restart only happens through an
#     explicitly configured mechanism (systemd unit or pm2 name). Without
#     one, the script updates + smoke-tests and tells you the restart command.
#   * `--dry-run` prints every action it WOULD take, mutating nothing.
#
# Usage:
#   scripts/deploy_mc_stack.sh [--dry-run] [--bot-only | --plugin-only]
#
# Configuration: set env vars, or copy deploy_mc_stack.env.example to
# deploy_mc_stack.env next to this script and fill it in. Both are read.
#
#   BOT_REPO        path to the Bot_CR checkout to update (default: this repo)
#   BOT_PYTHON      python of the bot venv (default: $BOT_REPO/.venv-local/bin/python)
#   BOT_SERVICE     systemd unit for the bot, e.g. "robotics-otp" (optional)
#   BOT_PM2_NAME    pm2 process name for the bot, e.g. "robotics-otp" (optional)
#   MC_JAR          path to the freshly built plugin jar
#                     (default: /home/swirx/dev/projects/mc-link/build/libs/MCLink-0.1.0-SNAPSHOT.jar)
#   MC_PLUGINS_DIR  Paper server's plugins/ directory (required for the plugin half)
#   MC_RELOAD_CMD   optional; run verbatim after the jar swap (e.g. an rcon
#                   "reload confirm") instead of just printing the hint.

set -euo pipefail

ME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$ME/.." && pwd)"

# ── load config ────────────────────────────────────────────────────────────
if [[ -f "$ME/deploy_mc_stack.env" ]]; then
    # shellcheck disable=SC1091
    source "$ME/deploy_mc_stack.env"
fi

DRY=""
WANT="all"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --bot-only) WANT="bot" ;;
        --plugin-only) WANT="plugin" ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

BOT_REPO="${BOT_REPO:-$REPO_ROOT}"
BOT_PYTHON="${BOT_PYTHON:-$BOT_REPO/.venv-local/bin/python}"
MC_JAR="${MC_JAR:-/home/swirx/dev/projects/mc-link/build/libs/MCLink-0.1.0-SNAPSHOT.jar}"
MC_PLUGINS_DIR="${MC_PLUGINS_DIR:-}"
MC_RELOAD_CMD="${MC_RELOAD_CMD:-}"

c_grn=$'\e[32m'; c_ylw=$'\e[33m'; c_red=$'\e[31m'; c_end=$'\e[0m'
info() { printf '%s[*]%s %s\n' "$c_grn" "$c_end" "$*"; }
warn() { printf '%s[!]%s %s\n' "$c_ylw" "$c_end" "$*"; }
die()  { printf '%s[x]%s %s\n' "$c_red" "$c_end" "$*" >&2; exit 1; }

# run CMD... — execute unless we're in dry-run.
run() {
    if [[ -n $DRY ]]; then
        printf '%s[dry-run]%s %s\n' "$c_ylw" "$c_end" "$*"
    else
        "$@"
    fi
}

# ── half 1: bot ────────────────────────────────────────────────────────────
update_bot() {
    info "bot: updating $BOT_REPO to nightly"
    [[ -d "$BOT_REPO/.git" ]] || die "BOT_REPO is not a git repo: $BOT_REPO"
    [[ -x "$BOT_PYTHON" ]] || die "BOT_PYTHON not executable: $BOT_PYTHON"

    run git -C "$BOT_REPO" fetch --prune --quiet || warn "bot: fetch failed (offline?); using local nightly"
    run git -C "$BOT_REPO" checkout --quiet nightly
    if git -C "$BOT_REPO" remote get-url origin >/dev/null 2>&1; then
        run git -C "$BOT_REPO" pull --ff-only --quiet origin nightly \
            || warn "bot: pull failed; deploying the local nightly HEAD instead"
    else
        warn "bot: no 'origin' remote — deploying local nightly HEAD"
    fi

    info "bot: smoke-testing the tree (dummy credentials, hermetic)"
    run env BOT_TOKEN=dummy APPWRITE_API_KEY=dummy \
        APPWRITE_ENDPOINT=https://example.test \
        "$BOT_PYTHON" "$BOT_REPO/scripts/smoke_test.py"

    local head
    head="$(git -C "$BOT_REPO" log -1 --format='%h %cd %s' --date=iso)"
    info "bot: nightly HEAD -> $head"
}

restart_bot() {
    if [[ -n ${BOT_SERVICE:-} ]]; then
        info "bot: restarting systemd unit '$BOT_SERVICE'"
        run systemctl restart "$BOT_SERVICE"
        sleep 2
        if ! systemctl is-active --quiet "$BOT_SERVICE"; then
            die "bot: unit '$BOT_SERVICE' did not come up (journalctl -u $BOT_SERVICE)"
        fi
        return 0
    fi
    if [[ -n ${BOT_PM2_NAME:-} ]]; then
        info "bot: restarting pm2 process '$BOT_PM2_NAME'"
        run pm2 restart "$BOT_PM2_NAME"
        return 0
    fi
    warn "bot: no restart mechanism configured. Set BOT_SERVICE or BOT_PM2_NAME,"
    warn "     or restart manually:  cd $BOT_REPO && nohup $BOT_PYTHON BOT.py &"
    return 0
}

# ── half 2: plugin ─────────────────────────────────────────────────────────
update_plugin() {
    [[ -n $MC_PLUGINS_DIR ]] || die "plugin: MC_PLUGINS_DIR is required for the plugin half"
    [[ -d "$MC_PLUGINS_DIR" ]] || die "plugin: MC_PLUGINS_DIR not a directory: $MC_PLUGINS_DIR"
    [[ -f "$MC_JAR" ]] || die "plugin: built jar not found: $MC_JAR (build it first)"

    local backup_dir="$MC_PLUGINS_DIR/backup"
    local ts
    ts="$(date +%Y%m%d-%H%M%S)"

    info "plugin: swapping MCLink jar in $MC_PLUGINS_DIR"
    info "plugin: new jar -> $MC_JAR"
    run mkdir -p "$backup_dir"

    # Any jar matching MCLink-*.jar older than this deploy must leave plugins/
    # entirely — a Paper server refuses to start two plugins with the same
    # `name: MCLink` from different jars.
    local found=0 old
    for old in "$MC_PLUGINS_DIR"/MCLink-*.jar; do
        [[ -e "$old" ]] || break
        run mv -f "$old" "$backup_dir/MCLink-${ts}.bak.jar"
        found=1
    done
    if [[ $found -eq 1 ]]; then
        info "plugin: previous MCLink jar(s) moved to $backup_dir/MCLink-${ts}.bak.jar"
    fi

    run cp -f "$MC_JAR" "$MC_PLUGINS_DIR/"

    if [[ -n $MC_RELOAD_CMD ]]; then
        info "plugin: running MC_RELOAD_CMD"
        run eval "$MC_RELOAD_CMD"
    else
        warn "plugin: restart the server (or /reload confirm) so the new jar loads."
    fi
    info "plugin: after reload, in-game commands are: /mcverify <code>, /otp <code>"
}

# ── run ────────────────────────────────────────────────────────────────────
info "deploying MC-Login stack (dry-run=$([ -n "$DRY" ] && echo yes || echo no))"
if [[ $WANT == "bot" || $WANT == "all" ]]; then
    update_bot
    restart_bot
fi
if [[ $WANT == "plugin" || $WANT == "all" ]]; then
    update_plugin
fi

info "done."
printf '%s\n' "- restart of the live bot needed for /mc hub display + OTP watcher"
printf '%s\n' "- plugin reload needed for /mcverify + /otp in-game"
printf '%s\n' "- still manual: confirm AuthMe lobby is enforced, MCLink config"
printf '%s\n' "  points at robotics_hub with a scoped key, and users re-pair via /mclink."