#!/usr/bin/env python3
"""doctor.py — check that everything reference-search needs is installed and reachable.

Read-only: it never writes to Zotero or to config.json. Run it after installing,
or when a step of the skill fails and you want to know why.

    python doctor.py            # local checks + Zotero
    python doctor.py --online   # also try one PubMed request
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import banklib as bl  # noqa: E402

OK, WARN, FAIL = "✓", "!", "✗"
_results: list[tuple[str, str, str, str]] = []  # (mark, area, what, hint)


def report(mark: str, area: str, what: str, hint: str = "") -> None:
    _results.append((mark, area, what, hint))
    print(f"  {mark} {what}")
    if hint and mark != OK:
        print(f"      → {hint}")


def run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)


def find_skill(name: str) -> Path | None:
    for base in (bl.SKILL_ROOT.parent, Path.home() / ".claude" / "skills", Path.home() / ".agents" / "skills"):
        if (base / name / "SKILL.md").is_file():
            return base / name
    return None


def find_bash() -> str | None:
    """On Windows the review step runs in Git Bash (what Claude Code's Bash tool uses). Git for Windows
    usually puts only git.exe on PATH, and a bare `bash` there may be WSL's, so look next to git first."""
    if sys.platform != "win32":
        return shutil.which("bash")
    candidates = [os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")]
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent  # …\Git\cmd\git.exe → …\Git
        candidates += [str(root / "bin" / "bash.exe"), str(root / "usr" / "bin" / "bash.exe")]
    candidates += [str(Path(os.environ.get(v, "")) / "Git" / "bin" / "bash.exe") for v in ("ProgramFiles", "LOCALAPPDATA") if os.environ.get(v)]
    candidates.append(shutil.which("bash"))
    for c in candidates:
        if c and Path(c).is_file() and "system32" not in c.lower():
            return c
    return None


def check_basics() -> None:
    print("基本")
    v = sys.version_info
    report(OK if v >= (3, 9) else FAIL, "基本", f"Python {v.major}.{v.minor}.{v.micro}（{sys.executable}）", "需要 Python 3.9 以上")
    report(OK, "基本", f"skill 位置：{bl.SKILL_ROOT}")
    cfg = bl.load_config()
    path = bl.config_path()
    if path.is_file():
        report(OK, "基本", f"設定檔：{path}")
    else:
        report(WARN, "基本", f"沒有設定檔 {path}，使用預設值", f"複製 config.example.json 成 config.json 再修改（見 README「設定」）")
    out = Path(cfg["output_root"])
    report(OK if out.is_dir() else WARN, "基本", f"輸出資料夾 output_root：{out}", "還不存在；第一次產出時會建立" if not out.is_dir() else "")
    if cfg.get("obsidian_vault"):
        vault = Path(cfg["obsidian_vault"])
        report(OK if vault.is_dir() else FAIL, "基本", f"Obsidian vault：{vault}", "資料夾不存在；確認 obsidian_vault 路徑，或設成空字串關閉同步")
    else:
        report(OK, "基本", "Obsidian 同步：關閉（obsidian_vault 未設定）")


def check_search() -> None:
    print("檢索")
    report(OK, "檢索", "PubMed／Europe PMC 檢索：內建 scripts/pubmed_search.py（只用標準函式庫）")
    pn = find_skill("paper-navigator")
    if pn:
        report(OK, "檢索", f"paper-navigator skill：{pn}")
        has_httpx = importlib.util.find_spec("httpx") is not None
        report(OK if has_httpx else WARN, "檢索", "httpx 套件（paper-navigator 的 S2／全文腳本需要）" + ("" if has_httpx else "：未安裝"),
               f'"{sys.executable}" -m pip install --user httpx')
    else:
        report(WARN, "檢索", "沒有安裝 paper-navigator skill",
               "臨床題目仍可用內建的 PubMed 檢索；非生醫題目（S2／arXiv）與開放取用全文下載需要它。安裝方式見 README")


def check_review(cfg: dict) -> None:
    print("交叉評審")
    script = bl.find_cross_review_script(cfg)
    report(OK if script else FAIL, "評審", f"cross-review skill：{script}" if script else "找不到 cross-review skill 的 run-cross-review.sh",
           "安裝方式見 README；裝在別處就在 config.json 設 cross_review_script")
    bash = find_bash()
    if not bash:
        report(FAIL, "評審", "找不到 bash", "Windows 請安裝 Git for Windows（附 Git Bash）" if sys.platform == "win32" else "請安裝 bash")
    else:
        report(OK, "評審", f"bash：{bash}")
    codex = shutil.which("codex")
    if not codex:
        report(FAIL, "評審", "找不到 Codex CLI（codex）", "npm install -g @openai/codex，再執行 codex login。沒有它就不能交叉評審")
        return
    code, out = run([codex, "--version"])
    report(OK if code == 0 else WARN, "評審", f"Codex CLI：{out.splitlines()[0] if out else codex}")
    code, out = run([codex, "login", "status"])
    if code == 0:
        report(OK, "評審", f"Codex 登入狀態：{out.splitlines()[-1] if out else '已登入'}")
    else:
        report(WARN, "評審", "Codex 可能沒有登入（codex login status 失敗）", "執行 codex login")


def check_zotero() -> None:
    print("Zotero")
    import zotero_import as zi  # imported here so a broken Zotero module never hides the other checks

    exe = zi.find_zotero()
    report(OK if exe else WARN, "Zotero", f"Zotero 程式：{exe}" if exe else "找不到 Zotero 程式",
           "沒開 Zotero 時腳本無法自動啟動；請手動開啟，或在 config.json 設 zotero_exe")
    if not zi._ping():
        report(WARN, "Zotero", "Zotero 沒有在執行（127.0.0.1:23119 連不上），略過連線檢查", "開啟 Zotero 後重跑 doctor")
        return
    report(OK, "Zotero", "連接器（127.0.0.1:23119/connector）可連線")
    status, body = zi.call("/api/users/0/collections?format=json&limit=1")
    if status == 200 and isinstance(body, list):
        report(OK, "Zotero", "本機 API 已開啟（查重與讀回核對用）")
    else:
        report(FAIL, "Zotero", f"本機 API 無法使用（HTTP {status}）",
               "Zotero 7 以上：設定 → 進階 → 勾選「允許此電腦上的其他應用程式與 Zotero 通訊」")


def check_online() -> None:
    print("網路")
    import pubmed_search as ps

    try:
        _, _, total = ps.search_pubmed("kidney", limit=1)
        report(OK, "網路", f"PubMed E-utilities 可連線（測試查詢 {total} 筆）")
    except SystemExit as exc:
        report(FAIL, "網路", f"PubMed 連線失敗：{exc}", "檢查網路或代理伺服器設定")


def main() -> None:
    bl.utf8_streams()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--online", action="store_true", help="另外送一次 PubMed 查詢測試網路")
    args = ap.parse_args()

    cfg = bl.load_config()
    check_basics()
    check_search()
    check_review(cfg)
    check_zotero()
    if args.online:
        check_online()

    fails = [r for r in _results if r[0] == FAIL]
    warns = [r for r in _results if r[0] == WARN]
    print()
    if not fails and not warns:
        print("全部通過。")
    else:
        print(f"{len(fails)} 項失敗、{len(warns)} 項提醒。")
        areas = sorted({r[1] for r in fails})
        if "評審" in areas:
            print("交叉評審目前做不了：skill 會在送審那一步停下來回報；要先匯入 Zotero 得由你明確同意（--allow-unreviewed）。")
        if "Zotero" in areas:
            print("Zotero 匯入目前做不了；引用庫的報告與 CSV／BibTeX／RIS 仍會產生。")
    sys.exit(1 if any(r[0] == FAIL and r[1] == "基本" for r in _results) else 0)


if __name__ == "__main__":
    main()
