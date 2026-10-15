#!/usr/bin/env sh
# Bootstrap the radar. Works on any Linux/macOS with a POSIX shell.
#
#   curl -sSL https://raw.githubusercontent.com/PastyNew/fourchar-radar/main/install.sh | sh
#
# Or, from a checkout:  sh install.sh
#
# Strategy: prefer a virtualenv (clean, no root, survives PEP 668), and fall
# back to a --user install on distros where venv creation is unavailable.

set -eu

BOLD=''
DIM=''
RED=''
GREEN=''
RESET=''
if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
    BOLD="$(printf '\033[1m')"
    DIM="$(printf '\033[2m')"
    RED="$(printf '\033[31m')"
    GREEN="$(printf '\033[32m')"
    RESET="$(printf '\033[0m')"
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
info() { printf '%s    %s%s\n' "$DIM" "$*" "$RESET"; }
die()  { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- python
find_python() {
    for candidate in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
                printf '%s' "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

step "Looking for Python 3.11+"
PY="$(find_python)" || die "Python 3.11 or newer is required.

  Debian/Ubuntu:  sudo apt install python3 python3-venv
  Fedora:         sudo dnf install python3
  Arch:           sudo pacman -S python
  Alpine:         sudo apk add python3
  openSUSE:       sudo zypper install python3

Alpine and some minimal images also need:  apk add python3-dev libffi-dev"
info "using $PY ($("$PY" --version 2>&1))"

# ------------------------------------------------------------------ venv
# A venv is preferred, but `python3 -m venv` fails on distros that ship the
# interpreter without ensurepip (Debian without python3-venv, some images).
WORKDIR="${FOURCHAR_RADAR_HOME:-$HOME/.local/share/fourchar-radar}"
VENV="$WORKDIR/venv"
PIP_ARGS=""

if "$PY" -m venv --help >/dev/null 2>&1 && "$PY" -c 'import ensurepip' >/dev/null 2>&1; then
    step "Creating a virtualenv in $VENV"
    mkdir -p "$WORKDIR"
    "$PY" -m venv "$VENV" 2>/dev/null || die "venv creation failed. Install the venv module:
  Debian/Ubuntu:  sudo apt install python3-venv
  Fedora:         sudo dnf install python3-virtualenv
  Alpine:         apk add py3-virtualenv"
    BIN="$VENV/bin/fourchar-radar"
    info "using the virtualenv"
else
    step "No venv available here; installing into your user site-packages"
    info "this is normal on minimal distros. It works, it just shares the interpreter."
    BIN=""
    PIP_ARGS="--user"
fi

# ------------------------------------------------------------------ pip
step "Installing fourchar-radar"
SRC=""
if [ -f "pyproject.toml" ] && [ -d "src/fourchar_radar" ]; then
    SRC="."   # running from a checkout
    info "installing from this directory"
fi

if [ -n "$BIN" ]; then
    "$VENV/bin/python" -m pip install --quiet --upgrade pip 2>/dev/null || true
    "$VENV/bin/python" -m pip install --quiet $PIP_ARGS ${SRC:+"$SRC"} \
        "fourchar-radar" 2>/dev/null \
    || "$VENV/bin/python" -m pip install --quiet $PIP_ARGS ${SRC:+"$SRC"} \
    || die "install failed"
else
    "$PY" -m pip install --quiet --upgrade --user pip 2>/dev/null || true
    # PEP 668 (Debian 12+, Fedora 40+) blocks plain --user installs. Retry with
    # the override rather than telling the user to go edit their system Python.
    "$PY" -m pip install --quiet --user $SRC "fourchar-radar" 2>/dev/null \
    || "$PY" -m pip install --quiet --user --break-system-packages $SRC "fourchar-radar" 2>/dev/null \
    || die "install failed. Try creating a virtualenv manually and re-running."
fi

# ------------------------------------------------------------------ link
if [ -n "$BIN" ]; then
    step "Linking the launcher into ~/.local/bin"
    mkdir -p "$HOME/.local/bin"
    LAUNCHER="$HOME/.local/bin/fourchar-radar"
    printf '#!/bin/sh\nexec "%s" "$@"\n' "$VENV/bin/fourchar-radar" > "$LAUNCHER"
    chmod +x "$LAUNCHER"
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) info "add to your shell profile:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
    esac
    COMMAND="$LAUNCHER"
else
    COMMAND="$(command -v fourchar-radar || echo "$HOME/.local/bin/fourchar-radar")"
fi

# ------------------------------------------------------------------ done
say ""
say "${GREEN}fourchar-radar installed.${RESET}"
say ""
if [ -n "$BIN" ]; then
    say "  database:  ${BOLD}$WORKDIR/radar.db${RESET}"
else
    # The --user branch has no virtualenv, so the app data dir is where the
    # database actually lands (XDG / Library / LOCALAPPDATA).
    say "  database:  ${BOLD}$("$PY" -c 'import fourchar_radar.platforms as p; print(p.default_db_path())')${RESET}"
fi
say ""
say "Start it with your webhook URL — no config file needed:"
say ""
say "    ${BOLD}fourchar-radar run https://discord.com/api/webhooks/ID/TOKEN${RESET}"
say ""
say "Get that URL in Discord: channel -> Edit Channel -> Integrations ->"
say "Webhooks -> New Webhook -> Copy Webhook URL."
say ""
say "Try it without alerts first:  ${BOLD}fourchar-radar run --dry-run --limit 20${RESET}"
say ""
