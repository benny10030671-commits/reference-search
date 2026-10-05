#!/usr/bin/env python3
"""zotero_import.py — put a citation bank into a named Zotero collection.

Talks to the running Zotero desktop app on 127.0.0.1:23119:
  - connector server (/connector/*)  — the only write path; it can ADD items,
    it cannot edit or delete anything that is already in the library
  - local API (/api/users/0/*)       — read-only; used for duplicate checks and
    for reading everything back after the import

Subcommands
    check    read-only: is Zotero reachable, does the collection exist, which
             references are new / already there / elsewhere in the library
    import   create the collection if needed, save new items (each with its
             quote note and tags), move them into the collection, read back
    verify   read-only: compare the collection against the bank and the log

Because imported notes cannot be changed afterwards, `import` refuses to run on
a bank whose cross-review is not marked done (override: --allow-unreviewed).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import banklib as bl  # noqa: E402

BASE = "http://127.0.0.1:23119"
# PowerShell's default "Mozilla/..." User-Agent makes Zotero drop the connection
# (it treats the caller as a browser); a plain UA plus the connector version header works.
HEADERS = {
    "User-Agent": "reference-search-skill/1.0",
    "Zotero-API-Version": "3",
    "X-Zotero-Connector-API-Version": "3",
    "Content-Type": "application/json",
}
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SESSION_CHUNK = 20


def call(path: str, body=None, raw: bytes | None = None, content_type: str | None = None, timeout: int = 180):
    headers = dict(HEADERS)
    data = None
    if raw is not None:
        data = raw
        headers["Content-Type"] = content_type or "application/octet-stream"
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            payload = resp.read()
            try:
                return resp.status, (json.loads(payload) if payload.strip() else None)
            except json.JSONDecodeError:
                return resp.status, payload.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _ping() -> bool:
    try:
        status, _ = call("/connector/ping", timeout=5)
        return status == 200
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return False


def find_zotero() -> Path | None:
    """The Zotero program: config.json's zotero_exe, else the default install location for this OS."""
    configured = bl.load_config().get("zotero_exe")
    if configured:
        return Path(configured) if Path(configured).exists() else None
    if sys.platform == "win32":
        roots = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        candidates = [Path(r) / "Zotero" / "zotero.exe" for r in roots if r]
        candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Zotero" / "zotero.exe")
    elif sys.platform == "darwin":
        candidates = [Path("/Applications/Zotero.app"), Path.home() / "Applications" / "Zotero.app"]
    else:
        found = shutil.which("zotero")
        candidates = [Path(found)] if found else []
        candidates += [Path("/opt/zotero/zotero"), Path("/usr/lib/zotero/zotero"), Path.home() / "Zotero_linux-x86_64" / "zotero"]
    return next((c for c in candidates if c.exists()), None)


def open_url(url: str) -> None:
    """Hand a zotero:// URL to the OS so the running Zotero handles it."""
    if hasattr(os, "startfile"):
        os.startfile(url)  # noqa: S606 - opens the zotero:// handler
    elif sys.platform == "darwin":
        subprocess.run(["open", url], check=False)
    elif shutil.which("xdg-open"):
        subprocess.run(["xdg-open", url], check=False)
    else:
        webbrowser.open(url)


