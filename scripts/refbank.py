#!/usr/bin/env python3
"""refbank.py — build, check and export a citation bank (bank.json).

Subcommands
    init           create an empty bank.json for a topic
    add            fetch bibliographic records + abstracts (PMID / DOI / pool.jsonl) into the bank
    show           print abstracts (and current annotations) so they can be read in batches
    annotate       merge category / tier / quotes written by the model into the bank
    drop           remove references from the bank
    verify         check every verbatim quote is a substring of its source, plus schema checks
    export         write the Markdown report, CSV, BibTeX and RIS
    packet         write the cross-review packet (scope.md + packet.md)
    mark-reviewed  record that the cross-review round is finished
    stats          print counts as JSON (for the README)
    config         print the settings in use (output folder, Obsidian sync, tool paths)

Bibliographic metadata is always fetched, never typed: a reference list is the
one place where a plausible-looking guess is worse than a blank.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import banklib as bl  # noqa: E402

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CROSSREF = "https://api.crossref.org/works/"
UA = "reference-search-skill/1.0 (Claude Code; python-urllib)"

DOC_TYPE_RULES = [
    ("Meta-Analysis", "統合分析"),
    ("Systematic Review", "系統性回顧"),
    ("Practice Guideline", "指引"),
    ("Guideline", "指引"),
    ("Consensus Development Conference", "指引"),
    ("Randomized Controlled Trial", "隨機對照試驗"),
    ("Clinical Trial Protocol", "計畫書"),
    ("Clinical Trial", "臨床試驗"),
    ("Case Reports", "病例報告"),
    ("Review", "綜述"),
    ("Editorial", "評論"),
    ("Comment", "評論"),
    ("Letter", "評論"),
    ("Observational Study", "觀察性研究"),
]

PUNCT_FOLD = str.maketrans(
    {"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-"}
)
LETTER_FOLD = str.maketrans({"ß": "ss", "ø": "o", "Ø": "O", "đ": "d", "ł": "l", "Ł": "L", "æ": "ae", "Æ": "AE"})


# ================================================================ HTTP


def _http(url: str, data: bytes | None = None, timeout: int = 45) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    last: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 404:
                raise
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"request failed after retries: {url} ({last})")


# ================================================================ record fetching


def _t(el, path: str) -> str:
    if el is None:
        return ""
    found = el.find(path)
    return "".join(found.itertext()).strip() if found is not None else ""


def _doc_type(pub_types: list[str]) -> str:
    for needle, label in DOC_TYPE_RULES:
        if needle in pub_types:
            return label
    return "原始研究"


def _clean_title(title: str) -> str:
    title = bl.norm_ws(title)
    if title.startswith("[") and title.rstrip(".").endswith("]"):
        title = title.rstrip(".")[1:-1]
    return title[:-1] if title.endswith(".") and not title.endswith("...") else title


def parse_pubmed_article(art) -> dict:
    cit = art.find("MedlineCitation")
    article = cit.find("Article") if cit is not None else None
    pmid = _t(cit, "PMID")

    parts = []
    if article is not None:
        for ab in article.findall("Abstract/AbstractText"):
            body = "".join(ab.itertext()).strip()
            if body:
                label = ab.get("Label")
                parts.append(f"{label}: {body}" if label else body)

    authors = []
    if article is not None:
        for a in article.findall("AuthorList/Author"):
            collective = _t(a, "CollectiveName")
            if collective:
                authors.append({"literal": collective})
            elif _t(a, "LastName"):
                authors.append({"family": _t(a, "LastName"), "given": _t(a, "ForeName") or _t(a, "Initials")})

    year = _t(article, "Journal/JournalIssue/PubDate/Year")
    if not year:
        year = _t(article, "Journal/JournalIssue/PubDate/MedlineDate")[:4]
    if not year.isdigit():
        year = _t(article, "ArticleDate/Year")

    doi = pmc = ""
    for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):
        kind, val = aid.get("IdType"), (aid.text or "").strip()
        if kind == "doi" and not doi:
            doi = val
        elif kind == "pmc" and not pmc:
            pmc = val
    if not doi and article is not None:
        for el in article.findall("ELocationID"):
            if el.get("EIdType") == "doi":
                doi = (el.text or "").strip()

    pub_types = [(pt.text or "").strip() for pt in art.findall(".//PublicationTypeList/PublicationType")]
    return {
        "pmid": pmid,
        "pmcid": pmc,
        "doi": doi,
        "title": _clean_title(_t(article, "ArticleTitle")),
        "authors": authors,
        "journal": _t(article, "Journal/Title"),
        "journal_abbr": _t(article, "Journal/ISOAbbreviation"),
        "year": int(year) if year.isdigit() else None,
        "volume": _t(article, "Journal/JournalIssue/Volume"),
        "issue": _t(article, "Journal/JournalIssue/Issue"),
        "pages": _t(article, "Pagination/MedlinePgn"),
        "language": _t(article, "Language"),
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "pub_types": [p for p in pub_types if p],
        "doc_type": _doc_type(pub_types),
        "abstract": "\n".join(parts),
        "item_type": "journalArticle",
        "metadata_source": "pubmed",
    }


def efetch(pmids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(pmids), 100):
        params = {"db": "pubmed", "id": ",".join(pmids[i : i + 100]), "retmode": "xml", "tool": "reference-search"}
        if os.environ.get("NCBI_API_KEY"):
            params["api_key"] = os.environ["NCBI_API_KEY"]
        raw = _http(f"{EUTILS}/efetch.fcgi", data=urllib.parse.urlencode(params).encode())
        root = ET.fromstring(raw)
        out += [parse_pubmed_article(a) for a in root.findall("PubmedArticle")]
        skipped = len(root.findall("PubmedBookArticle"))
        if skipped:
            print(f"  略過 {skipped} 筆 PubMed 書籍章節（請用 annotate 的 new 手動加入）", file=sys.stderr)
        time.sleep(0.4)
    return out


def _strip_tags(text: str) -> str:
    return bl.norm_ws(html.unescape(re.sub(r"<[^>]+>", " ", text or "")))


def fetch_by_doi(doi: str) -> dict | None:
    """Europe PMC first (has abstracts); Crossref as a metadata-only fallback."""
    doi = bl.norm_doi(doi)
    query = urllib.parse.urlencode({"query": f'DOI:"{doi}"', "resultType": "core", "format": "json", "pageSize": 1})
    try:
        hits = json.loads(_http(f"{EPMC}?{query}")).get("resultList", {}).get("result", [])
    except Exception as exc:  # network trouble: fall through to Crossref
        print(f"  Europe PMC 查詢失敗（{exc}），改查 Crossref", file=sys.stderr)
        hits = []
    if hits:
        r = hits[0]
        if r.get("pmid"):
            recs = efetch([r["pmid"]])
            if recs:
                return recs[0]
        ji = r.get("journalInfo") or {}
        jr = ji.get("journal") or {}
        authors = []
        for a in (r.get("authorList") or {}).get("author", []):
            if a.get("collectiveName"):
                authors.append({"literal": a["collectiveName"]})
            elif a.get("lastName"):
                authors.append({"family": a["lastName"], "given": a.get("firstName") or a.get("initials", "")})
        year = str(ji.get("yearOfPublication") or r.get("pubYear") or "")
        pub_types = (r.get("pubTypeList") or {}).get("pubType", [])
        return {
            "pmid": "",
            "pmcid": r.get("pmcid", ""),
            "doi": r.get("doi", doi),
            "title": _clean_title(_strip_tags(r.get("title", ""))),
            "authors": authors,
            "journal": jr.get("title", ""),
            "journal_abbr": jr.get("isoabbreviation") or jr.get("medlineAbbreviation", ""),
            "year": int(year) if year.isdigit() else None,
            "volume": ji.get("volume", ""),
            "issue": ji.get("issue", ""),
            "pages": r.get("pageInfo", ""),
            "language": r.get("language", ""),
            "url": f"https://doi.org/{doi}",
            "pub_types": pub_types,
            "doc_type": "預印本" if r.get("source") == "PPR" else "原始研究",
            "abstract": _strip_tags(r.get("abstractText", "")),
            "item_type": "preprint" if r.get("source") == "PPR" else "journalArticle",
            "metadata_source": "europepmc",
        }
    try:
        m = json.loads(_http(CROSSREF + urllib.parse.quote(doi))).get("message", {})
    except Exception as exc:
        print(f"  DOI {doi}：Europe PMC 與 Crossref 都查不到（{exc}）。arXiv 文獻請改用 --pool；其他文件用 annotate 的 \"new\": true", file=sys.stderr)
        return None
    authors = []
    for a in m.get("author", []):
        if a.get("family"):
            authors.append({"family": a["family"], "given": a.get("given", "")})
        elif a.get("name"):
            authors.append({"literal": a["name"]})
    dp = ((m.get("issued") or {}).get("date-parts") or [[None]])[0]
    return {
        "pmid": "",
        "pmcid": "",
        "doi": doi,
        "title": _clean_title(_strip_tags((m.get("title") or [""])[0])),
        "authors": authors,
        "journal": (m.get("container-title") or [""])[0],
        "journal_abbr": (m.get("short-container-title") or [""])[0],
        "year": dp[0] if dp and dp[0] else None,
        "volume": m.get("volume", ""),
        "issue": m.get("issue", ""),
        "pages": m.get("page", ""),
        "language": m.get("language", ""),
        "url": f"https://doi.org/{doi}",
        "pub_types": [m.get("type", "")],
        "doc_type": "原始研究",
        "abstract": _strip_tags(m.get("abstract", "")),
        "item_type": "journalArticle",
        "metadata_source": "crossref",
    }


def from_pool_record(rec: dict) -> dict:
    """Convert an S2-shaped pool.jsonl record (paper-navigator scripts) to a bank ref."""
    ext = rec.get("externalIds") or {}
    authors = []
    for a in rec.get("authors") or []:
        name = bl.norm_ws(a.get("name", ""))
        if not name:
            continue
        if " " in name:
            given, family = name.rsplit(" ", 1)
            authors.append({"family": family, "given": given})
        else:
            authors.append({"literal": name})
    venue = (rec.get("publicationVenue") or {}).get("name") or (rec.get("journal") or {}).get("name") or rec.get("venue") or ""
    arxiv = ext.get("ArXiv", "")
    doi = ext.get("DOI", "")
    url = f"https://doi.org/{doi}" if doi else (f"https://arxiv.org/abs/{arxiv}" if arxiv else rec.get("url", ""))
    return {
        "pmid": str(ext.get("PubMed", "") or ""),
        "pmcid": ext.get("PubMedCentral", ""),
        "doi": doi,
        "arxiv": arxiv,
        "title": _clean_title(rec.get("title", "")),
        "authors": authors,
        "journal": venue or ("arXiv" if arxiv else ""),
        "journal_abbr": "",
        "year": rec.get("year"),
        "volume": (rec.get("journal") or {}).get("volume", "") or "",
        "issue": "",
        "pages": (rec.get("journal") or {}).get("pages", "") or "",
        "language": "",
        "url": url,
        "pub_types": rec.get("_pub_types") or rec.get("publicationTypes") or [],
        "doc_type": "預印本" if arxiv and not venue else "原始研究",
        "abstract": rec.get("abstract") or "",
        "item_type": "preprint" if arxiv and not venue else "journalArticle",
        "metadata_source": f"pool:{rec.get('_source', 's2')}",
    }


# ================================================================ citekeys


def _ascii(text: str) -> str:
    text = text.translate(LETTER_FOLD)
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def make_citekey(ref: dict, existing: set[str]) -> str:
    base = bl.first_author(ref) or (ref.get("title") or "Anon").split()[0]
    base = re.sub(r"[^A-Za-z0-9]", "", _ascii(base))[:30] or "Anon"
    key = f"{base}{ref.get('year') or 'nd'}"
    if key not in existing:
        return key
    for suffix in "bcdefghijklmnopqrstuvwxyz":
        if key + suffix not in existing:
            return key + suffix
    raise RuntimeError(f"too many collisions for {key}")


# ================================================================ quote verification


def _source_variants(bank_dir: Path, ref: dict, kind: str) -> tuple[list[str], str]:
    """Return (normalised source texts to search, problem message)."""
    if kind == "abstract":
        text = ref.get("abstract") or ""
        if not text:
            return [], "這篇沒有摘要文字，無法以 abstract 為來源"
        return [bl.norm_ws(text)], ""
    path = ref.get("fulltext")
    if not path:
        return [], "source 是 fulltext，但這篇沒有設定 fulltext 檔案路徑"
    p = Path(path)
    if not p.is_absolute():
        p = bank_dir / p
    if not p.is_file():
        return [], f"找不到全文檔：{p}"
    raw = p.read_text(encoding="utf-8", errors="replace")
    variants = []
    if p.suffix.lower() in (".xml", ".html", ".htm", ".nxml"):
        variants.append(_strip_tags(raw))
    else:
        variants.append(bl.norm_ws(raw))
        # Markdown full texts (Jina / PMC pages) carry link and emphasis syntax.
        md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", raw)
        variants.append(bl.norm_ws(re.sub(r"[*_`]", "", md)))
        variants.append(_strip_tags(md))
    return variants, ""


def _hint(needle: str, haystack: str) -> str:
    if needle.translate(PUNCT_FOLD) in haystack.translate(PUNCT_FOLD):
        return "只差在引號／破折號字元，請從來源原樣複製"
    if needle.lower() in haystack.lower():
        return "只差在大小寫"
    m = difflib.SequenceMatcher(None, haystack, needle, autojunk=False).find_longest_match(0, len(haystack), 0, len(needle))
    if m.size >= 20:
        end = m.b + m.size
        notes = []
        if m.b > 0:
            notes.append(f"引用句的「{needle[max(0, m.b - 45):m.b]}」對不上，來源在這個位置是「{haystack[max(0, m.a - 45):m.a]}」")
        if end < len(needle):
            notes.append(f"引用句的「{needle[end:end + 45]}」對不上，來源在這個位置是「{haystack[m.a + m.size:m.a + m.size + 45]}」")
        return "有一段吻合，但" + "；".join(notes)
    return "來源裡找不到相近的文字（可能是憑記憶寫的，或來源選錯）"


def check_ref(bank: dict, bank_dir: Path, ref: dict) -> list[str]:
    problems = []
    key = ref.get("citekey", "?")
    sections = bl.section_names(bank)
    if ref.get("category") and ref["category"] not in bank.get("categories", {}):
        problems.append(f"{key}: 類別「{ref['category']}」不在 bank.categories 裡")
    if ref.get("tier") and ref["tier"] not in bl.TIERS:
        problems.append(f"{key}: 層級「{ref['tier']}」不是 {'／'.join(bl.TIERS)}")
    cache: dict[str, tuple[list[str], str]] = {}
    for i, q in enumerate(ref.get("quotes", []), 1):
        tag = f"{key}#{i}"
        if not bl.norm_ws(q.get("claim", "")):
            problems.append(f"{tag}: 沒有寫「可支持的論點」")
        if q.get("section") not in sections:
            problems.append(f"{tag}: 段落「{q.get('section')}」不在 bank.sections 裡")
        src = q.get("source")
        if src == "paraphrase":
            if not bl.norm_ws(q.get("locator", "")):
                problems.append(f"{tag}: 全文摘述要寫 locator（章節／段落／表格位置）")
            if bl.norm_ws(q.get("quote", "")):
                problems.append(f"{tag}: 全文摘述不該帶逐字引用句（有版權的全文只記摘述與位置）")
            continue
        if src not in ("abstract", "fulltext"):
            problems.append(f"{tag}: source 必須是 abstract／fulltext／paraphrase，現在是「{src}」")
            continue
        needle = bl.norm_ws(q.get("quote", ""))
        if not needle:
            problems.append(f"{tag}: 引用句是空的")
            continue
        if src not in cache:
            cache[src] = _source_variants(bank_dir, ref, src)
        variants, err = cache[src]
        if err:
            problems.append(f"{tag}: {err}")
        elif not any(needle in v for v in variants):
            problems.append(f"{tag}: 引用句不是來源的逐字子字串 —— {_hint(needle, variants[0])}")
    return problems


def check_bank(bank: dict, bank_dir: Path, refs: list[dict] | None = None) -> list[str]:
    problems = []
    for ref in refs if refs is not None else bank["refs"]:
        problems += check_ref(bank, bank_dir, ref)
    if refs is None:
        seen: dict[str, str] = {}
        for ref in bank["refs"]:
            for kind, val in (("PMID", ref.get("pmid")), ("DOI", bl.norm_doi(ref.get("doi", "")))):
                if not val:
                    continue
                k = f"{kind}:{val}"
                if k in seen:
                    problems.append(f"{ref['citekey']}: 與 {seen[k]} 重複（{k}）")
                seen[k] = ref["citekey"]
    return problems


# ================================================================ commands


def cmd_init(args) -> None:
    out = Path(args.out)
    if out.exists() and not args.force:
        bl.die(f"{out} 已存在；同主題續作請直接用它，要重建才加 --force")
    bank = {
        "topic": args.topic,
        "topic_en": args.topic_en or "",
        "folder": args.folder,
        "slug": args.slug,
        "tag": args.tag,
        "collection": args.collection or args.topic_en or args.folder,
        "date": args.date or dt.date.today().isoformat(),
        "sections": [],
        "categories": {},
        "review": {"status": "none"},
        "refs": [],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    bl.save_bank(bank, out)
    print(f"已建立 {out}\n接著請在檔內填 sections（稿件段落）與 categories（文獻類別），再用 add 抓書目。")


def _split_ids(text: str | None, file: str | None) -> list[str]:
    raw = text or ""
    if file:
        raw += "\n" + Path(file).read_text(encoding="utf-8")
    return [x for x in re.split(r"[\s,;]+", raw) if x]


def cmd_add(args) -> None:
    bank = bl.load_bank(args.bank)
    have_pmid = {r.get("pmid") for r in bank["refs"] if r.get("pmid")}
    have_doi = {bl.norm_doi(r.get("doi", "")) for r in bank["refs"] if r.get("doi")}
    keys = {r["citekey"] for r in bank["refs"]}
    incoming: list[dict] = []

    pmids = [p for p in _split_ids(args.pmids, args.pmid_file) if p.isdigit()]
    fresh = [p for p in dict.fromkeys(pmids) if p not in have_pmid]
    if fresh:
        got = efetch(fresh)
        missing = set(fresh) - {r["pmid"] for r in got}
        if missing:
            print(f"  PubMed 查不到這些 PMID：{', '.join(sorted(missing))}", file=sys.stderr)
        incoming += got

    for doi in dict.fromkeys(_split_ids(args.dois, None)):
        if bl.norm_doi(doi) in have_doi:
            continue
        rec = fetch_by_doi(doi)
        if rec:
            incoming.append(rec)

    if args.pool:
        wanted = set(_split_ids(args.ids, None))
        if not wanted:
            bl.die("--pool 要搭配 --ids（pool.jsonl 裡的 paperId）")
        for line in Path(args.pool).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("paperId") in wanted:
                incoming.append(from_pool_record(rec))
                wanted.discard(rec.get("paperId"))
        if wanted:
            print(f"  pool 裡找不到：{', '.join(sorted(wanted))}", file=sys.stderr)

    added = []
    for rec in incoming:
        if (rec.get("pmid") and rec["pmid"] in have_pmid) or (rec.get("doi") and bl.norm_doi(rec["doi"]) in have_doi):
            continue
        rec["citekey"] = make_citekey(rec, keys)
        keys.add(rec["citekey"])
        rec.update({"category": args.category or "", "tier": args.tier or "", "note": "", "flags": [], "quotes": []})
        if rec.get("pmid"):
            have_pmid.add(rec["pmid"])
        if rec.get("doi"):
            have_doi.add(bl.norm_doi(rec["doi"]))
        bank["refs"].append(rec)
        added.append(rec)
    bl.save_bank(bank, args.bank)
    for rec in added:
        warn = "" if rec.get("abstract") else "  ← 沒有摘要"
        print(f"+ {rec['citekey']}  {rec.get('pmid') or rec.get('doi') or ''}  {rec['title'][:70]}{warn}")
    print(f"新增 {len(added)} 篇，bank 共 {len(bank['refs'])} 篇。")


def _select(bank: dict, args) -> list[dict]:
    refs = bank["refs"]
    if getattr(args, "citekeys", None):
        wanted = _split_ids(args.citekeys, None)
        by_key = {r["citekey"]: r for r in refs}
        missing = [k for k in wanted if k not in by_key]
        if missing:
            bl.die(f"bank 裡沒有這些引用鍵：{', '.join(missing)}")
        return [by_key[k] for k in wanted]
    if getattr(args, "pending", False):
        # "annotated" is set by `annotate`, so a paper deliberately left without quotes is not shown again.
        refs = [r for r in refs if not r.get("annotated")]
    return refs


def cmd_show(args) -> None:
    bank = bl.load_bank(args.bank)
    refs = _select(bank, args)
    total = len(refs)
    refs = refs[args.offset : args.offset + args.limit] if args.limit else refs[args.offset :]
    print(f"# {total} 篇符合，顯示第 {args.offset + 1}–{args.offset + len(refs)} 篇\n")
    for r in refs:
        ids = " | ".join(x for x in (f"PMID {r['pmid']}" if r.get("pmid") else "", f"DOI {r['doi']}" if r.get("doi") else "") if x)
        print(f"=== {r['citekey']} | {ids} | {r.get('journal_abbr') or r.get('journal', '')} {r.get('year', '')} | {r.get('doc_type', '')}")
        print(f"類別 {r.get('category') or '—'}｜層級 {r.get('tier') or '—'}｜全文檔 {r.get('fulltext') or '—'}")
        print(r.get("title", ""))
        if not args.no_abstract:
            print("ABSTRACT:\n" + (r.get("abstract") or "（沒有摘要）"))
        if r.get("quotes") or r.get("note"):
            print("ANNOTATION:")
            print(json.dumps({"note": r.get("note", ""), "flags": r.get("flags", []), "quotes": r.get("quotes", [])}, ensure_ascii=False, indent=1))
        print()


ANNOTATION_KEYS = {"category", "tier", "doc_type", "note", "flags", "fulltext", "fulltext_read"}
METADATA_KEYS = {"pmid", "pmcid", "doi", "title", "authors", "journal", "journal_abbr", "year", "volume", "issue", "pages", "language", "url", "abstract", "item_type"}


def cmd_annotate(args) -> None:
    bank = bl.load_bank(args.bank)
    data = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
    entries = [dict(v, citekey=k) for k, v in data.items()] if isinstance(data, dict) else data
    by_key = {r["citekey"]: r for r in bank["refs"]}
    by_pmid = {r["pmid"]: r for r in bank["refs"] if r.get("pmid")}
    sections = bl.section_names(bank)
    touched = []
    for e in entries:
        ref = by_key.get(e.get("citekey", "")) or by_pmid.get(str(e.get("pmid", "")))
        if ref is None:
            if not e.get("new"):
                bl.die(f"bank 裡沒有 {e.get('citekey') or e.get('pmid')}。先用 add 抓書目；PubMed／DOI 都查不到的文件才用 \"new\": true 手動建立")
            if not e.get("citekey") or not e.get("title"):
                bl.die("手動建立的文獻至少要有 citekey 與 title")
            ref = {"citekey": e["citekey"], "authors": [], "quotes": [], "flags": [], "note": "", "metadata_source": "manual", "item_type": "journalArticle"}
            ref.update({k: e[k] for k in METADATA_KEYS if k in e})
            bank["refs"].append(ref)
            by_key[ref["citekey"]] = ref
        for k in ANNOTATION_KEYS:
            if k in e:
                ref[k] = e[k]
        if "quotes" in e:
            ref["quotes"] = e["quotes"]
        if "add_quotes" in e:
            ref.setdefault("quotes", []).extend(e["add_quotes"])
        for q in ref.get("quotes", []):
            if args.add_sections and q.get("section") and q["section"] not in sections:
                bank["sections"].append(q["section"])
                sections.append(q["section"])
        ref["annotated"] = True
        touched.append(ref)
    bl.save_bank(bank, args.bank)
    problems = check_bank(bank, Path(args.bank).resolve().parent, touched)
    print(f"已併入 {len(touched)} 篇的標註。")
    if problems:
        print(f"\n這一批有 {len(problems)} 個問題，請修正後重新 annotate：")
        for p in problems:
            print("  ✗ " + p)
        sys.exit(1)
    print("這一批全部通過逐字比對與格式檢查。")


def cmd_drop(args) -> None:
    bank = bl.load_bank(args.bank)
    wanted = set(_split_ids(args.citekeys, None))
    imported = [r["citekey"] for r in bank["refs"] if r["citekey"] in wanted and (r.get("zotero") or {}).get("itemKey")]
    if imported and not args.force:
        bl.die(f"這些已經匯入 Zotero，從 bank 移除不會刪掉 Zotero 裡的項目：{', '.join(imported)}。確定要移除請加 --force")
    before = len(bank["refs"])
    bank["refs"] = [r for r in bank["refs"] if r["citekey"] not in wanted]
    bl.save_bank(bank, args.bank)
    print(f"移除 {before - len(bank['refs'])} 篇，剩 {len(bank['refs'])} 篇。")


def cmd_verify(args) -> None:
    bank = bl.load_bank(args.bank)
    problems = check_bank(bank, Path(args.bank).resolve().parent)
    st = bl.stats(bank)
    print(
        f"{st['refs']} 篇；逐字引用句 {st['quotes_verbatim']} 句（摘要 {st['from_abstract']}、全文 {st['from_fulltext']}），"
        f"全文摘述 {st['paraphrases']} 條；沒有引用句 {st['refs_without_quotes']} 篇；未分類 {st['refs_unclassified']} 篇。"
    )
    if problems:
        print(f"\n{len(problems)} 個問題：")
        for p in problems:
            print("  ✗ " + p)
        sys.exit(1)
    print("全部通過：每一句逐字引用句都是來源的逐字子字串。")


# ---------------------------------------------------------------- export


def _cell(text) -> str:
    return bl.norm_ws(str(text if text is not None else "")).replace("|", "\\|")


def _ieee_name(a: dict) -> str:
    if a.get("literal"):
        return a["literal"]
    initials = " ".join(f"{p[0]}." for p in re.split(r"[\s\-]+", a.get("given", "")) if p)
    return f"{initials} {a.get('family', '')}".strip()


def ieee_reference(ref: dict) -> str:
    authors = ref.get("authors") or []
    if len(authors) >= 3:
        who = f"{_ieee_name(authors[0])} et al."
    else:
        who = " and ".join(_ieee_name(a) for a in authors)
    bits = [f'"{ref.get("title", "")},"']
    journal = ref.get("journal_abbr") or ref.get("journal")
    if journal:
        bits.append(f"*{journal}*,")
    if ref.get("volume"):
        bits.append(f"vol. {ref['volume']},")
    if ref.get("issue"):
        bits.append(f"no. {ref['issue']},")
    if ref.get("pages"):
        bits.append(f"pp. {ref['pages']},")
    bits.append(f"{ref.get('year') or 'n.d.'}.")
    link = f"https://doi.org/{bl.clean_doi(ref['doi'])}" if ref.get("doi") else ref.get("url", "")
    if link:
        bits.append(f"[Online]. Available: {link}")
    return f"{who + ', ' if who else ''}{' '.join(bits)}"


def _fulltext_cell(ref: dict) -> str:
    if ref.get("fulltext"):
        return "已存檔"
    return ref.get("fulltext_read") or "—"


def _stats_paragraph(bank: dict) -> str:
    st = bl.stats(bank)
    s = f"共 {st['refs']} 篇、{st['quotes_verbatim']} 句逐字引用句：{st['from_abstract']} 句取自摘要、{st['from_fulltext']} 句取自全文"
    if st["secondhand"]:
        s += f"，其中 {st['secondhand']} 句是該文對他方文件（指引、政策）的轉述"
    s += "。每一句都經程式比對，確認是來源文字的逐字子字串。"
    if st["paraphrases"]:
        s += f"另有 {st['paraphrases']} 條是讀全文後寫的摘述，只記內容、數字與段落位置，沒有抄錄原句。"
    if st["refs_without_quotes"]:
        s += f"{st['refs_without_quotes']} 篇沒有引用句（需要全文）。"
    s += f"\n\n逐字比對只保證引用句沒抄錯。旁邊的中文論點是 AI 的解讀，{bl.review_phrase(bank)}，沒有逐篇對照全文核對。引用句是原文，放進稿件前要改寫成自己的文字。"
    return s


def build_markdown(bank: dict, narrative: str | None) -> str:
    num = bl.numbering(bank)
    refs = bl.ordered_refs(bank)
    today = dt.date.today().isoformat()
    out = [f"# {bank['topic']}：寫作用引用庫", "", f"最後更新：{today}｜產出者：Claude Code（AI）｜未經人工核對", "", _stats_paragraph(bank), ""]

    if narrative:
        unknown = sorted({k for k in re.findall(r"\[@([^\]\s;,]+)\]", narrative) if k not in num})
        if unknown:
            bl.die(f"總結段落引用了 bank 裡沒有的引用鍵：{', '.join(unknown)}")
        out += [re.sub(r"\[@([^\]\s;,]+)\]", lambda m: f"[{num[m.group(1)]}]", narrative).strip(), ""]

    out += ["## 各段落可引用的句子", "", "依稿件段落排列；同一段落內，核心文獻在前。「來源」欄標示引用句取自摘要、全文，或是該文對他方文件的轉述。", ""]
    for sec in bl.section_names(bank):
        rows = [(r, q) for r in refs for q in r.get("quotes", []) if q.get("section") == sec]
        if not rows:
            continue
        rows.sort(key=lambda rq: (bl.TIERS.index(rq[0]["tier"]) if rq[0].get("tier") in bl.TIERS else 9, num[rq[0]["citekey"]]))
        out += [f"### {sec}", ""]
        if bl.section_hint(bank, sec):
            out += [bl.section_hint(bank, sec), ""]
        out += ["| 可支持的論點 | 文獻 | 逐字引用句（全文摘述則為段落位置） | 來源 |", "|---|---|---|---|"]
        for r, q in rows:
            out.append(f"| {_cell(q.get('claim'))} | {_cell(bl.short_label(r))} [{num[r['citekey']]}] | {_cell(bl.quote_text(q))} | {_cell(bl.source_label(q))} |")
        out.append("")

    st = bl.stats(bank)
    tiers = "、".join(f"{t} {n} 篇" for t, n in st["tiers"].items())
    out += ["## 完整引用庫", "", "「層級」是建議的閱讀與引用優先順序，不是證據品質分級。「類型」取自 PubMed 出版類型，部分經人工修正。", "", f"{st['refs']} 篇，依類別排列。層級：{tiers}。", ""]
    for cat, name in bank.get("categories", {}).items():
        members = [r for r in refs if r.get("category") == cat]
        if not members:
            continue
        out += [f"### {cat}. {name}（{len(members)} 篇）", "", "| 編號 | 引用鍵 | 文獻 | 類型 | 層級 | 引用句 | 全文 | 備註 |", "|---|---|---|---|---|---|---|---|"]
        for r in members:
            venue = f"{r.get('journal_abbr') or r.get('journal', '')} {r.get('year') or ''}".strip()
            out.append(
                f"| [{num[r['citekey']]}] | `{r['citekey']}` | {_cell(r.get('title'))}（{_cell(venue)}） | {_cell(r.get('doc_type'))} | {_cell(r.get('tier'))} | {len(r.get('quotes', []))} | {_cell(_fulltext_cell(r))} | {_cell(r.get('note'))} |"
            )
        out.append("")

    out += ["## 參考文獻", ""]
    for r in refs:
        out += [f"[{num[r['citekey']]}] {ieee_reference(r)}", ""]
    return "\n".join(out).rstrip() + "\n"


def _annotation_lines(bank: dict, ref: dict) -> list[str]:
    lines = [f"[{q.get('section', '')}] {q.get('claim', '')} ── “{bl.quote_text(q)}”（{bl.source_label(q)}）" for q in ref.get("quotes", [])]
    if ref.get("note"):
        lines.append(f"備註：{ref['note']}")
    return lines


def _bib_escape(text: str) -> str:
    return re.sub(r"([%&#_$])", r"\\\1", str(text or "").replace("\\", "").replace("{", "\\{").replace("}", "\\}"))


def build_bibtex(bank: dict) -> str:
    out = []
    for r in bl.ordered_refs(bank):
        authors = " and ".join("{" + a["literal"] + "}" if a.get("literal") else f"{a.get('family', '')}, {a.get('given', '')}".strip(", ") for a in r.get("authors") or [])
        kind = "misc" if r.get("item_type") == "preprint" else "article"
        fields = [
            ("author", _bib_escape(authors).replace("\\{", "{").replace("\\}", "}")),
            ("title", "{" + _bib_escape(r.get("title", "")) + "}"),
            ("journal", _bib_escape(r.get("journal_abbr") or r.get("journal", ""))),
            ("year", str(r.get("year") or "")),
            ("volume", r.get("volume", "")),
            ("number", r.get("issue", "")),
            ("pages", str(r.get("pages", "")).replace("-", "--", 1) if "--" not in str(r.get("pages", "")) else r.get("pages", "")),
            ("doi", r.get("doi", "")),
            ("pmid", r.get("pmid", "")),
            ("keywords", _bib_escape("; ".join(bl.ref_tags(bank, r)))),
            ("annote", _bib_escape(" || ".join(_annotation_lines(bank, r)))),
        ]
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields if v)
        out.append(f"@{kind}{{{r['citekey']},\n{body}\n}}\n")
    return "\n".join(out)


def build_ris(bank: dict) -> str:
    out = []
    for r in bl.ordered_refs(bank):
        lines = ["TY  - JOUR"]
        for a in r.get("authors") or []:
            lines.append("AU  - " + (a["literal"] if a.get("literal") else f"{a.get('family', '')}, {a.get('given', '')}".strip(", ")))
        lines.append(f"TI  - {r.get('title', '')}")
        if r.get("journal"):
            lines.append(f"JO  - {r['journal']}")
        if r.get("journal_abbr"):
            lines.append(f"J2  - {r['journal_abbr']}")
        if r.get("year"):
            lines.append(f"PY  - {r['year']}")
        if r.get("volume"):
            lines.append(f"VL  - {r['volume']}")
        if r.get("issue"):
            lines.append(f"IS  - {r['issue']}")
        if r.get("pages"):
            sp, _, ep = str(r["pages"]).partition("-")
            lines.append(f"SP  - {sp}")
            if ep:
                lines.append(f"EP  - {ep.lstrip('-')}")
        if r.get("doi"):
            lines.append(f"DO  - {r['doi']}")
        if r.get("pmid"):
            lines.append(f"AN  - {r['pmid']}")
        link = f"https://doi.org/{bl.clean_doi(r['doi'])}" if r.get("doi") else r.get("url", "")
        if link:
            lines.append(f"UR  - {link}")
        lines += [f"KW  - {t}" for t in bl.ref_tags(bank, r)]
        lines += [f"N1  - {bl.norm_ws(n)}" for n in _annotation_lines(bank, r)]
        lines += [f"ID  - {r['citekey']}", "ER  - ", ""]
        out.append("\n".join(lines))
    return "\n".join(out)


def write_csv(bank: dict, path: Path) -> None:
    num = bl.numbering(bank)
    header = ["編號", "引用鍵", "PMID", "DOI", "第一作者", "年份", "期刊", "文件類型", "類別", "層級", "建議段落", "可支持的論點", "逐字引用句", "引用句來源", "段落位置", "備註"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in bl.ordered_refs(bank):
            base = [num[r["citekey"]], r["citekey"], r.get("pmid", ""), r.get("doi", ""), bl.first_author(r), r.get("year", ""), r.get("journal_abbr") or r.get("journal", ""), r.get("doc_type", ""), bl.category_label(bank, r), r.get("tier", "")]
            quotes = r.get("quotes") or [{}]
            for q in quotes:
                w.writerow(base + [q.get("section", ""), q.get("claim", ""), q.get("quote", ""), bl.source_label(q) if q else "", q.get("locator", ""), r.get("note", "")])


def cmd_export(args) -> None:
    bank = bl.load_bank(args.bank)
    bank_dir = Path(args.bank).resolve().parent
    problems = check_bank(bank, bank_dir)
    unclassified = [r["citekey"] for r in bank["refs"] if not r.get("category") or not r.get("tier")]
    if unclassified:
        problems.append(f"{len(unclassified)} 篇還沒有類別或層級：{', '.join(unclassified[:15])}{'…' if len(unclassified) > 15 else ''}（補上，或用 drop 移除）")
    if not bl.section_names(bank) or not bank.get("categories"):
        problems.append("bank.sections 或 bank.categories 是空的")
    if problems and not args.force:
        print(f"有 {len(problems)} 個問題，沒有匯出：")
        for p in problems[:60]:
            print("  ✗ " + p)
        sys.exit(1)
    out_dir = Path(args.out_dir) if args.out_dir else bank_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{bank['date']}-{bank['slug']}"
    narrative = Path(args.narrative).read_text(encoding="utf-8") if args.narrative else None
    files = {
        f"{stem}-citation-bank.md": build_markdown(bank, narrative),
        f"{stem}.bib": build_bibtex(bank),
        f"{stem}.ris": build_ris(bank),
    }
    for name, text in files.items():
        (out_dir / name).write_text(text, encoding="utf-8")
    write_csv(bank, out_dir / f"{stem}-citation-bank.csv")
    print("已寫出：")
    for name in [*files, f"{stem}-citation-bank.csv"]:
        print(f"  {out_dir / name}")
    if not narrative:
        print("（沒有給 --narrative，主報告只有引用句表與完整引用庫，沒有總結段落）")


# ---------------------------------------------------------------- review packet

PACKET_HEADER = """# 送審封包：{topic} —— 寫作用引用庫

