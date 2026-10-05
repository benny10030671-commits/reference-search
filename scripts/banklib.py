"""Shared helpers for the reference-search scripts.

bank.json is the single source of truth for a citation bank: every export
(Markdown, CSV, BibTeX, RIS), the cross-review packet and the Zotero import are
generated from it, so the numbers and wording can never drift apart between
files. See references/bank-schema.md for the schema.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent

# config.json sits next to SKILL.md and is not tracked by git, so `git pull` never overwrites it.
# config.example.json documents every key.
CONFIG_DEFAULTS = {
    "output_root": "~/Documents/reference-search",
    "obsidian_vault": "",
    "obsidian_subdir": "reference-search",
    "obsidian_moc": "",
    "zotero_exe": "",
    "cross_review_script": "",
}
_PATH_KEYS = ("output_root", "obsidian_vault", "zotero_exe", "cross_review_script")

TIERS = ["核心", "重要佐證", "補充"]
SOURCE_LABELS = {
    "abstract": "摘要",
    "fulltext": "全文",
    "paraphrase": "全文摘述（未抄錄原句）",
}
SECONDHAND_SUFFIX = "（該文對他方文件的轉述）"
NO_QUOTE_FLAG = "無引用句-需全文"
SECONDHAND_FLAG = "二手轉述-待核對原文"

_WS = re.compile(r"\s+")


def utf8_streams() -> None:
    """Windows consoles default to cp950 here; Chinese output would crash print()."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def die(msg: str, code: int = 1) -> None:
    print(f"錯誤：{msg}", file=sys.stderr)
    sys.exit(code)


def config_path() -> Path:
    return Path(os.environ.get("REFERENCE_SEARCH_CONFIG") or SKILL_ROOT / "config.json")


def load_config() -> dict:
    """Settings from config.json (missing keys fall back to CONFIG_DEFAULTS); paths come back expanded."""
    cfg = dict(CONFIG_DEFAULTS)
    path = config_path()
    if path.is_file():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8-sig")))
        except json.JSONDecodeError as exc:
            die(f"{path} 不是有效的 JSON：{exc}")
    for key in _PATH_KEYS:
        if cfg.get(key):
            cfg[key] = str(Path(os.path.expandvars(os.path.expanduser(str(cfg[key])))))
    return cfg


def find_cross_review_script(cfg: dict | None = None) -> Path | None:
    """run-cross-review.sh from the cross-review skill: config, env var, then the usual install locations."""
    cfg = cfg or load_config()
    home = Path.home()
    candidates = [
        cfg.get("cross_review_script"),
        os.environ.get("CROSS_REVIEW_SCRIPT"),
        SKILL_ROOT.parent / "cross-review/scripts/run-cross-review.sh",
        home / ".claude/skills/cross-review/scripts/run-cross-review.sh",
        home / ".agents/skills/cross-review/scripts/run-cross-review.sh",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c).resolve()
    return None


def bash_path(path: Path | str) -> str:
    """C:\\x\\y → /c/x/y, the form Git Bash expects; POSIX paths pass through unchanged."""
    return re.sub(r"^([A-Za-z]):", lambda m: "/" + m.group(1).lower(), str(path).replace("\\", "/"))