def ensure_zotero(launch: bool) -> None:
    if _ping():
        return
    if not launch:
        bl.die("連不上 Zotero（127.0.0.1:23119）。請先開啟 Zotero。")
    exe = find_zotero()
    if exe is None:
        bl.die("連不上 Zotero，也找不到 Zotero 程式。請手動開啟 Zotero 後再試，或在 config.json 設定 zotero_exe。")
    print("Zotero 沒有在執行，正在啟動…", file=sys.stderr)
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-a", str(exe)])
    elif sys.platform == "win32":
        subprocess.Popen([str(exe)], close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        subprocess.Popen([str(exe)], close_fds=True, start_new_session=True)
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(3)
        if _ping():
            # Zotero sometimes restarts itself once right after first launch.
            time.sleep(6)
            if _ping():
                return
    bl.die("Zotero 啟動後 120 秒內仍連不上連接器。請確認 Zotero 視窗已開啟後再試。")


def api_list(path: str) -> list[dict]:
    out, start = [], 0
    sep = "&" if "?" in path else "?"
    while True:
        status, page = call(f"{path}{sep}format=json&limit=100&start={start}")
        if status != 200 or not isinstance(page, list):
            bl.die(
                f"Zotero 本機 API 讀取失敗（{path} → HTTP {status}）。請到 Zotero「設定 → 進階」勾選"
                "「允許此電腦上的其他應用程式與 Zotero 通訊」。"
            )
        out += page
        if len(page) < 100:
            return out
        start += 100


def item_ids(data: dict) -> tuple[str, str, str]:
    pmid = str(data.get("PMID") or "").strip()
    if not pmid:
        m = re.search(r"PMID:\s*(\d+)", data.get("extra") or "")
        pmid = m.group(1) if m else ""
    return bl.norm_doi(data.get("DOI") or ""), pmid, bl.norm_title(data.get("title") or "")


def match_items(ref: dict, items: list[dict]) -> list[dict]:
    doi, pmid, title = bl.norm_doi(ref.get("doi", "")), str(ref.get("pmid") or ""), bl.norm_title(ref.get("title", ""))
    hits = []
    for it in items:
        idoi, ipmid, ititle = item_ids(it["data"])
        if (doi and doi == idoi) or (pmid and pmid == ipmid):
            hits.append(it)
        elif title and title == ititle and not ((doi and idoi) or (pmid and ipmid)):
            hits.append(it)
    return hits


def top_collections() -> list[dict]:
    return [c for c in api_list("/api/users/0/collections") if not c["data"].get("parentCollection")]


def find_collection(name: str) -> dict | None:
    hits = [c for c in top_collections() if c["data"]["name"] == name]
    if len(hits) > 1:
        bl.die(f"文獻庫頂層有 {len(hits)} 個同名分類「{name}」，無法判斷要用哪一個。請先在 Zotero 改名。")
    return hits[0] if hits else None


def selected() -> dict:
    status, sel = call("/connector/getSelectedCollection", {})
    if status != 200 or not isinstance(sel, dict):
        bl.die(f"getSelectedCollection 失敗（HTTP {status}）")
    return sel


def tree_id(name: str) -> str | None:
    hits = [t["id"] for t in selected().get("targets", []) if t.get("name") == name and t.get("level") == 1 and t["id"].startswith("C")]
    return hits[0] if len(hits) == 1 else None


def select_library_root(items: list[dict]) -> None:
    """New collections are created under whatever is selected in the Zotero window, so move
    the selection to the library root first. zotero://select/library alone does not do it;
    selecting an item that is not in the current collection does."""
    sel = selected()
    if sel.get("id") is None:
        return
    current = {c["key"] for c in api_list("/api/users/0/collections") if c["data"]["name"] == sel.get("name")}
    cand = next((it["key"] for it in items if not (set(it["data"].get("collections") or []) & current)), None)
    if not cand:
        bl.die(f"Zotero 目前選在分類「{sel.get('name')}」，而且找不到不在這個分類裡的項目可用來切換。請在 Zotero 左欄點一下「我的文獻庫」後再試。")
    open_url(f"zotero://select/library/items/{cand}")
    deadline = time.time() + 25
    while time.time() < deadline:
        time.sleep(1.5)
        if selected().get("id") is None:
            return
    bl.die("沒辦法把 Zotero 的選取切回文獻庫根目錄。請在 Zotero 左欄點一下「我的文獻庫」後再試。")


def create_collection(name: str, items: list[dict]) -> None:
    select_library_root(items)
    rdf = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:z="http://www.zotero.org/namespaces/export#" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<z:Collection rdf:about="#c1"><dc:title>{xml_escape(name)}</dc:title></z:Collection></rdf:RDF>'
    )
    status, body = call("/connector/import", raw=rdf.encode("utf-8"), content_type="application/rdf+xml")
    if status != 201:
        bl.die(f"建立分類失敗（HTTP {status}）：{body}")


