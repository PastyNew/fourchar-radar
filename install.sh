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

# Where to install from, in order of preference:
#   1. a checkout we are standing in
#   2. PyPI
#   3. the GitHub archive (the package is not on PyPI yet, so this is what
#      actually runs for everyone using the one-liner)
REPO_SLUG="PastyNew/fourchar-radar"
TARBALL="https://github.com/$REPO_SLUG/archive/refs/heads/main.tar.gz"

TARGET=""
if [ -f "pyproject.toml" ] && [ -d "src/fourchar_radar" ]; then
    TARGET="."; info "installing from this directory"
fi

if [ -z "$TARGET" ]; then
    info "installing from PyPI"
    TARGET="fourchar-radar"
fi

# try_install <python> <extra pip args...>
try_install() {
    local py="$1"; shift
    "$py" -m pip install --quiet "$@" "$TARGET" 2>/dev/null && return 0
    # PEP 668 (Debian 12+, Fedora 40+) refuses --user installs outright.
    "$py" -m pip install --quiet "$@" --break-system-packages "$TARGET" 2>/dev/null && return 0
    return 1
}

install_ok=0

if [ -n "$BIN" ]; then
    "$VENV/bin/python" -m pip install --quiet --upgrade pip 2>/dev/null || true
    try_install "$VENV/bin/python" || true
    # Fall back to the GitHub archive when PyPI has no release yet.
    if ! "$VENV/bin/python" -c "import fourchar_radar" 2>/dev/null; then
        info "not on PyPI, using the GitHub archive"
        try_install "$VENV/bin/python" "$TARBALL" && install_ok=1
    else
        install_ok=1
    fi
else
    "$PY" -m pip install --quiet --upgrade --user pip 2>/dev/null || true
    try_install "$PY" --user || true
    if ! "$PY" -c "import fourchar_radar" 2>/dev/null; then
        info "not on PyPI, using the GitHub archive"
        try_install "$PY" --user "$TARBALL" && install_ok=1
    else
        install_ok=1
    fi
fi

[ "$install_ok" = "1" ] || die "install failed.

  Tried: $TARGET
  You can install from a checkout instead:
    git clone https://github.com/$REPO_SLUG && cd fourchar-radar && sh install.sh"

# ------------------------------------------------------------------ path
# A `--user` install drops the console script in ~/.local/bin, which is NOT on
# PATH on most distros by default. Detect that, create the launcher if pip did
# not, and tell the user exactly what to run — or do it for them.
USER_BIN="$HOME/.local/bin"

if [ -z "$BIN" ]; then
    step "Checking the launcher is on PATH"

    if ! command -v fourchar-radar >/dev/null 2>&1; then
        # pip puts the script here, but only if the user bin dir exists first.
        if [ ! -x "$USER_BIN/fourchar-radar" ]; then
            mkdir -p "$USER_BIN"
            PYTHON_BIN="$("$PY" -c 'import sysconfig; print(sysconfig.get_path("scripts", "posix_user"))')"
            if [ -x "$PYTHON_BIN/fourchar-radar" ]; then
                cp "$PYTHON_BIN/fourchar-radar" "$USER_BIN/fourchar-radar"
                chmod +x "$USER_BIN/fourchar-radar"
                info "created $USER_BIN/fourchar-radar"
            fi
        fi

        if ! command -v fourchar-radar >/dev/null 2>&1; then
            printf '\n'
            printf '%sPATH is not set up yet.%s Nothing else is wrong — run this:\n\n' \
                "${RED}" "${RESET}"
            printf '    export PATH="$HOME/.local/bin:$PATH"\n\n'
            printf 'and to make it permanent:\n\n'
            printf "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc\n\n"
            printf 'then re-run %sfourchar-radar --version%s to confirm.\n\n' "$BOLD" "$RESET"
            exit 1
        fi
    else
        info "found at $(command -v fourchar-radar)"
    fi
fi

# ------------------------------------------------------------------ link
if [ -n "$BIN" ]; then
    step "Linking the launcher into ~/.local/bin"
    mkdir -p "$USER_BIN"
    LAUNCHER="$USER_BIN/fourchar-radar"
    printf '#!/bin/sh\nexec "%s" "$@"\n' "$VENV/bin/fourchar-radar" > "$LAUNCHER"
    chmod +x "$LAUNCHER"
    case ":$PATH:" in
        *":$USER_BIN:"*) ;;
        *)
            info "note: ~/.local/bin is not on your PATH"
            info "add it with:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
            ;;
    esac
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
