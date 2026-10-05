# reference-search installer for Windows (PowerShell 5.1+).
#
#   irm https://raw.githubusercontent.com/benny10030671-commits/reference-search/main/install.ps1 | iex
#
# Installs (or updates) into %USERPROFILE%\.claude\skills:
#   reference-search  - this skill
#   cross-review      - https://github.com/quanru/cross-review (Codex review step), if missing
#   paper-navigator   - skills/paper-navigator from https://github.com/EvoScientist/EvoSkills, if missing
# then creates config.json from config.example.json (never overwrites it) and runs doctor.py.
# Messages are ASCII on purpose: Windows PowerShell 5.1 misreads non-ASCII in a BOM-less script.
#
# Options when run from a saved copy:  .\install.ps1 -NoDeps   .\install.ps1 -SkillsDir D:\skills

param(
    [string]$SkillsDir = (Join-Path $HOME ".claude\skills"),
    [string]$RepoUrl = "https://github.com/benny10030671-commits/reference-search.git",
    [switch]$NoDeps
)

# Native tools (git, pip) report progress on stderr; with "Stop", Windows PowerShell 5.1 turns that into
# errors. Failures are caught through $LASTEXITCODE instead, and Fail() throws (never exit, which would
# close the window when run through iex).
$ErrorActionPreference = "Continue"

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "  x $msg" -ForegroundColor Red; throw $msg }

function Find-Python {
    # The Microsoft Store "python" stub exists on PATH but only opens the Store, so test that it really runs.
    foreach ($cand in @("python", "py", "python3")) {
        if (-not (Get-Command $cand -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = & $cand -c "import sys; print(sys.version_info >= (3, 9))" 2>$null
            if ($out -eq "True") { return $cand }
        } catch { }
    }
    return $null
}

function Invoke-Git {
    & git @args
    if ($LASTEXITCODE -ne 0) { Fail "git $($args -join ' ') failed" }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "git not found. Install Git for Windows first (it also provides Git Bash, which the review step needs): https://git-scm.com/download/win"
}
$python = Find-Python
if (-not $python) {
    Fail "Python 3.9+ not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')."
}

New-Item -ItemType Directory -Force -Path $SkillsDir | Out-Null
$target = Join-Path $SkillsDir "reference-search"

if (Test-Path (Join-Path $target ".git")) {
    Say "Updating reference-search in $target"
    Invoke-Git -C $target pull --ff-only
} elseif (Test-Path $target) {
    Fail "$target exists but is not a git checkout. Move it away and run the installer again."
} else {
    Say "Installing reference-search into $target"
    Invoke-Git clone --depth 1 $RepoUrl $target
}

if (-not $NoDeps) {
    $xr = Join-Path $SkillsDir "cross-review"
    if (Test-Path (Join-Path $xr "SKILL.md")) {
        Say "cross-review already installed ($xr)"
    } else {
        Say "Installing cross-review into $xr"
        Invoke-Git clone --depth 1 https://github.com/quanru/cross-review.git $xr
    }

    $pn = Join-Path $SkillsDir "paper-navigator"
    if (Test-Path (Join-Path $pn "SKILL.md")) {
        Say "paper-navigator already installed ($pn)"
    } else {
        Say "Installing paper-navigator into $pn (from EvoScientist/EvoSkills)"
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("evoskills-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
        Invoke-Git clone --depth 1 --filter=blob:none --sparse https://github.com/EvoScientist/EvoSkills.git $tmp
        Invoke-Git -C $tmp sparse-checkout set skills/paper-navigator
        Copy-Item -Recurse -ErrorAction Stop (Join-Path $tmp "skills\paper-navigator") $pn
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $tmp
    }

    & $python -c "import httpx" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Say "Installing the httpx package (paper-navigator's scripts need it)"
        & $python -m pip install --user --quiet httpx
        if ($LASTEXITCODE -ne 0) { Warn "pip install httpx failed; run it yourself: $python -m pip install --user httpx" }
    }
}

$config = Join-Path $target "config.json"
if (-not (Test-Path $config)) {
    Copy-Item -ErrorAction Stop (Join-Path $target "config.example.json") $config
    Say "Created $config (edit output_root / obsidian_vault there if you like)"
}

Say "Checking the environment"
& $python (Join-Path $target "scripts\doctor.py")

Write-Host ""
Say "Done. Restart Claude Code, then type:  /reference-search <your topic>"
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Warn "The Codex review step needs the Codex CLI:  npm install -g @openai/codex   then   codex login"
}