def write_select_shortcut(path: Path, item_keys: list[str]) -> tuple[Path, str]:
    """A .url shortcut that makes Zotero select the given items in the library root.

    The library root matters: pressing Delete inside a collection only removes the items from
    that collection and leaves them in the library, which is how 67 superseded items were left
    behind on 2026-10-05. From the library root the same action moves them to the trash."""
    url = f"zotero://select/library/items?itemKey={','.join(item_keys)}"
    path.write_text(f"[InternetShortcut]\nURL={url}\n", encoding="utf-8")
    return path, url


def how_to_open(shortcut: Path, url: str) -> str:
    """Windows opens the .url shortcut by double-click; elsewhere the zotero:// link goes to `open`/`xdg-open`."""
    if sys.platform == "win32":
        return f"開啟 {shortcut}"
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    return f'在終端機執行 {opener} "{url}"'


def zotero_item(bank: dict, ref: dict) -> dict:
    creators = []
    for a in ref.get("authors") or []:
        if a.get("literal"):
            creators.append({"lastName": a["literal"], "fieldMode": 1, "creatorType": "author"})
        else:
            creators.append({"firstName": a.get("given", ""), "lastName": a.get("family", ""), "creatorType": "author"})
    extra = []
    if ref.get("pmid"):
        extra.append(f"PMID: {ref['pmid']}")
    if ref.get("pmcid"):
        extra.append(f"PMCID: {ref['pmcid']}")
    if ref.get("arxiv"):
        extra.append(f"arXiv: {ref['arxiv']}")
    extra.append(f"Citation Key: {ref['citekey']}")
    url = f"https://doi.org/{bl.clean_doi(ref['doi'])}" if ref.get("doi") else ref.get("url", "")
    item = {
        "id": ref["citekey"],
        "itemType": ref.get("item_type") or "journalArticle",
        "title": ref.get("title", ""),
        "creators": creators,
        "abstractNote": ref.get("abstract", ""),
        "date": str(ref.get("year") or ""),
        "DOI": ref.get("doi", ""),
        "url": url,
        "volume": ref.get("volume", ""),
        "issue": ref.get("issue", ""),
        "pages": ref.get("pages", ""),
        "publicationTitle": ref.get("journal", ""),
        "journalAbbreviation": ref.get("journal_abbr", ""),
        "language": ref.get("language", ""),
        "extra": "\n".join(extra),
        "notes": [{"note": bl.note_html(bank, ref)}],
        "tags": [],
        "attachments": [],
    }
    return {k: v for k, v in item.items() if v not in ("", None)}


# ---------------------------------------------------------------- planning


def build_plan(bank: dict, on_duplicate: str, replace: set[str], replace_stale: bool) -> dict:
    items = api_list("/api/users/0/items/top")
    coll = find_collection(bank["collection"])
    ckey = coll["key"] if coll else None
    plan = {"items": items, "collection": coll, "new": [], "copy": [], "skip_elsewhere": [], "in_collection": [], "stale": [], "replace": []}
    for ref in bank["refs"]:
        hits = match_items(ref, items)
        inside = [it for it in hits if ckey and ckey in (it["data"].get("collections") or [])]
        if inside:
            recorded = ref.get("zotero") or {}
            is_stale = bool(recorded.get("noteHash")) and recorded["noteHash"] != bl.note_hash(ref)
            entry = {"ref": ref, "itemKeys": [it["key"] for it in inside]}
            if ref["citekey"] in replace or (replace_stale and is_stale):
                plan["replace"].append(entry)
            elif is_stale:
                plan["stale"].append(entry)
            else:
                plan["in_collection"].append(entry)
        elif hits:
            entry = {"ref": ref, "itemKeys": [it["key"] for it in hits]}
            plan["copy" if on_duplicate == "copy" else "skip_elsewhere"].append(entry)
        else:
            plan["new"].append({"ref": ref, "itemKeys": []})
    return plan