def load_bank(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        die(f"找不到 bank 檔：{p}")
    bank = json.loads(p.read_text(encoding="utf-8"))
    bank.setdefault("refs", [])
    bank.setdefault("sections", [])
    bank.setdefault("categories", {})
    return bank


def save_bank(bank: dict, path: str | Path) -> None:
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(bank, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def norm_ws(text: str) -> str:
    # chr(0x200B) is the zero-width space that some publisher pages scatter through text.
    return _WS.sub(" ", (text or "").replace(chr(0x200B), "")).strip()


def norm_title(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def clean_doi(doi: str) -> str:
    """Strip a resolver prefix but keep the DOI's own case (for display and links)."""
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", (doi or "").strip(), flags=re.I)


def norm_doi(doi: str) -> str:
    """Lower-cased DOI for matching (DOIs are case-insensitive)."""
    return clean_doi(doi).lower()


# ---------------------------------------------------------------- sections


def section_names(bank: dict) -> list[str]:
    return [s["name"] if isinstance(s, dict) else s for s in bank.get("sections", [])]


def section_hint(bank: dict, name: str) -> str:
    for s in bank.get("sections", []):
        if isinstance(s, dict) and s.get("name") == name:
            return s.get("hint", "")
    return ""


# ---------------------------------------------------------------- ordering


def _tier_rank(ref: dict) -> int:
    tier = ref.get("tier")
    return TIERS.index(tier) if tier in TIERS else len(TIERS)


def ordered_refs(bank: dict) -> list[dict]:
    """Category order (as declared), then tier, then insertion order."""
    cats = list(bank.get("categories", {}))
    indexed = list(enumerate(bank["refs"]))

    def key(pair):
        i, ref = pair
        cat = ref.get("category")
        return (cats.index(cat) if cat in cats else len(cats), _tier_rank(ref), i)

    return [ref for _, ref in sorted(indexed, key=key)]


def numbering(bank: dict) -> dict[str, int]:
    return {ref["citekey"]: n for n, ref in enumerate(ordered_refs(bank), 1)}


# ---------------------------------------------------------------- labels


def first_author(ref: dict) -> str:
    authors = ref.get("authors") or []
    if not authors:
        return ""
    a = authors[0]
    return a.get("family") or a.get("literal") or ""


def short_label(ref: dict) -> str:
    return f"{first_author(ref) or ref['citekey']} {ref.get('year') or 'n.d.'}".strip()


def source_label(quote: dict) -> str:
    label = SOURCE_LABELS.get(quote.get("source", ""), quote.get("source", ""))
    if quote.get("secondhand"):
        label += SECONDHAND_SUFFIX
    return label


def quote_text(quote: dict) -> str:
    """What goes in the 'quote' column: the verbatim quote, or the locator for a paraphrase."""
    if quote.get("source") == "paraphrase":
        return f"〔{quote.get('locator', '')}；摘述，未抄錄原句〕"
    return quote.get("quote", "")


def real_quotes(ref: dict) -> list[dict]:
    return [q for q in ref.get("quotes", []) if q.get("source") != "paraphrase"]


def ref_flags(ref: dict) -> list[str]:
    flags = list(ref.get("flags") or [])
    if not ref.get("quotes") and NO_QUOTE_FLAG not in flags:
        flags.append(NO_QUOTE_FLAG)
    if any(q.get("secondhand") for q in ref.get("quotes", [])) and SECONDHAND_FLAG not in flags:
        flags.append(SECONDHAND_FLAG)
    return flags


def category_label(bank: dict, ref: dict) -> str:
    cat = ref.get("category", "")
    name = bank.get("categories", {}).get(cat, "")
    return f"{cat}. {name}" if name else cat


def ref_tags(bank: dict, ref: dict, extra: tuple[str, ...] | list[str] = ()) -> list[str]:
    tag = bank["tag"]
    tags = [tag]
    cat = ref.get("category")
    if cat:
        tags.append(f"{tag}/{cat}-{bank.get('categories', {}).get(cat, '')}".rstrip("-"))
    if ref.get("tier"):
        tags.append(f"{tag}/{ref['tier']}")
    tags += [f"{tag}/{f}" for f in ref_flags(ref)]
    tags += [t for t in extra if t and t not in tags]
    return tags


# ---------------------------------------------------------------- review state


def review_done(bank: dict) -> bool:
    return (bank.get("review") or {}).get("status") == "done"


def review_phrase(bank: dict) -> str:
    rv = bank.get("review") or {}
    if rv.get("status") == "done":
        who = rv.get("reviewer") or "外部評審"
        when = rv.get("date") or ""
        return f"已經過一輪 {who} 交叉評審（{when}）".replace("（）", "")
    return "尚未經過交叉評審"


# ---------------------------------------------------------------- Zotero note


def note_html(bank: dict, ref: dict) -> str:
    e = html.escape
    parts = [f"<h2>引用句（{e(bank['tag'])} 引用庫 {e(str(bank.get('date', '')))}）</h2>"]
    quotes = ref.get("quotes", [])
    if quotes:
        parts.append("<ul>")
        for q in quotes:
            li = f"<li><b>{e(q.get('section', ''))}</b><br>{e(q.get('claim', ''))}<br>"
            if q.get("source") == "paraphrase":
                li += f"全文摘述（未抄錄原句）：{e(q.get('locator', ''))}"
            else:
                li += f"<blockquote>{e(q.get('quote', ''))}</blockquote>來源：{e(source_label(q))}"
                if q.get("locator"):
                    li += f"｜{e(q['locator'])}"
            parts.append(li + "</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>這篇沒有可逐字引用的句子（需要全文）。</p>")
    if ref.get("note"):
        parts.append(f"<p><b>備註：</b>{e(ref['note'])}</p>")
    parts.append(
        f"<p>由 Claude Code（AI）整理。引用句經程式逐字比對；論點摘要{e(review_phrase(bank))}，未經人工核對。</p>"
    )
    return "".join(parts)


def note_hash(ref: dict) -> str:
    """Content fingerprint of what the Zotero note says (ignores the dated footer)."""
    payload = json.dumps(
        {"quotes": ref.get("quotes", []), "note": ref.get("note", "")},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- stats


def stats(bank: dict) -> dict:
    refs = bank["refs"]
    quotes = [q for r in refs for q in r.get("quotes", [])]
    by_cat = {}
    for key, name in bank.get("categories", {}).items():
        by_cat[f"{key}. {name}"] = sum(1 for r in refs if r.get("category") == key)
    return {
        "refs": len(refs),
        "quotes_verbatim": sum(1 for q in quotes if q.get("source") != "paraphrase"),
        "from_abstract": sum(1 for q in quotes if q.get("source") == "abstract"),
        "from_fulltext": sum(1 for q in quotes if q.get("source") == "fulltext"),
        "secondhand": sum(1 for q in quotes if q.get("secondhand")),
        "paraphrases": sum(1 for q in quotes if q.get("source") == "paraphrase"),
        "refs_without_quotes": sum(1 for r in refs if not r.get("quotes")),
        "refs_unclassified": sum(1 for r in refs if not r.get("category") or not r.get("tier")),
        "tiers": {t: sum(1 for r in refs if r.get("tier") == t) for t in TIERS},
        "categories": by_cat,
        "fulltext_saved": sum(1 for r in refs if r.get("fulltext")),
        "review": (bank.get("review") or {}).get("status", "none"),
        "in_zotero": sum(1 for r in refs if (r.get("zotero") or {}).get("itemKey")),
    }