> 所有材料都已內嵌在這份文件裡。**不要讀磁碟上的檔案，也不要上網查證**；以這裡附的摘要與全文段落為準。
> 材料不足以判斷時，直接標「材料不足」，不要猜，也不要憑記憶補充文獻內容。

## 這是什麼

AI 為稿件寫作整理的引用庫，共 {n_refs} 篇、{n_rows} 列。每一列是：一句英文逐字引用句（已由程式確認是摘要或全文的逐字子字串）、一句中文「可支持的論點」（AI 的解讀）、建議放的稿件段落。
逐字引用句本身不必查錯字。要查的是**中文論點有沒有超出引用句與摘要所能支持的範圍**，以及總結段落的結論。
這些附註之後會匯入文獻管理軟體，匯入後很難修改，所以請在這一輪把問題找出來。

## 請你做的事

1. **逐列檢查中文論點。** 論點是否完全被同列引用句（必要時加上同一篇的摘要）支持？特別留意濃縮時走樣的地方：研究終點、比較組、時間範圍、族群與適用條件；「未檢出顯著差異」被寫成「沒有差異」或「風險相同」；數字、分母、單位、信賴區間；作者的結論被寫成既定事實；轉述他方指引或政策的句子沒有標明是轉述；論點加進了來源沒有的資訊。
2. **檢查引用句是否截得太短**，以致缺分母、缺比較組或缺終點定義。若摘要裡有該補的句子，請指出是哪一句。
3. **檢查建議段落、類別與層級**是否明顯不當。
4. **檢查總結段落**（若有）：每個結論是否有列可依；有沒有把「本庫沒找到」寫成「文獻不存在」；有沒有把可能重疊的世代算成多份獨立證據；各研究的定義不同時有沒有被合併陳述。
5. **指出明顯缺漏的證據類型或該補查的方向。** 只說方向；不要憑記憶列出具體書目。