def print_plan(bank: dict, plan: dict) -> None:
    coll = plan["collection"]
    print(f"分類「{bank['collection']}」：{'已存在（' + coll['key'] + '，' + str(coll['meta'].get('numItems', '?')) + ' 筆）' if coll else '不存在，匯入時會建立'}")
    print(f"文獻庫頂層項目：{len(plan['items'])} 筆；bank：{len(bank['refs'])} 篇")
    print(f"  要新增：{len(plan['new'])}")
    print(f"  庫裡別處已有、會另存一筆進分類（之後請合併重複項目）：{len(plan['copy'])}")
    print(f"  庫裡別處已有、略過：{len(plan['skip_elsewhere'])}")
    print(f"  已在分類裡、不動：{len(plan['in_collection'])}")
    print(f"  已在分類裡、但附註內容已和 bank 不同（要更新請加 --replace-stale）：{len(plan['stale'])}")
    print(f"  要以修訂版取代：{len(plan['replace'])}")
    for label, key in (("別處已有", "copy"), ("別處已有（略過）", "skip_elsewhere"), ("附註過時", "stale")):
        for e in plan[key]:
            print(f"    {label}：{e['ref']['citekey']} → {', '.join(e['itemKeys'])}")
    print(f"交叉評審：{bl.review_phrase(bank)}")


# ---------------------------------------------------------------- commands


def cmd_check(args) -> None:
    bank = bl.load_bank(args.bank)
    ensure_zotero(launch=not args.no_launch)
    plan = build_plan(bank, args.on_duplicate, set(), False)
    sel = selected()
    print(f"Zotero 連線正常。目前選取：{sel.get('name') or sel.get('libraryName')}（{'文獻庫根目錄' if sel.get('id') is None else '分類'}）")
    print_plan(bank, plan)


def _log_path(args, bank: dict) -> Path:
    if args.log:
        return Path(args.log)
    return Path(args.bank).resolve().parent / f"{bank['date']}-{bank['slug']}-zotero-import-log.json"


def _load_log(path: Path, bank: dict) -> dict:
    if path.is_file():
        log = json.loads(path.read_text(encoding="utf-8"))
        if "runs" in log:
            return log
        # A hand-written log from before this script existed: keep it, never overwrite it.
        return {"collection": {"name": bank["collection"]}, "legacy": log, "runs": []}
    return {"collection": {"name": bank["collection"]}, "runs": []}


