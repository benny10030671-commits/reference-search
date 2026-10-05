#!/usr/bin/env bash
# reference-search installer for macOS / Linux (also works in Git Bash on Windows).
#
#   curl -fsSL https://raw.githubusercontent.com/benny10030671-commits/reference-search/main/install.sh | bash
#
# Installs (or updates) into ~/.claude/skills:
#   reference-search  - this skill
#   cross-review      - https://github.com/quanru/cross-review (Codex review step), if missing
#   paper-navigator   - skills/paper-navigator from https://github.com/EvoScientist/EvoSkills, if missing
# then creates config.json from config.example.json (never overwrites it) and runs doctor.py.
#
# Environment overrides: SKILLS_DIR, REPO_URL, NO_DEPS=1
set -euo pipefail

SKILLS_DIR="${SKILLS_DIR:-$HOME/.claude/skills}"
REPO_URL="${REPO_URL:-https://github.com/benny10030671-commits/reference-search.git}"
NO_DEPS="${NO_DEPS:-0}"

say()  { printf '\033[36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
fail() { printf '\033[31m  x %s\033[0m\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || fail "git not found. Install git first."

PYTHON=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && [ "$("$cand" -c 'import sys; print(sys.version_info >= (3, 9))' 2>/dev/null)" = "True" ]; then
    PYTHON="$cand"; break
  fi
done
[ -n "$PYTHON" ] || fail "Python 3.9+ not found. Install it from https://www.python.org/downloads/"

mkdir -p "$SKILLS_DIR"
target="$SKILLS_DIR/reference-search"

if [ -d "$target/.git" ]; then
  say "Updating reference-search in $target"
  git -C "$target" pull --ff-only
elif [ -e "$target" ]; then
  fail "$target exists but is not a git checkout. Move it away and run the installer again."
else
  say "Installing reference-search into $target"
  git clone --depth 1 "$REPO_URL" "$target"
fi

if [ "$NO_DEPS" != "1" ]; then
  xr="$SKILLS_DIR/cross-review"
  if [ -f "$xr/SKILL.md" ]; then
    say "cross-review already installed ($xr)"
  else
    say "Installing cross-review into $xr"
    git clone --depth 1 https://github.com/quanru/cross-review.git "$xr"
  fi

  pn="$SKILLS_DIR/paper-navigator"
  if [ -f "$pn/SKILL.md" ]; then
    say "paper-navigator already installed ($pn)"
  else
    say "Installing paper-navigator into $pn (from EvoScientist/EvoSkills)"
    tmp="$(mktemp -d)"
    git clone --depth 1 --filter=blob:none --sparse https://github.com/EvoScientist/EvoSkills.git "$tmp/evoskills"
    git -C "$tmp/evoskills" sparse-checkout set skills/paper-navigator
    cp -R "$tmp/evoskills/skills/paper-navigator" "$pn"
    rm -rf "$tmp"
  fi

  if ! "$PYTHON" -c 'import httpx' >/dev/null 2>&1; then
    say "Installing the httpx package (paper-navigator's scripts need it)"
    "$PYTHON" -m pip install --user --quiet httpx || warn "pip install httpx failed; run it yourself: $PYTHON -m pip install --user httpx"
  fi
fi

if [ ! -f "$target/config.json" ]; then
  cp "$target/config.example.json" "$target/config.json"
  say "Created $target/config.json (edit output_root / obsidian_vault there if you like)"
fi

say "Checking the environment"
"$PYTHON" "$target/scripts/doctor.py" || true

echo
say "Done. Restart Claude Code, then type:  /reference-search <your topic>"
command -v codex >/dev/null 2>&1 || warn "The Codex review step needs the Codex CLI:  npm install -g @openai/codex   then   codex login"
