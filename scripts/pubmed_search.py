#!/usr/bin/env python3
"""Search biomedical literature via PubMed (NCBI E-utilities) and Europe PMC.

Bundled with reference-search so clinical topics work without any extra
package: standard library only. The command line and the record shape match
the `pubmed_search.py` some paper-navigator installs carry, so either copy can
feed the same pool.jsonl and `refbank.py add --pool`.

Both backends are keyless:
  - PubMed / NCBI E-utilities — 3 req/s (10 with NCBI_API_KEY). Authoritative
    for MeSH terms and publication types, which is how you filter for
    Meta-Analysis / RCT / Systematic Review reliably.
  - Europe PMC — no published rate limit, better coverage of non-English and
    non-MEDLINE journals, and returns citation counts + open-access status.

Reproducibility: PubMed rewrites your query (MeSH explosion, term mapping).
The rewritten string is what belongs in a Methods section, so it is always
reported on stderr and stored on every record as `_query_translation`.

Examples:
    # Meta-analyses only, with the translated query for your Methods section
    python pubmed_search.py -q "regional citrate anticoagulation" --pub-type ma

    # Both backends into a shared pool
    python pubmed_search.py -q "citrate calcium-containing replacement" \
        --source both --limit 30 --output pool.jsonl --append

    # Just show what PubMed actually searched for
    python pubmed_search.py -q "sepsis fluid resuscitation" --print-query
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import banklib as bl  # noqa: E402

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
UA = "reference-search-skill/1.0 (Claude Code; python-urllib)"

# Shorthand → PubMed publication-type term. [pt] is an exact controlled
# vocabulary, so this is far more reliable than putting "meta-analysis" in the
# free text (which also matches papers that merely cite one).
PUB_TYPES = {
    "ma": ["Meta-Analysis"],
    "sr": ["Systematic Review"],
    "rct": ["Randomized Controlled Trial"],
    "ct": ["Clinical Trial"],
    "review": ["Review"],
    "guideline": ["Guideline", "Practice Guideline"],
    "observational": ["Observational Study"],
    "cohort": ["Observational Study"],
    "case-report": ["Case Reports"],
}

_LAST_NCBI_CALL = 0.0


def _get(url: str, params: dict, timeout: int = 45) -> bytes:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"User-Agent": UA})
    last: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise SystemExit(f"Error: HTTP {exc.code} from {url}")
            wait = int(exc.headers.get("Retry-After") or 3 * 2**attempt)
            print(f"HTTP {exc.code}; waiting {wait}s before retry…", file=sys.stderr)
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            time.sleep(3 * 2**attempt)
    raise SystemExit(f"Error: request failed after retries: {url} ({last})")


def _ncbi_params(extra: dict) -> dict:
    """Common E-utilities params. NCBI asks callers to identify themselves."""
    p = {"tool": "reference-search", **extra}
    # Only sent if the user opted in by setting the env var — never inferred.
    if os.environ.get("NCBI_EMAIL"):
        p["email"] = os.environ["NCBI_EMAIL"]
    if os.environ.get("NCBI_API_KEY"):
        p["api_key"] = os.environ["NCBI_API_KEY"]
    return p


def _pace_ncbi() -> None:
    """NCBI throttles at 3 req/s without a key, 10 req/s with one."""
    global _LAST_NCBI_CALL
    interval = 0.11 if os.environ.get("NCBI_API_KEY") else 0.35
    delta = time.monotonic() - _LAST_NCBI_CALL
    if delta < interval:
        time.sleep(interval - delta)
    _LAST_NCBI_CALL = time.monotonic()


def build_query(query: str, year_min: int | None = None, year_max: int | None = None, pub_types: list[str] | None = None) -> str:
    """Compose a PubMed query string from the query plus filters."""
    parts = [f"({query})"]
    if pub_types:
        terms = [f'"{t}"[pt]' for s in pub_types for t in PUB_TYPES.get(s.lower(), [s])]
        parts.append("(" + " OR ".join(terms) + ")")
    if year_min or year_max:
        parts.append(f'("{year_min or 1800}"[dp] : "{year_max or 3000}"[dp])')
    return " AND ".join(parts)


def _text(node: ET.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    el = node.find(path)
    return "".join(el.itertext()).strip() if el is not None else default


def _parse_article(art: ET.Element, query_translation: str) -> dict:
    """Convert one PubmedArticle XML node into an S2-compatible record."""
    medline = art.find("MedlineCitation")
    article = medline.find("Article") if medline is not None else None
    pmid = _text(medline, "PMID")

    # Keep section labels (BACKGROUND/METHODS/...): for triage "RESULTS: ..." is worth more than raw prose.
    abstract_parts = []
    if article is not None:
        for ab in article.findall(".//Abstract/AbstractText"):
            label = ab.get("Label")
            body = "".join(ab.itertext()).strip()
            if body:
                abstract_parts.append(f"{label}: {body}" if label else body)

    authors = []
    if article is not None:
        for a in article.findall(".//AuthorList/Author"):
            collective = _text(a, "CollectiveName")
            if collective:
                authors.append({"name": collective})
                continue
            name = " ".join(x for x in (_text(a, "ForeName") or _text(a, "Initials"), _text(a, "LastName")) if x)
            if name:
                authors.append({"name": name})

    year = None
    if article is not None:
        # MedlineDate is free text like "2008 Jul-Aug" or "2008-2009"
        y = _text(article, ".//Journal/JournalIssue/PubDate/Year") or _text(article, ".//Journal/JournalIssue/PubDate/MedlineDate")[:4]
        try:
            year = int(y)
        except (TypeError, ValueError):
            year = None

    doi = pmc = ""
    for aid in art.findall(".//ArticleIdList/ArticleId"):
        kind, val = aid.get("IdType"), (aid.text or "").strip()
        if kind == "doi" and not doi:
            doi = val
        elif kind == "pmc" and not pmc:
            pmc = val
    if not doi and article is not None:
        for el in article.findall("ELocationID"):
            if el.get("EIdType") == "doi":
                doi = (el.text or "").strip()
                break

    pub_types = [(pt.text or "").strip() for pt in art.findall(".//PublicationTypeList/PublicationType") if (pt.text or "").strip()]
    mesh = [(d.text or "").strip() for d in art.findall(".//MeshHeadingList/MeshHeading/DescriptorName") if (d.text or "").strip()]
    journal = (_text(article, ".//Journal/Title") or _text(article, ".//Journal/ISOAbbreviation")) if article is not None else ""

    ext_ids = {"PubMed": pmid}
    if doi:
        ext_ids["DOI"] = doi
    if pmc:
        ext_ids["PubMedCentral"] = pmc
    pdf_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc}/" if pmc else ""

    return {
        "paperId": f"pmid:{pmid}",
        "externalIds": ext_ids,
        "title": _text(article, "ArticleTitle") if article is not None else "",
        "authors": authors,
        "year": year,
        # NCBI does not expose citation counts; None (not 0) so triage never reads "unknown" as "uncited".
        "citationCount": None,
        "influentialCitationCount": None,
        "tldr": None,
        "isOpenAccess": bool(pmc),
        "openAccessPdf": {"url": pdf_url} if pdf_url else None,
        "publicationVenue": {"name": journal} if journal else None,
        "abstract": "\n".join(abstract_parts),
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "_source": "pubmed",
        "_pub_types": pub_types,
        "_mesh": mesh,
        "_query_translation": query_translation,
    }


def search_pubmed(query: str, limit: int = 20, year_min: int | None = None, year_max: int | None = None,
                  pub_types: list[str] | None = None, sort: str = "relevance") -> tuple[list[dict], str, int]:
    """Search PubMed. Returns (records, translated_query, total_hits)."""
    term = build_query(query, year_min, year_max, pub_types)
    _pace_ncbi()
    data = json.loads(_get(f"{EUTILS_BASE}/esearch.fcgi", _ncbi_params({
        "db": "pubmed", "term": term, "retmax": min(limit, 200), "retmode": "json",
        "sort": {"relevance": "relevance", "date": "pub_date"}.get(sort, "relevance"),
    })))
    result = data.get("esearchresult", {})
    pmids = result.get("idlist", []) or []
    translation = result.get("querytranslation", term)
    total = int(result.get("count", 0) or 0)
    if not pmids:
        return [], translation, total

    _pace_ncbi()
    root = ET.fromstring(_get(f"{EUTILS_BASE}/efetch.fcgi", _ncbi_params({"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"})))
    records = [_parse_article(a, translation) for a in root.findall(".//PubmedArticle")]
    # efetch returns PMID order, not relevance order; restore esearch's ranking or the top hits get buried.
    order = {p: i for i, p in enumerate(pmids)}
    records.sort(key=lambda r: order.get(r["externalIds"].get("PubMed", ""), 999))
    return records, translation, total


def search_europepmc(query: str, limit: int = 20, year_min: int | None = None, year_max: int | None = None,
                     pub_types: list[str] | None = None) -> list[dict]:
    """Search Europe PMC. Wider journal coverage; carries citation counts."""
    parts = [f"({query})"]
    if pub_types:
        terms = [f'PUB_TYPE:"{t}"' for s in pub_types for t in PUB_TYPES.get(s.lower(), [s])]
        parts.append("(" + " OR ".join(terms) + ")")
    if year_min or year_max:
        parts.append(f"(PUB_YEAR:[{year_min or 1800} TO {year_max or 3000}])")
    term = " AND ".join(parts)
    data = json.loads(_get(EPMC_BASE, {"query": term, "format": "json", "resultType": "core", "pageSize": min(limit, 100)}))

    records = []
    for r in (data.get("resultList", {}) or {}).get("result", []) or []:
        pmid, pmcid, doi = r.get("pmid", ""), r.get("pmcid", ""), r.get("doi", "")
        ext_ids = {k: v for k, v in (("PubMed", pmid), ("DOI", doi), ("PubMedCentral", pmcid)) if v}
        try:
            year = int(r.get("pubYear"))
        except (TypeError, ValueError):
            year = None
        journal = ((r.get("journalInfo") or {}).get("journal") or {}).get("title", "")
        pub_type_list = (r.get("pubTypeList") or {}).get("pubType") or []
        if isinstance(pub_type_list, str):
            pub_type_list = [pub_type_list]
        pdf_url = next((ft["url"] for ft in ((r.get("fullTextUrlList") or {}).get("fullTextUrl") or []) if ft.get("documentStyle") == "pdf" and ft.get("url")), "")
        records.append({
            # Same paperId space as PubMed when a PMID exists, so one paper from both backends collapses to one row.
            "paperId": f"pmid:{pmid}" if pmid else f"epmc:{r.get('source', 'MED')}:{r.get('id', '')}",
            "externalIds": ext_ids,
            "title": (r.get("title") or "").strip(),
            "authors": [{"name": n.strip()} for n in (r.get("authorString") or "").split(",") if n.strip()],
            "year": year,
            "citationCount": r.get("citedByCount"),
            "influentialCitationCount": None,
            "tldr": None,
            "isOpenAccess": r.get("isOpenAccess") == "Y",
            "openAccessPdf": {"url": pdf_url} if pdf_url else None,
            "publicationVenue": {"name": journal} if journal else None,
            "abstract": (r.get("abstractText") or "").strip(),
            "url": f"https://europepmc.org/article/{r.get('source', 'MED')}/{r.get('id', '')}",
            "_source": "europepmc",
            "_pub_types": pub_type_list,
            "_mesh": [],
            "_query_translation": term,
        })
    return records


def merge(primary: list[dict], secondary: list[dict]) -> list[dict]:
    """Merge two backends, preferring the first. Matches on paperId then DOI.

    A Europe PMC duplicate is not discarded outright: its citation count and
    open-access link are folded into the kept PubMed record.
    """
    by_id = {r["paperId"]: r for r in primary}
    by_doi = {(r.get("externalIds") or {}).get("DOI", "").lower(): r for r in primary if (r.get("externalIds") or {}).get("DOI")}
    merged = list(primary)
    for r in secondary:
        doi = (r.get("externalIds") or {}).get("DOI", "").lower()
        dup = by_id.get(r["paperId"]) or (by_doi.get(doi) if doi else None)
        if dup is None:
            merged.append(r)
            continue
        if dup.get("citationCount") is None and r.get("citationCount") is not None:
            dup["citationCount"] = r["citationCount"]
            dup["_citation_source"] = "europepmc"
        if not dup.get("openAccessPdf") and r.get("openAccessPdf"):
            dup["openAccessPdf"] = r["openAccessPdf"]
            dup["isOpenAccess"] = True
    return merged


def _truncate(text: str, n: int = 280) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n].rstrip() + "…"


def format_paper(p: dict, i: int) -> str:
    authors = [a["name"] for a in (p.get("authors") or [])]
    author_str = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
    venue = (p.get("publicationVenue") or {}).get("name", "Unknown venue")
    cites = p.get("citationCount")
    cite_str = "N/A (PubMed)" if cites is None else f"**{cites}**"
    ext = p.get("externalIds") or {}
    ids = [f"{label}: `{ext[k]}`" for k, label in (("PubMed", "PMID"), ("DOI", "DOI"), ("PubMedCentral", "PMC")) if ext.get(k)]
    # Publication types drive study-design triage — surface them.
    design = [t for t in p.get("_pub_types") or [] if t not in {"Journal Article", "English Abstract", "Research Support, Non-U.S. Gov't"}]
    design_line = f"\n🏷 {' · '.join(design)}" if design else ""
    abstract = f"\n> {_truncate(p['abstract'])}" if p.get("abstract") else ""
    pdf = f"\n📄 [Full text]({p['openAccessPdf']['url']})" if (p.get("openAccessPdf") or {}).get("url") else ""
    return (
        f"## {i}. {p.get('title', '')}\n**{author_str}** ({p.get('year') or 'n.d.'}) — {venue}\n"
        f"Citations: {cite_str} | {' | '.join(ids)}{design_line}{abstract}{pdf}\n*(via {p.get('_source', '?')})*\n"
    )


def emit(records: list[dict], args, title: str) -> None:
    if args.output:
        out = Path(args.output)
        existing: set[str] = set()
        if args.append and out.is_file() and not args.no_dedup:
            for line in out.read_text(encoding="utf-8").splitlines():
                try:
                    existing.add(json.loads(line).get("paperId", ""))
                except json.JSONDecodeError:
                    pass
        seen, kept = set(existing), []
        for r in records:
            if not args.no_dedup and r["paperId"] in seen:
                continue
            seen.add(r["paperId"])
            kept.append(r)
        with out.open("a" if args.append else "w", encoding="utf-8") as fh:
            for r in kept:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        dropped = len(records) - len(kept)
        total = sum(1 for _ in out.open(encoding="utf-8"))
        print(f"Wrote {len(kept)} papers to {out}" + (f" ({dropped} already in file)" if dropped else "") + (f" ({total} total)" if args.append else ""), file=sys.stderr)
        print(json.dumps({"status": "ok", "written": len(kept), "total": total, "output": str(out), "appended": bool(args.append)}))
    elif args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
    else:
        print(f"# {title}\n")
        for i, r in enumerate(records, 1):
            print(format_paper(r, i))


def main() -> None:
    bl.utf8_streams()
    parser = argparse.ArgumentParser(description="Search PubMed / Europe PMC for biomedical literature")
    parser.add_argument("--query", "-q", required=True, help="Search query")
    parser.add_argument("--limit", "-l", type=int, default=20, help="Max results")
    parser.add_argument("--year-min", type=int, help="Earliest publication year")
    parser.add_argument("--year-max", type=int, help="Latest publication year")
    parser.add_argument("--pub-type", help="Comma-separated study-design filter. Shorthands: " + ", ".join(sorted(PUB_TYPES))
                        + ". Any other value is passed through as a literal PubMed [pt] term.")
    parser.add_argument("--source", choices=["pubmed", "europepmc", "both"], default="pubmed",
                        help="Backend (default pubmed). 'both' merges, preferring PubMed and borrowing Europe PMC citation counts.")
    parser.add_argument("--sort", choices=["relevance", "date"], default="relevance", help="Sort order")
    parser.add_argument("--print-query", action="store_true",
                        help="Print the query PubMed actually ran, then exit. Use this string in the Methods section — it is what is reproducible, not your input.")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--output", "-o", help="Write results to a JSONL file instead of stdout")
    parser.add_argument("--append", action="store_true", help="Append to --output (records already in the file are skipped)")
    parser.add_argument("--no-dedup", action="store_true", help="With --append, keep records already in the file")
    args = parser.parse_args()

    pub_types = [t.strip() for t in args.pub_type.split(",") if t.strip()] if args.pub_type else None

    if args.print_query:
        _, translation, total = search_pubmed(args.query, 1, args.year_min, args.year_max, pub_types, args.sort)
        print(f"# PubMed query translation\n\n```\n{translation}\n```\n")
        print(f"Total hits: **{total}**")
        return

    records: list[dict] = []
    if args.source in ("pubmed", "both"):
        records, translation, total = search_pubmed(args.query, args.limit, args.year_min, args.year_max, pub_types, args.sort)
        print(f"PubMed: {total} total hits, retrieved {len(records)}", file=sys.stderr)
        print(f"PubMed query translation: {translation}", file=sys.stderr)
    if args.source in ("europepmc", "both"):
        epmc = search_europepmc(args.query, args.limit, args.year_min, args.year_max, pub_types)
        print(f"Europe PMC: retrieved {len(epmc)}", file=sys.stderr)
        records = merge(records, epmc) if records else epmc

    if not records:
        print(f"No results for '{args.query}'", file=sys.stderr)
        return
    emit(records[: args.limit], args, f'Biomedical search: "{args.query}"')


if __name__ == "__main__":
    main()