def _save_log(path: Path, log: dict) -> None:
    path.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_import(args) -> None:
    bank = bl.load_bank(args.bank)
    today = dt.date.today().isoformat()
    if not bl.review_done(bank) and not args.allow_unreviewed and not args.dry_run:
        bl.die(
            "這份 bank 還沒有標記交叉評審完成。匯入後的附註無法修改或刪除，所以預設要先評審、修訂，再匯入。"
            "評審做完請跑 refbank.py mark-reviewed；使用者明確要求先匯入時才加 --allow-unreviewed。"
        )
    unclassified = [r["citekey"] for r in bank["refs"] if not r.get("category") or not r.get("tier")]
    if unclassified:
        bl.die(f"{len(unclassified)} 篇還沒有類別或層級，先補上或 drop：{', '.join(unclassified[:10])}")

    ensure_zotero(launch=not args.no_launch)
    replace = {k for k in re.split(r"[\s,]+", args.replace or "") if k}
    plan = build_plan(bank, args.on_duplicate, replace, args.replace_stale)
    print_plan(bank, plan)

    tag = bank["tag"]
    jobs = []  # (ref, extra tags, superseded item keys)
    jobs += [(e["ref"], (), []) for e in plan["new"]]
    jobs += [(e["ref"], (f"{tag}/重複-待合併",), []) for e in plan["copy"]]
    jobs += [(e["ref"], (f"{tag}/修訂版-{today}",), e["itemKeys"]) for e in plan["replace"]]
    batch = (args.batch_tag,) if args.batch_tag else ()

    groups: dict[tuple[str, ...], list[dict]] = {}
    for ref, extra, _old in jobs:
        groups.setdefault(tuple(bl.ref_tags(bank, ref, extra + batch)), []).append(ref)
    sessions = []
    for tags, refs in groups.items():
        for i in range(0, len(refs), SESSION_CHUNK):
            sessions.append({"sessionID": f"{bank['slug']}-{uuid.uuid4().hex[:12]}", "tags": list(tags), "refs": refs[i : i + SESSION_CHUNK]})

    if args.dry_run:
        out = Path(args.dry_run)
        payload = [{"sessionID": s["sessionID"], "tags": s["tags"], "items": [zotero_item(bank, r) for r in s["refs"]]} for s in sessions]
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n[dry-run] 沒有寫入 Zotero。{len(jobs)} 筆、{len(sessions)} 個工作階段的內容已寫到 {out}")
        return
    if not jobs:
        print("\n沒有要匯入的項目。")
        return

    # --- collection
    if plan["collection"] is None:
        create_collection(bank["collection"], plan["items"])
        time.sleep(1)
        plan["collection"] = find_collection(bank["collection"])
        if plan["collection"] is None:
            bl.die("分類建立後讀不回來，已停止（還沒有匯入任何文獻）。")
        print(f"已建立分類「{bank['collection']}」（{plan['collection']['key']}）")
    ckey = plan["collection"]["key"]
    target = tree_id(bank["collection"])
    if not target:
        bl.die(f"在 Zotero 的儲存目標清單裡找不到頂層分類「{bank['collection']}」，已停止（還沒有匯入任何文獻）。")

    log_path = _log_path(args, bank)
    log = _load_log(log_path, bank)
    log["collection"] = {"name": bank["collection"], "key": ckey, "treeViewID": target}
    run = {"date": today, "started": dt.datetime.now().isoformat(timespec="seconds"), "review": bank.get("review"), "sessions": [], "superseded": [], "duplicates_elsewhere": [], "skipped": {}}
    log["runs"].append(run)
    run["duplicates_elsewhere"] = [{"citekey": e["ref"]["citekey"], "existingItemKeys": e["itemKeys"], "action": "copied"} for e in plan["copy"]]
    run["duplicates_elsewhere"] += [{"citekey": e["ref"]["citekey"], "existingItemKeys": e["itemKeys"], "action": "skipped"} for e in plan["skip_elsewhere"]]
    run["skipped"] = {"already_in_collection": [e["ref"]["citekey"] for e in plan["in_collection"]], "stale_not_replaced": [e["ref"]["citekey"] for e in plan["stale"]]}
    run["superseded"] = [{"citekey": e["ref"]["citekey"], "oldItemKeys": e["itemKeys"]} for e in plan["replace"]]
    _save_log(log_path, log)

    # --- save
    before = {it["key"] for it in plan["items"]}
    for s in sessions:
        items = [zotero_item(bank, r) for r in s["refs"]]
        entry = {"sessionID": s["sessionID"], "tags": s["tags"], "citekeys": [r["citekey"] for r in s["refs"]]}
        run["sessions"].append(entry)
        status, body = call("/connector/saveItems", {"sessionID": s["sessionID"], "uri": items[0].get("url") or "https://pubmed.ncbi.nlm.nih.gov/", "items": items})
        entry["saveItems"] = status
        _save_log(log_path, log)
        if status != 201:
            bl.die(f"saveItems 失敗（HTTP {status}）：{body}\n已完成的工作階段記在 {log_path}。修正後重跑即可，已匯入的會被判定為「已在分類裡」而跳過。")
        status, body = call("/connector/updateSession", {"sessionID": s["sessionID"], "target": target, "tags": s["tags"]})
        entry["updateSession"] = status
        _save_log(log_path, log)
        if status != 200:
            bl.die(f"updateSession 失敗（HTTP {status}）：{body}\n這一批已存進 Zotero，但可能還在原本選取的位置、沒有標籤。紀錄在 {log_path}。")
        print(f"  已存 {len(items)} 筆 → {bank['collection']}（{', '.join(s['tags'][1:]) or s['tags'][0]}）")

    # --- read back
    time.sleep(2)
    inside = api_list(f"/api/users/0/collections/{ckey}/items/top")
    problems = []
    job_extra = {ref["citekey"]: extra for ref, extra, _old in jobs}
    for ref, _extra, _old in jobs:
        fresh = [it for it in match_items(ref, inside) if it["key"] not in before]
        if len(fresh) != 1:
            problems.append(f"{ref['citekey']}: 讀回 {len(fresh)} 筆新項目（預期 1）")
            continue
        it = fresh[0]
        have = {t["tag"] for t in it["data"].get("tags") or []}
        want = set(bl.ref_tags(bank, ref, job_extra[ref["citekey"]] + batch))
        if not want <= have:
            problems.append(f"{ref['citekey']}: 缺標籤 {sorted(want - have)}")
        if not it.get("meta", {}).get("numChildren"):
            problems.append(f"{ref['citekey']}: 沒有附註")
        ref["zotero"] = {"itemKey": it["key"], "collectionKey": ckey, "imported": today, "noteHash": bl.note_hash(ref)}
    for e in plan["in_collection"]:
        e["ref"].setdefault("zotero", {"itemKey": e["itemKeys"][0], "collectionKey": ckey})
    bl.save_bank(bank, args.bank)

    run["verification"] = {"imported": len(jobs), "collection_total": len(inside), "problems": problems, "verified": not problems}
    run["finished"] = dt.datetime.now().isoformat(timespec="seconds")

    old_keys = [k for e in plan["replace"] for k in e["itemKeys"]]
    if old_keys:
        shortcut, url = write_select_shortcut(Path(args.bank).resolve().parent / f"刪除舊版-{today}.url", old_keys)
        run["delete_old_url"] = url
        run["delete_old_shortcut"] = str(shortcut)
    _save_log(log_path, log)

    print(f"\n匯入 {len(jobs)} 筆；分類現有 {len(inside)} 筆。紀錄：{log_path}")
    if problems:
        print(f"讀回核對有 {len(problems)} 個問題：")
        for p in problems:
            print("  ✗ " + p)
    else:
        print("讀回核對通過：每筆都在分類裡、有標籤、有引用句附註。")
    if plan["copy"]:
        print(f"{len(plan['copy'])} 筆在文獻庫別處原本就有，這次另存了一筆（標籤 {tag}/重複-待合併）。請在 Zotero「重複項目」合併。")
    if old_keys:
        print(
            f"{len(old_keys)} 筆舊版要由使用者移到垃圾桶：{how_to_open(shortcut, url)}，Zotero 會在「我的文獻庫」選取它們；"
            f"確認右側顯示選取了 {len(old_keys)} 個項目後，按右鍵選「將項目移到垃圾桶」。刪完後跑 verify。"
        )
    if problems:
        sys.exit(1)


