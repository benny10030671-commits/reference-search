# bank.json 與標註檔的格式

`bank.json` 是引用庫的單一資料來源。主報告、CSV、BibTeX、RIS、送審封包、Zotero 項目與附註都由它產生，
所以數字與措辭不會在不同檔案之間走樣。檔名：`<YYYY-MM-DD>-<slug>-bank.json`，放在主題資料夾。

## bank.json

以下範例的題目、文獻與引用句都是格式示意，不是真實資料。

```json
{
  "topic": "敗血性休克早期使用升壓劑的預後",
  "topic_en": "Early Vasopressor Initiation in Septic Shock",
  "folder": "Early-Vasopressor-Sepsis",
  "slug": "early-vasopressor-sepsis",
  "tag": "EVS",
  "collection": "Early Vasopressor Initiation in Septic Shock",
  "date": "2026-10-04",
  "sections": [
    {"name": "前言：指引建議與證據缺口", "hint": "帶出研究動機；Introduction 最後一段的主要素材。"},
    "方法：「早期」的定義與時間零點",
    "討論：早期升壓劑與死亡率",
    "限制：觀察性設計與適應症干擾"
  ],
  "categories": {
    "A": "早期升壓劑的直接證據",
    "B": "指引與臨床實務調查"
  },
  "review": {"status": "none"},
  "refs": [ … ]
}
```

- `date` 是建庫日期，決定所有匯出檔的檔名前綴；續作時不要改。
- `sections` 的順序就是主報告裡的順序。項目可以是字串，或帶 `hint` 的物件（hint 會印在該段落表格上方）。
- `categories` 的 key 順序決定文獻編號 `[N]` 的順序（先類別、再層級、再加入順序）。
- `review.status`：`none`／`done`／`failed`。由 `refbank.py mark-reviewed` 寫入；`zotero_import.py import` 只接受 `done`。

## refs 裡的一篇

書目欄位由 `refbank.py add` 填入，不要手改：
`citekey`、`pmid`、`pmcid`、`doi`、`title`、`authors`（`{"family","given"}` 或團體作者 `{"literal"}`）、`journal`、
`journal_abbr`、`year`、`volume`、`issue`、`pages`、`language`、`url`、`pub_types`、`abstract`、`item_type`、`metadata_source`。

標註欄位由你透過 `annotate` 填：

| 欄位 | 說明 |
|---|---|
| `category` | `categories` 的 key |
| `tier` | `核心`／`重要佐證`／`補充` |
| `doc_type` | `add` 依 PubMed 出版類型猜的（原始研究、統合分析、系統性回顧、隨機對照試驗、指引、綜述、病例報告、評論、計畫書、觀察性研究）；不對就改 |
| `note` | 引用這篇前該知道的事：族群與年代、可能重疊的世代、摘要與全文不一致處、主要限制 |
| `flags` | 自訂標記，會變成 Zotero 標籤 `<TAG>/<flag>`。`無引用句-需全文` 與 `二手轉述-待核對原文` 會自動加，不必手寫 |
| `fulltext` | 已存檔全文的路徑，相對於主題資料夾，例如 `papers/Smith2023-PMC0000000.md` |
| `fulltext_read` | 沒存檔但讀過時的說明，例如 `線上已讀`（顯示在主報告「全文」欄） |
| `quotes` | 見下 |

`zotero`（`itemKey`、`collectionKey`、`imported`、`noteHash`）由 `zotero_import.py` 寫入。

## quotes 裡的一列

```json
{"section": "討論：早期升壓劑與死亡率",
 "claim": "此世代中，休克發生 6 小時內開始 norepinephrine 者的 28 天死亡率較低（觀察性關聯）",
 "quote": "Initiation of norepinephrine within 6 hours of shock onset was associated with lower 28-day mortality.",
 "source": "abstract"}
```

| `source` | 意義 | 檢查方式 |
|---|---|---|
| `abstract` | 取自摘要 | `quote` 必須是 `abstract` 欄的逐字子字串 |
| `fulltext` | 取自已存檔的開放取用全文 | `quote` 必須是 `fulltext` 檔案內容的逐字子字串 |
| `paraphrase` | 讀了有版權的全文，只記摘述 | 不帶 `quote`；必須有 `locator`（如 `全文 Results「Mortality」段、Table 4`） |

選填：`locator`（全文引用句的位置）、`secondhand: true`（這句是該文對別的文件的轉述；主報告與附註會標示，文獻會加上
`二手轉述-待核對原文` 標籤）。

逐字比對只把連續空白視為一個空格，其餘字元必須完全相同，包含引號與破折號的樣式。摘要是分段的
（`BACKGROUND: …`、`RESULTS: …`），一句引用句不能跨段；需要兩段的內容就拆成兩列。

## 標註檔（給 `refbank.py annotate --from`）

一個 JSON 陣列，每個元素對應一篇，用 `citekey`（或 `pmid`）指定。只有出現的欄位會被覆寫：

```json
[
  {"citekey": "Smith2021", "category": "A", "tier": "核心",
   "note": "單中心回溯性世代，2012–2018 年；時間零點定義為乳酸 > 4 mmol/L。",
   "quotes": [ {…}, {…} ]},
  {"citekey": "Lee2022", "add_quotes": [ {…} ]},
  {"citekey": "SSC2021", "new": true, "title": "…", "authors": [{"literal": "Surviving Sepsis Campaign"}], "year": 2021,
   "url": "https://…", "item_type": "report", "category": "B", "tier": "重要佐證", "quotes": []}
]
```

- `quotes` 取代該篇全部引用句；`add_quotes` 附加在後面。
- `"new": true` 只用在 PubMed、Europe PMC、Crossref 都查不到的文件。書目只能來自你實際開啟讀到的頁面。
- 段落名稱必須已在 `bank.sections`；確實需要新段落時加 `--add-sections`。
- 評審後修訂某一篇：`refbank.py show --bank <bank> --citekeys X --no-abstract` 印出現有標註，改好後重送。

## 指令速查

```
refbank.py init | add | show | annotate | drop | verify | export | packet | mark-reviewed | stats | config
zotero_import.py check | import | verify
pubmed_search.py -q "..." [--pub-type ...] [--source both] [--print-query] [--output pool.jsonl --append]
doctor.py [--online]
```

每個子指令都有 `--help`。