## 輸出格式（繁體中文）

先給總評（3 到 6 句）。接著一張表，只列有問題的列：

| 引用鍵#列 | 問題 | 嚴重度（高／中／低） | 建議改寫 |

再列對總結段落的意見，最後列「材料不足、無法判斷」的項目。
"""


def _context_window(bank_dir: Path, ref: dict, quote: str, width: int = 500) -> str:
    variants, err = _source_variants(bank_dir, ref, "fulltext")
    if err:
        return ""
    needle = bl.norm_ws(quote)
    for v in variants:
        pos = v.find(needle)
        if pos >= 0:
            return v[max(0, pos - width) : pos + len(needle) + width]
    return ""


def cmd_packet(args) -> None:
    bank = bl.load_bank(args.bank)
    bank_dir = Path(args.bank).resolve().parent
    problems = check_bank(bank, bank_dir)
    if problems and not args.force:
        bl.die(f"bank 還有 {len(problems)} 個逐字比對或格式問題，先跑 verify 修好再送審（送審要審的是論點，不是抄錯的引用句）")
    num = bl.numbering(bank)
    refs = bl.ordered_refs(bank)
    n_rows = sum(len(r.get("quotes", [])) for r in refs)
    out = [PACKET_HEADER.format(topic=bank["topic"], n_refs=len(refs), n_rows=n_rows)]
    out += ["## 稿件段落", ""] + [f"- {s}" for s in bl.section_names(bank)] + [""]
    out += ["## 文獻類別", ""] + [f"- {k}. {v}" for k, v in bank.get("categories", {}).items()] + ["", "層級：核心／重要佐證／補充，是閱讀與引用的優先順序，不是證據品質。", ""]
    if args.narrative:
        text = Path(args.narrative).read_text(encoding="utf-8")
        text = re.sub(r"\[@([^\]\s;,]+)\]", lambda m: f"[{m.group(1)}]", text)
        out += ["## 總結段落（請一併審查）", "", text.strip(), ""]
    out += ["## 逐篇材料", ""]
    for r in refs:
        ids = "｜".join(x for x in (f"PMID {r['pmid']}" if r.get("pmid") else "", f"DOI {r['doi']}" if r.get("doi") else "") if x)
        venue = f"{r.get('journal_abbr') or r.get('journal', '')} {r.get('year') or ''}".strip()
        out += [f"### [{num[r['citekey']]}] {r['citekey']} —— {r.get('title', '')}（{venue}）", "", f"{ids}｜類型 {r.get('doc_type', '')}｜類別 {r.get('category', '')}｜層級 {r.get('tier', '')}", ""]
        include_abstract = args.abstracts == "all" or (args.abstracts == "core" and r.get("tier") == "核心")
        if include_abstract and r.get("abstract"):
            out += ["摘要：", "", "> " + r["abstract"].replace("\n", "\n> "), ""]
        elif not r.get("abstract"):
            out += ["（這篇沒有摘要）", ""]
        for i, q in enumerate(r.get("quotes", []), 1):
            out += [f"- **{r['citekey']}#{i}**｜段落：{q.get('section', '')}｜來源：{bl.source_label(q)}", f"  - 論點：{q.get('claim', '')}"]
            if q.get("source") == "paraphrase":
                out.append(f"  - 全文摘述，沒有逐字引用句；位置：{q.get('locator', '')}（你看不到全文，這類列只需檢查論點本身是否自相矛盾或與摘要衝突）")
            else:
                out.append(f"  - 引用句：{q.get('quote', '')}")
                if q.get("source") == "fulltext":
                    ctx = _context_window(bank_dir, r, q.get("quote", ""))
                    if ctx:
                        out.append(f"  - 全文前後文（節錄）：…{ctx}…")
        if not r.get("quotes"):
            out.append("- （沒有引用句）")
        if r.get("note"):
            out.append(f"- 備註：{r['note']}")
        out.append("")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    packet = "\n".join(out)
    (out_dir / "packet.md").write_text(packet, encoding="utf-8")
    scope = (
        f"# 審查對象\n\n{bank['topic']} 的寫作用引用庫（{len(refs)} 篇、{n_rows} 列引用句與中文論點）。\n\n"
        "這不是程式碼變更。完整材料與審查指示都在下方的 Host Context Note 裡，已全部內嵌；不要讀磁碟，不要上網。\n"
    )
    (out_dir / "scope.md").write_text(scope, encoding="utf-8")
    size_kb = len(packet.encode("utf-8")) / 1024
    posix = bl.bash_path(out_dir.resolve())
    print(f"已寫出 {out_dir / 'packet.md'}（{size_kb:.0f} KB）與 scope.md")
    if size_kb > 320:
        print("封包超過 320 KB：考慮用 --abstracts core，或分兩批送審（先核心與重要佐證）。")
    script = bl.find_cross_review_script()
    if script:
        script_arg = bl.bash_path(script)
    else:
        script_arg = "$HOME/.claude/skills/cross-review/scripts/run-cross-review.sh"
        print(
            "\n注意：找不到 cross-review skill 的 run-cross-review.sh。安裝方式見 README（或在 config.json 設 cross_review_script）；"
            "沒有它就無法送審，見 references/cross-review.md 的失敗處理。"
        )
    print("\n送審指令（用 Bash 工具、run_in_background）：\n")
    print(
        f'bash "{script_arg}" --repo "{posix}" --path scope.md '
        f'--context-file "{posix}/packet.md" --output-dir "{posix}/run" --timeout-seconds 2400 '
        "--question '依 Host Context Note 的指示審查這份引用庫：逐列檢查中文論點是否被同列逐字引用句與內嵌摘要支持，並審查總結段落。以繁體中文、依指定格式回覆。'"
    )


def cmd_mark_reviewed(args) -> None:
    bank = bl.load_bank(args.bank)
    bank["review"] = {
        "status": args.status,
        "reviewer": args.reviewer or "",
        "date": args.date or dt.date.today().isoformat(),
        "record": args.record or "",
    }
    bl.save_bank(bank, args.bank)
    print(f"review = {json.dumps(bank['review'], ensure_ascii=False)}")


def cmd_stats(args) -> None:
    bank = bl.load_bank(args.bank)
    print(json.dumps(bl.stats(bank), ensure_ascii=False, indent=1))


def cmd_config(args) -> None:
    cfg = bl.load_config()
    path = bl.config_path()
    print(f"設定檔：{path}" + ("" if path.is_file() else "（不存在，以下是預設值；第一次使用請跑 doctor.py）"))
    script = bl.find_cross_review_script(cfg)
    cfg["cross_review_script"] = str(script) if script else "（找不到）"
    print(json.dumps(cfg, ensure_ascii=False, indent=1))


# ================================================================ CLI


def main() -> None:
    bl.utf8_streams()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="建立空的 bank.json")
    p.add_argument("--out", required=True)
    p.add_argument("--topic", required=True, help="中文題目")
    p.add_argument("--topic-en", help="英文題目（預設當 Zotero 分類名稱）")
    p.add_argument("--folder", required=True, help="主題資料夾名，例如 Early-Vasopressor-Sepsis")
    p.add_argument("--slug", required=True, help="檔名用 slug，例如 early-vasopressor-sepsis")
    p.add_argument("--tag", required=True, help="Zotero 標籤前綴，例如 EVS")
    p.add_argument("--collection", help="Zotero 分類名稱")
    p.add_argument("--date", help="YYYY-MM-DD（預設今天）")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("add", help="抓書目與摘要進 bank")
    p.add_argument("--bank", required=True)
    p.add_argument("--pmids", help="逗號或空白分隔")
    p.add_argument("--pmid-file")
    p.add_argument("--dois", help="PubMed 沒收的文獻：Europe PMC → Crossref")
    p.add_argument("--pool", help="paper-navigator 的 pool.jsonl（非 PubMed 的紀錄用）")
    p.add_argument("--ids", help="搭配 --pool：要取的 paperId")
    p.add_argument("--category")
    p.add_argument("--tier", choices=bl.TIERS)
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("show", help="分批印出摘要與現有標註")
    p.add_argument("--bank", required=True)
    p.add_argument("--citekeys")
    p.add_argument("--pending", action="store_true", help="只看還沒有引用句的")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--no-abstract", action="store_true")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("annotate", help="併入標註（類別、層級、引用句）")
    p.add_argument("--bank", required=True)
    p.add_argument("--from", dest="from_file", required=True, help="標註 JSON（list 或以引用鍵為 key 的 dict）")
    p.add_argument("--add-sections", action="store_true", help="標註裡出現的新段落名稱自動加進 bank.sections")
    p.set_defaults(fn=cmd_annotate)

    p = sub.add_parser("drop", help="從 bank 移除文獻")
    p.add_argument("--bank", required=True)
    p.add_argument("--citekeys", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_drop)

    p = sub.add_parser("verify", help="逐字比對與格式檢查")
    p.add_argument("--bank", required=True)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("export", help="寫出 .md／.csv／.bib／.ris")
    p.add_argument("--bank", required=True)
    p.add_argument("--out-dir", help="預設與 bank 同資料夾")
    p.add_argument("--narrative", help="總結段落的 Markdown；文獻用 [@引用鍵] 標示")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("packet", help="寫出交叉評審封包")
    p.add_argument("--bank", required=True)
    p.add_argument("--out-dir", required=True, help="放在 scratchpad，例如 <scratchpad>/xr")
    p.add_argument("--narrative")
    p.add_argument("--abstracts", choices=["all", "core", "none"], default="all")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_packet)

    p = sub.add_parser("mark-reviewed", help="記錄交叉評審已完成")
    p.add_argument("--bank", required=True)
    p.add_argument("--reviewer", help="例如 'Codex CLI <版本>（<模型>，<effort>）'")
    p.add_argument("--date")
    p.add_argument("--record", help="交叉評審紀錄檔名")
    p.add_argument("--status", default="done", choices=["done", "none", "failed"])
    p.set_defaults(fn=cmd_mark_reviewed)

    p = sub.add_parser("stats", help="印出統計（JSON）")
    p.add_argument("--bank", required=True)
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("config", help="印出目前使用的設定（輸出位置、Obsidian 同步、工具路徑）")
    p.set_defaults(fn=cmd_config)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