def cmd_verify(args) -> None:
    bank = bl.load_bank(args.bank)
    ensure_zotero(launch=not args.no_launch)
    coll = find_collection(bank["collection"])
    if coll is None:
        bl.die(f"Zotero 裡沒有頂層分類「{bank['collection']}」")
    inside = api_list(f"/api/users/0/collections/{coll['key']}/items/top")
    keys = {it["key"] for it in inside}
    problems = []
    for ref in bank["refs"]:
        hits = match_items(ref, inside)
        if not hits:
            problems.append(f"{ref['citekey']}: 不在分類裡")
        elif len(hits) > 1:
            problems.append(f"{ref['citekey']}: 分類裡有 {len(hits)} 筆（{', '.join(h['key'] for h in hits)}）")
        elif not hits[0].get("meta", {}).get("numChildren"):
            problems.append(f"{ref['citekey']}: 沒有附註")
    # Trashed items are not listed by /items/top, so "still listed" means "not trashed".
    library = api_list("/api/users/0/items/top")
    tag = bank["tag"]
    # Items that carry this bank's tag but sit outside the collection are almost always
    # superseded copies that were removed from the collection instead of trashed.
    orphans = [
        it["key"]
        for it in library
        if it["key"] not in keys and any(t["tag"] == tag or t["tag"].startswith(tag + "/") for t in it["data"].get("tags") or [])
    ]
    log_path = _log_path(args, bank)
    if log_path.is_file():
        for run in _load_log(log_path, bank)["runs"]:
            for s in run.get("superseded", []):
                still = [k for k in s.get("oldItemKeys", []) if k in keys]
                if still:
                    problems.append(f"{s['citekey']}: 舊版還在分類裡（{', '.join(still)}），尚未刪除")
    extra = [it for it in inside if not any(it in match_items(r, [it]) for r in bank["refs"])]
    print(f"分類「{bank['collection']}」（{coll['key']}）：{len(inside)} 筆；bank：{len(bank['refs'])} 篇。")
    if extra:
        print(f"分類裡有 {len(extra)} 筆不在 bank 裡（可能是使用者自己加的，沒有動）：{', '.join(it['key'] for it in extra[:20])}")
    if orphans:
        shortcut, url = write_select_shortcut(Path(args.bank).resolve().parent / f"選取分類外的{tag}項目.url", orphans)
        problems.append(
            f"文獻庫裡有 {len(orphans)} 筆帶「{tag}」標籤但不在分類裡、也沒進垃圾桶（多半是只被移出分類的舊版）。"
            f"{how_to_open(shortcut, url)} 可在 Zotero 選取它們，由使用者決定是否移到垃圾桶"
        )
    if problems:
        print(f"{len(problems)} 個問題：")
        for p in problems:
            print("  ✗ " + p)
        sys.exit(1)
    print("核對通過：bank 的每一篇都在分類裡，各一筆，都有附註。")


