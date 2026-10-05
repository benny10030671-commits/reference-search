# reference-search

[Claude Code](https://docs.claude.com/en/docs/claude-code) 的 skill。給一個研究或寫作題目，交付一份寫稿時可以直接取用的
**引用庫**，並匯入 Zotero：

```
/reference-search 敗血性休克早期使用升壓劑的預後
```

每篇文獻附上英文逐字引用句、這句話能支持的論點（中文）、適合放在稿件哪一段。引用句由程式逐字比對原文，
整份經 Codex 交叉評審後，匯入 Zotero 的具名分類，每筆帶引用句附註與標籤。

> 產出與操作說明都是**繁體中文**（臺灣用語）。English summary at the bottom.

## 流程

| 步驟 | 做什麼 | 用什麼 |
|---|---|---|
| 1 | 檢索與篩選文獻 | `paper-navigator` skill 的檢索方法；臨床題目用內建的 PubMed＋Europe PMC 檢索 |
| 2 | 抓書目與摘要，建立引用庫 `bank.json` | `scripts/refbank.py add` |
| 3 | 逐篇標註：引用句、論點、稿件段落、類別、層級 | Claude 分批閱讀摘要後寫入 |
| 4 | 程式逐字比對每一句引用句，匯出報告 | `scripts/refbank.py verify`、`export` |
| 5 | 交叉評審，逐條裁定後修訂 | `cross-review` skill（Codex CLI） |
| 6 | 匯入 Zotero 並讀回核對 | `scripts/zotero_import.py` |
| 7 | 存檔、寫 README，可選擇同步到 Obsidian vault | 依 `SKILL.md` |

交叉評審排在匯入之前：Zotero 的本機介面只能新增，不能修改或刪除已匯入的項目，附註要在匯入前定稿。

## 安裝

### 先準備

| 需要 | 用途 | 沒有的話 |
|---|---|---|
| [Claude Code](https://docs.claude.com/en/docs/claude-code) | 執行這個 skill | 無法使用 |
| Git（Windows 請裝 [Git for Windows](https://git-scm.com/download/win)，附 Git Bash） | 安裝；交叉評審的腳本在 bash 裡跑 | 無法安裝 |
| [Python](https://www.python.org/downloads/) 3.9 以上 | 所有腳本（只用標準函式庫） | 無法使用 |
| [Zotero](https://www.zotero.org/download/) 7 以上 | 最後的匯入 | 仍會產出報告、CSV、BibTeX、RIS，只是不匯入 |
| [Codex CLI](https://github.com/openai/codex)（`npm install -g @openai/codex`，再 `codex login`） | 交叉評審 | 停在評審那一步回報；要不經評審就匯入得由你明確同意 |

Zotero 要多設定一項：**設定 → 進階 → 勾選「允許此電腦上的其他應用程式與 Zotero 通訊」**（查重與讀回核對要用）。

### 一行安裝

Windows（PowerShell）：

```powershell
irm https://raw.githubusercontent.com/benny10030671-commits/reference-search/main/install.ps1 | iex
```

macOS／Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/benny10030671-commits/reference-search/main/install.sh | bash
```

安裝程式會做這些事，然後執行環境檢查：

1. 把這個 repo 裝到 `~/.claude/skills/reference-search`（已裝過就 `git pull` 更新）。
2. 沒有的話，從原作者的 repo 安裝兩個相依的 skill：
   [`cross-review`](https://github.com/quanru/cross-review) 與
   [`paper-navigator`](https://github.com/EvoScientist/EvoSkills/tree/main/skills/paper-navigator)；
   並安裝 paper-navigator 需要的 Python 套件 `httpx`。
3. 沒有 `config.json` 就從 `config.example.json` 複製一份（已有的不會覆蓋）。

裝完**重新啟動 Claude Code**，skill 清單就會出現 `reference-search`。

### 手動安裝

```bash
git clone https://github.com/benny10030671-commits/reference-search ~/.claude/skills/reference-search
git clone https://github.com/quanru/cross-review ~/.claude/skills/cross-review
# paper-navigator：把 EvoScientist/EvoSkills 的 skills/paper-navigator 資料夾複製到 ~/.claude/skills/paper-navigator
cp ~/.claude/skills/reference-search/config.example.json ~/.claude/skills/reference-search/config.json
python ~/.claude/skills/reference-search/scripts/doctor.py
```

### 檢查環境

```bash
python ~/.claude/skills/reference-search/scripts/doctor.py           # macOS／Linux 用 python3
python ~/.claude/skills/reference-search/scripts/doctor.py --online  # 另外測試 PubMed 連線
```

逐項列出 Python、設定、相依 skill、bash、Codex 登入狀態、Zotero 連線與本機 API，缺什麼會附上修正方式。
第一次在 Claude Code 裡使用時，skill 也會自己跑一次。

## 設定

`~/.claude/skills/reference-search/config.json`（不在 git 版控內，更新不會覆蓋）：

| 欄位 | 預設 | 說明 |
|---|---|---|
| `output_root` | `~/Documents/reference-search` | 產出放這裡，每個題目一個子資料夾 |
| `obsidian_vault` | 空（不同步） | 填 vault 路徑就會把每個題目資料夾複製一份進去 |
| `obsidian_subdir` | `reference-search` | 放在 vault 裡的哪個資料夾 |
| `obsidian_moc` | 空 | 選填：一則索引筆記（相對於 vault），每個題目在裡面加一列 |
| `zotero_exe` | 空（自動找） | Zotero 不在預設安裝位置時才要填 |
| `cross_review_script` | 空（自動找） | cross-review 不在 `~/.claude/skills/` 時才要填 `run-cross-review.sh` 的路徑 |

路徑可以用 `~`。`python scripts/refbank.py config` 會印出實際使用的值。

## 使用

在 Claude Code 裡：

```
/reference-search <題目>
```

或直接說「幫我找這個題目的參考文獻並匯入 Zotero」「建引用庫」「這個題目可以引用哪些文獻、引哪幾句」。
同一個題目再呼叫一次，會接著既有的引用庫做（補文獻、評審後修訂附註）。

一個題目大約 100 篇上下，整個流程（含約 10 到 15 分鐘的 Codex 評審）會跑一段時間，中途不需要你回答問題。

## 產出

存在 `<output_root>/<題目資料夾>/`：

| 檔案 | 內容 |
|---|---|
| `<日期>-<slug>-bank.json` | 引用庫本體。其他檔案都由它產生，續作也靠它 |
| `<日期>-<slug>-citation-bank.md` | 主報告：證據總結、依稿件段落排列的引用句、完整引用庫、參考文獻 |
| `<日期>-<slug>-citation-bank.csv` | 一列一句引用句 |
| `<日期>-<slug>.bib`、`.ris` | 備份與他用（已匯入 Zotero，不要再匯一次） |
| `<日期>-<slug>-search-log.md` | 每次查詢與 PubMed 改寫後的檢索式（Methods 用） |
| `<日期>-<slug>-cross-review.md` | Codex 的意見與逐條裁定 |
| `<日期>-<slug>-zotero-import-log.json` | 匯入紀錄與讀回核對結果 |
| `README.md` | 這個題目的索引、結論、沒有涵蓋的範圍 |

Zotero 裡：一個以英文題目命名的頂層分類；每筆有標籤 `<TAG>`、`<TAG>/<類別>`、`<TAG>/<層級>`，以及一則引用句附註。
你原本的分類與項目不會被動到。

## 腳本

平常由 skill 呼叫，也可以單獨使用。每個子指令都有 `--help`。

```bash
RS="$HOME/.claude/skills/reference-search/scripts"

python "$RS/pubmed_search.py" -q "septic shock norepinephrine timing" --pub-type rct,sr,ma --source both
python "$RS/pubmed_search.py" -q "…" --print-query        # 只看 PubMed 改寫後的檢索式與總筆數

python "$RS/refbank.py" init     --out <bank.json> --topic "…" --folder <Folder> --slug <slug> --tag <TAG>
python "$RS/refbank.py" add      --bank <bank.json> --pmids "12345678, 23456789"
python "$RS/refbank.py" show     --bank <bank.json> --pending --limit 10
python "$RS/refbank.py" annotate --bank <bank.json> --from ann-01.json
python "$RS/refbank.py" verify   --bank <bank.json>
python "$RS/refbank.py" export   --bank <bank.json> --narrative narrative.md
python "$RS/refbank.py" packet   --bank <bank.json> --out-dir xr --narrative narrative.md
python "$RS/refbank.py" config

python "$RS/zotero_import.py" check  --bank <bank.json>     # 唯讀：連線、分類、查重
python "$RS/zotero_import.py" import --bank <bank.json>     # 加 --dry-run <檔> 可以只看要送出的內容
python "$RS/zotero_import.py" verify --bank <bank.json>     # 唯讀：分類內容對照引用庫
```

`NCBI_API_KEY`（選填）可把 PubMed 的速率上限從每秒 3 次提高到 10 次；`NCBI_EMAIL`（選填）會依 NCBI 建議附在請求裡。

## 檔案

| 路徑 | 內容 |
|---|---|
| `SKILL.md` | 完整流程與規則（Claude 讀的） |
| `scripts/pubmed_search.py` | PubMed＋Europe PMC 檢索，研究設計用 `[pt]` 篩選，記錄改寫後的檢索式 |
| `scripts/refbank.py` | 引用庫：抓書目、標註、逐字比對、匯出、送審封包 |
| `scripts/zotero_import.py` | Zotero 匯入：查重、建分類、存入、讀回核對 |
| `scripts/doctor.py` | 環境檢查 |
| `scripts/banklib.py` | 共用函式與設定讀取 |
| `references/` | bank 格式、交叉評審、Zotero、交付檔案的細節（Claude 需要時才讀） |
| `install.ps1`、`install.sh` | 安裝程式 |
| `config.example.json` | 設定範本 |

## 限制

- 這是寫稿用的引用來源整理，不是系統性回顧的正式檢索。臨床題目只查 PubMed 與 Europe PMC，沒有 Embase、
  Cochrane 與中文資料庫（CNKI、萬方、維普、華藝），不能據此說「沒有相關研究」。
- 逐字比對只保證引用句沒抄錯。中文論點是 AI 的解讀，經過一輪 AI 交叉評審，沒有人工核對。投稿前請對照原文。
- 預設以摘要為準；只有取得的開放取用全文會做全文引用句。有版權的全文只記摘述與位置，不抄原句。
- 文獻庫別處已有的同一篇文獻，無法直接加進新分類，只能另存一筆，再到 Zotero「重複項目」合併。
- 測試環境：Windows 11、Python 3.12、Zotero 9、Codex CLI 0.154。macOS／Linux 的路徑偵測有寫，但沒有實機測過；
  遇到問題請開 issue 並附上 `doctor.py` 的輸出。

## 更新與移除

- 更新：再跑一次安裝指令，或 `git -C ~/.claude/skills/reference-search pull`。`config.json` 不會被覆蓋。
- 移除：刪掉 `~/.claude/skills/reference-search`。已產出的檔案與 Zotero 裡的項目不受影響。
  cross-review 與 paper-navigator 若別的地方沒用到，也可以一併刪除。

## 第三方元件

安裝程式會從原作者的 repo 下載下列 skill，本 repo 不包含它們的程式碼：

- [cross-review](https://github.com/quanru/cross-review)（quanru）
- [paper-navigator](https://github.com/EvoScientist/EvoSkills/tree/main/skills/paper-navigator)（EvoScientist，Apache-2.0）

各自的授權以原 repo 為準。

## English summary

A Claude Code skill that turns a research topic into a manuscript-ready **citation bank**: it searches the literature
(PubMed + Europe PMC for clinical topics, via a bundled stdlib-only script; `paper-navigator` for everything else),
extracts verbatim quotable sentences from each abstract (machine-checked against the source), maps each sentence to a
claim and a manuscript section, has the whole bank cross-reviewed by Codex CLI, and finally imports it into a named
Zotero collection with a quote note and tags on every item. All prompts and outputs are in Traditional Chinese.

Install with `install.ps1` (Windows) or `install.sh` (macOS/Linux) as shown above, restart Claude Code, then run
`/reference-search <topic>`. Requirements: Git, Python 3.9+, Zotero 7+ with "Allow other applications on this computer
to communicate with Zotero" enabled, and Codex CLI for the review step. Run `scripts/doctor.py` to see what is missing.

## 授權

[MIT](LICENSE)