def main() -> None:
    bl.utf8_streams()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--bank", required=True)
        p.add_argument("--no-launch", action="store_true", help="Zotero 沒開時不要自動啟動")
        p.add_argument("--log", help="匯入紀錄路徑（預設在 bank 同資料夾）")

    p = sub.add_parser("check", help="唯讀：連線、分類、查重")
    common(p)
    p.add_argument("--on-duplicate", choices=["copy", "skip"], default="copy")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("import", help="匯入並讀回核對")
    common(p)
    p.add_argument("--on-duplicate", choices=["copy", "skip"], default="copy", help="文獻庫別處已有同一篇時：copy＝另存一筆進分類並標「重複-待合併」（預設）；skip＝略過")
    p.add_argument("--batch-tag", help="這一批額外加的標籤，例如 'EVS/補入-2026-10-05'")
    p.add_argument("--replace", help="這些引用鍵已在分類裡，但要以修訂版取代（逗號分隔）")
    p.add_argument("--replace-stale", action="store_true", help="所有附註內容已和 bank 不同的項目都以修訂版取代")
    p.add_argument("--allow-unreviewed", action="store_true", help="尚未交叉評審也匯入（附註之後改不了）")
    p.add_argument("--dry-run", metavar="FILE", help="不寫入 Zotero，把要送出的內容寫到 FILE")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("verify", help="唯讀：分類內容對照 bank")
    common(p)
    p.set_defaults(fn=cmd_verify)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
