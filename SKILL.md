---
name: reference-search
description: "輸入一個研究或寫作題目，一次做完「檢索文獻 → 挑出可採用的 reference → 逐篇標出可引用的句子與適合放的稿件段落 → Codex 交叉評審校閱 → 匯入 Zotero 具名分類（每筆附引用句附註與標籤）」。檢索以 paper-navigator 為基底，校閱用 cross-review。使用時機：使用者輸入 /reference-search，或說「幫我找這個題目的 reference／參考文獻並匯入 Zotero」「建引用庫」「這個題目可以引用哪些文獻、引哪幾句」「把文獻加註後放進 Zotero」「寫稿要用的文獻幫我整理進 Zotero」「reference search」「citation bank」；也用於既有主題的續作（補文獻、評審後修訂附註）。只想找文獻或讀文獻、不需要引用句與 Zotero 時用 paper-navigator；單篇評讀用 paper-review；系統性回顧的正式檢索不要用這個（改用 meta-pipe 這類多資料庫、雙人篩選的流程）。"
---

# Reference Search：從題目到 Zotero 裡可以直接引用的文獻庫

使用者給一個題目，這個 skill 交付一個**寫稿時可以直接取用的引用庫**：每篇文獻附上英文逐字引用句、
這句話能支持的論點（中文）、適合放在稿件哪一段；整份經 Codex 交叉評審後，匯入 Zotero 的具名分類。

三個既有元件各管一段，這個 skill 只負責把它們串起來，並補上中間的「引用庫」：

| 階段 | 用什麼 |
|---|---|
| 檢索與篩選 | `paper-navigator` skill 的檢索方法；臨床題目用本 skill 內建的 `scripts/pubmed_search.py`（PubMed＋Europe PMC） |
| 引用庫（書目、引用句、匯出、送審封包） | 本 skill 的 `scripts/refbank.py`，單一資料來源是 `bank.json` |
| 校閱 | `cross-review` skill（Codex CLI） |
| 匯入 Zotero | 本 skill 的 `scripts/zotero_import.py` |

腳本在本 skill 資料夾的 `scripts/`，通常是 `$HOME/.claude/skills/reference-search/scripts/`；以載入 skill 時顯示的
base directory 為準。下文簡寫為 `$RS`。用 Bash 工具執行 `python "$RS/refbank.py" …`；macOS／Linux 沒有 `python`
指令時改用 `python3`。腳本只用 Python 標準函式庫。

## 流程與順序

```
0 起手 → 1 檢索 → 2 建 bank、抓書目 → 3 標註可引用的部分 → 4 驗證、總結、匯出
       → 5 交叉評審 → 6 裁定與修訂 → 7 匯入 Zotero → 8 存檔與同步 → 9 回報
```

**交叉評審排在匯入 Zotero 之前，這是刻意的。** Zotero 的本機介面只能新增，不能修改或刪除已匯入的項目。
曾經先匯入、隔天才評審，結果 123 筆裡有 63 筆附註要換，只能另匯修訂版再請使用者手動刪舊的。
所以先評審、先修訂，匯入一次到位。`zotero_import.py import` 會拒絕尚未標記評審完成的 bank；
只有使用者明講「先匯入再說」時才加 `--allow-unreviewed`。

整個流程不需要中途停下來問使用者。命名、分類、段落都先自己決定，在第一則進度訊息講明，使用者要改再改。
只有題目本身含糊到無法檢索（例如只有一個縮寫）才先問。

## 0. 起手

1. **讀設定。** `python "$RS/refbank.py" config` 印出輸出位置（`output_root`）與 Obsidian 同步設定，下文的
   `<output_root>` 指這個值。使用者的 `CLAUDE.md` 對輸出位置另有規定時，以 `CLAUDE.md` 為準。
   印出「沒有 config.json」時是第一次使用：先跑 `python "$RS/doctor.py"`，把缺少的元件（cross-review、Codex CLI、
   Zotero 設定）一次告訴使用者，然後照預設值繼續；缺的元件到用得到的那一步才會擋住。
2. **先看是不是續作。** 在 `<output_root>` 找同主題資料夾。有 `*-bank.json` 就沿用：
   讀 README、跑 `refbank.py stats`，直接接著做（見文末「續作與修訂」）。
   資料夾在但沒有 bank.json（本 skill 之前手動整理的主題）：新增的文獻另建 bank、沿用同一個 Zotero 分類名稱，
   腳本會把已在分類裡的文獻判為既有而跳過。
3. **定名**（寫進 bank）：資料夾名（英文短名，如 `Early-Vasopressor-Sepsis`）、slug（小寫，如 `early-vasopressor-sepsis`）、
   Zotero 標籤前綴（短，如 `EVS`）、Zotero 分類名稱（英文題目，使用者有指定就用指定的）。
4. **推定稿件類型與段落骨架。** 引用句要掛在「稿件段落」上才有用，所以先想這篇稿子大概長什麼樣
   （原著、回溯性世代、SR/MA、病例報告、綜述），列出 8 到 14 個段落，寫成「大段：主題」，例如
   `前言：指引歧異與證據缺口`、`方法：血糖異常的定義與評估`、`討論：糖尿病前期捐贈者的預後`、`限制：追蹤時間與研究設計`。
   段落可以在檢索後調整。
5. 建 bank：

```bash
python "$RS/refbank.py" init --out "<output_root>/<Folder>/<YYYY-MM-DD>-<slug>-bank.json" \
  --topic "<中文題目>" --topic-en "<English title>" --folder <Folder> --slug <slug> --tag <TAG>
```

然後直接編輯 bank.json 的 `sections`（字串，或 `{"name": …, "hint": "這段要用這些句子做什麼"}`）。
`categories` 等檢索看過全貌再填。結構見 `references/bank-schema.md`。

## 1. 檢索（paper-navigator）

有安裝 `paper-navigator` 就用 Skill 工具載入，照它的 ITERATIVE 流程做（RUBRIC → Probe → R2/R3/R4 → 分層篩選），
它的規則全部適用。沒有安裝時：臨床題目照下面的要求，用內建的 `pubmed_search.py` 自己做多輪檢索（先寫納入條件、
試探查詢、依段落骨架分角度擴充，直到新查詢幾乎不再帶出新文獻）；非生醫題目告訴使用者要先安裝 paper-navigator。
以下是引用庫特有的要求：

- **臨床／生醫題目一律查 PubMed＋Europe PMC**，用內建腳本（paper-navigator 若也附 `pubmed_search.py`，兩者介面與
  輸出相同）：

  ```bash
  python "$RS/pubmed_search.py" -q "<查詢>" --source both --pub-type rct,sr,ma --limit 50 \
    --output <scratchpad>/pool.jsonl --append      # --print-query 只看 PubMed 改寫後的檢索式與總筆數
  ```

  研究設計用 `--pub-type`（`ma`、`sr`、`rct`、`ct`、`guideline`、`observational`、`review`、`case-report`），
  不要把 "meta-analysis" 打進查詢字串，否則連只是引用統合分析的文章都會被找進來。
- **每一次查詢都記下來**：輸入字串、出版類型篩選、PubMed 總筆數、PubMed 改寫後的檢索式（`--print-query` 或 stderr）。
  這是之後 search log 與 Methods 的材料，事後補不回來。
- **「可採用」的範圍比「最相關」寬。** 寫一篇稿子需要的不只是直接證據，還有：指引與政策立場、背景數字（盛行率、
  自然病程）、方法學先例（定義、終點、統計作法）、機轉、對照基準（一般族群或相近族群的風險）、結論相反的研究。
  檢索角度（angle tags）要對應段落骨架，每個段落都要有文獻可掛。結論相反的研究要刻意找，不要只收支持主流說法的。
- **用核心文獻往外追**：對核心文獻做相似文獻查詢（PubMed MCP 的 `find_related_articles`，或 paper-navigator 的
  引文追蹤）、翻重要綜述與統合分析的納入清單。
- **OpenEvidence（`oe_ask`，有連接時）只當線索**：回答另存原文，裡面提到的文獻要回 PubMed 確認存在才納入，回答本身不引用。
- 篇數由題目決定（實際用過的題目約 110 到 130 篇），不要為了湊數收邊緣文獻。看過但沒納入、可能有用的，記在「待讀」。

檢索完成後定 `categories`（依文獻在稿件裡的角色分 5 到 9 類，A、B、C…），寫進 bank.json。
層級三級：`核心`（稿件一定會引、要優先讀全文）、`重要佐證`、`補充`。層級是閱讀與引用的優先順序，不是證據品質。

## 2. 抓書目進 bank

書目一律用腳本抓，不要手打。參考文獻是最不能有「看起來合理的猜測」的地方。

```bash
python "$RS/refbank.py" add --bank <bank> --pmids "24342975, 33988343, …"     # PubMed efetch：完整書目＋完整摘要
python "$RS/refbank.py" add --bank <bank> --dois "10.1111/tri.13699"          # PubMed 沒收：Europe PMC → Crossref
python "$RS/refbank.py" add --bank <bank> --pool <pool.jsonl> --ids "<paperId>,…"   # pool.jsonl 裡的紀錄（S2／arXiv 等非生醫題目）
```

引用鍵自動產生（`Smith2022`，撞名加 b、c）。PubMed 與 DOI 都查不到的文件（政策文件、學會網頁）才在標註檔裡用
`"new": true` 手動建立，書目只能來自你實際讀到的頁面。

## 3. 標註可引用的部分

這是整個 skill 的核心產出。分批做，每批約 10 篇：

```bash
python "$RS/refbank.py" show --bank <bank> --pending --limit 10          # 印出還沒標註的 10 篇完整摘要
# 讀完後把標註寫成 <scratchpad>/ann-01.json（格式見 references/bank-schema.md），再併入：
python "$RS/refbank.py" annotate --bank <bank> --from <scratchpad>/ann-01.json
```

`annotate` 併入後會立刻對這一批做逐字比對，抄錯的當場列出來，修好重送即可。不要手改 bank.json 裡的大段內容；
評審後的小幅改字可以直接 Edit，改完跑 `verify`。

每篇給：`category`、`tier`、必要時修正 `doc_type`、`note`（引用這篇前該知道的事）、以及 `quotes`。每一句 quote 是一列：

| 欄位 | 內容 |
|---|---|
| `section` | 這句話適合放的稿件段落（bank.sections 之一） |
| `claim` | 這句話能支持的論點，繁體中文一句 |
| `quote` | 英文原文，從摘要或全文**原樣複製** |
| `source` | `abstract`、`fulltext`（開放取用全文已存檔），或 `paraphrase`（有版權的全文：不抄原句，只寫 `locator`） |
| `secondhand` | 這句是該文對別的文件（指引、政策）的轉述時設 `true` |

**引用句怎麼挑。** 一篇通常 2 到 5 句，核心文獻可以更多。挑稿件真的會用到的：背景事實、主要結果與數字、
作者結論、方法定義、作者自陳的限制。引用句要能獨立撐起論點：分母、比較組、終點定義、追蹤時間若在相鄰句子裡，
就把相鄰句一起收進來或另開一列。有一次評審後補了 26 句，補的全是分母、終點定義與比較組。

**論點怎麼寫。** 逐字比對只保證引用句沒抄錯，出錯的幾乎都是中文論點。過去一輪 Codex 評審抓到約 90 處，型態很固定：

- 濃縮時改掉了終點、比較組、時間範圍或適用條件。論點的範圍要和引用句一樣寬，不能更寬。
- 把「未檢出顯著差異」寫成「沒有差異」或「風險相同」。人數少的研究尤其要保留原意。
- 數字沒帶分母或單位；把次群組的數字寫成整個世代的。
- 把作者的結論寫成既定事實。作者的主張就寫「作者結論：…」。
- 加進來源沒有的資訊（例如摘要沒說量了幾次，論點卻寫「單次測量」）。
- 轉述他方指引的句子沒標明。寫成「依 X 2023 轉述：…」，並設 `secondhand`；這類立場在取得原文前都是二手。
- 有年代性的描述沒標年代。寫成「（2014 年發表時的描述）…」。

**全文。** 預設以摘要為準。核心文獻盡量取得全文：開放取用的用 paper-navigator 的 `fetch_paper.py` 存到
`<主題資料夾>/papers/`，在標註裡設 `fulltext` 路徑，全文引用句同樣會逐字比對（沒有 paper-navigator 就以摘要為準）。
需要機構權限的全文若透過使用者的瀏覽器讀到，只記摘述與位置（`source: paraphrase`），不抄原句。
摘要與全文不一致的地方寫進 `note`。
哪些讀了全文、哪些只讀摘要，要在 README 如實交代。

摘要裡找不到可引用句的文獻可以留空（會自動標「無引用句-需全文」），不要硬湊。

## 4. 驗證、寫總結、匯出

```bash
python "$RS/refbank.py" verify --bank <bank>
```

全部通過後寫**總結段落**（`<scratchpad>/narrative.md`，建議標題與寫法見 `references/deliverables.md`）：目前證據的樣貌、
先讀哪幾篇、引用前要注意的地方、沒有涵蓋的範圍、看過但沒納入的待讀文獻。文獻用 `[@引用鍵]` 標示，匯出時自動換成編號。
寫的時候守兩條：「本庫沒找到」不等於「文獻不存在」；可能重疊的世代不要算成兩份獨立證據。

```bash
python "$RS/refbank.py" export --bank <bank> --narrative <scratchpad>/narrative.md
```

產出主報告 `.md`（總結＋依稿件段落排列的引用句表＋完整引用庫＋參考文獻）、`.csv`（一列一句）、`.bib`、`.ris`，
統計數字由腳本算，不要手寫。另外手寫 search log（格式見 `references/deliverables.md`）。

## 5. 交叉評審（cross-review）

用 Skill 工具載入 `cross-review`，讀它要求的兩份 reference。送審方式照下面做（細節與失敗處理見
`references/cross-review.md`）：

```bash
python "$RS/refbank.py" packet --bank <bank> --out-dir <scratchpad>/xr --narrative <scratchpad>/narrative.md
```

這會產生內嵌全部材料的封包（每篇的完整摘要、每一列的論點與引用句、總結段落、審查指示），並印出送審指令。
用 Bash 工具以 `run_in_background` 執行那條指令。Codex 用 xhigh 跑 200 KB 左右的封包約需 10 到 15 分鐘。
`packet` 說找不到 cross-review，或 `codex` 指令不存在、沒登入時，照 `references/cross-review.md` 的失敗處理：
如實回報，不要用別的方式假裝做過評審。

等待期間做不依賴評審結果的事：`python "$RS/zotero_import.py" check --bank <bank>`（唯讀查重）、寫 README 草稿、
寫 search log。完成後讀 `<scratchpad>/xr/run/assessment.md`。

如果背景工作被系統以記憶體不足中止：不要自行重跑。先用 `assessor-run.log` 尾端留下的中途訊息做能做的修訂，
然後停在這裡回報，由使用者決定是否重跑；這種情況下不要匯入 Zotero。

## 6. 裁定與修訂

Codex 的意見不能照單全收，也不能略過。逐條裁定：成立、部分成立、不採納（寫理由）。

- 它只看得到封包。標為「材料不足」的項目，自己拿完整摘要或全文再查一次；有一次這類 12 項裡有 10 項其實有依據，
  只是引用句截太短，處理方式是補引用句，不是刪論點。
- 修訂用新的標註檔重跑 `annotate`（或小幅 Edit），之後 `verify` 必須再次全數通過。
- 總結段落被指出的問題要改 `narrative.md`。
- 寫交叉評審紀錄 `<YYYY-MM-DD>-<slug>-cross-review.md`：經過、Codex 總評（原文引用）、逐條裁定表、不採納的理由、
  仍未解決的問題。格式見 `references/cross-review.md`。
- 標記完成並重新匯出：

```bash
python "$RS/refbank.py" mark-reviewed --bank <bank> --reviewer "Codex CLI <版本>（<模型>，<effort>）" --record <紀錄檔名>
python "$RS/refbank.py" export --bank <bank> --narrative <scratchpad>/narrative.md
```

預設評審一輪。修訂幅度很大（例如總結整段重寫）或使用者要求時才送第二輪。

## 7. 匯入 Zotero

```bash
python "$RS/zotero_import.py" check  --bank <bank>     # 唯讀：連線、分類是否存在、查重
python "$RS/zotero_import.py" import --bank <bank>     # 建分類（若無）→ 存入 → 移進分類＋標籤 → 讀回核對
```

`import` 會自己處理：Zotero 沒開就啟動、分類不存在就建在文獻庫頂層、依標籤組合分批存入、每筆附引用句附註、
最後從本機 API 讀回，確認每筆都在分類裡、有標籤、有附註，並把 item key 寫回 bank、過程寫進匯入紀錄 JSON。

要知道的行為（完整說明與疑難排解見 `references/zotero.md`）：

- 已在目標分類裡的文獻會跳過，所以失敗後重跑是安全的。
- 文獻庫別處已有的同一篇（DOI 或 PMID 相同）：連接器無法把既有項目加進分類，所以預設另存一筆進分類，並加標籤
  `<TAG>/重複-待合併`，回報時請使用者到 Zotero「重複項目」合併。使用者不要重複項目時用 `--on-duplicate skip`。
- 只動目標分類。其他既有分類與項目一律不碰。
- Zotero 有設定同步時，匯入的內容會同步到 zotero.org。
- 標籤：`<TAG>`、`<TAG>/<類別>`、`<TAG>/<層級>`、`<TAG>/無引用句-需全文`、`<TAG>/二手轉述-待核對原文`。

讀回核對有問題時，如實回報問題清單，不要宣稱匯入完成。

## 8. 存檔與同步

位置依 `refbank.py config` 的設定；使用者的 `CLAUDE.md` 另有規定時以 `CLAUDE.md` 為準。

1. 所有保留性檔案在 `<output_root>/<Folder>/`：bank.json、主報告 `.md`、`.csv`、`.bib`、`.ris`、
   search log、交叉評審紀錄、Zotero 匯入紀錄、OpenEvidence 回答、`papers/`、`README.md`。過程檔（pool.jsonl、
   標註批次檔、送審封包）留在 scratchpad。
2. 寫 `README.md`（檔案清單、目前結論、引用前要注意、交叉評審、Zotero、沒有涵蓋的範圍、下一步）。
   範本見 `references/deliverables.md`。README 要寫明 `.ris` 已經匯入過、不要再匯一次。
3. 設定了 `obsidian_vault` 時，把整個主題資料夾複製到 `<obsidian_vault>/<obsidian_subdir>/<Folder>/`，vault 副本的
   README 把 `.md` 檔改成 `[[wikilink]]`；也設定了 `obsidian_moc` 就在那則筆記的表格新增或更新一列。
   細節見 `references/deliverables.md`。沒設定 vault 就跳過這步。

## 9. 回報

簡短，先講使用者接下來用得到的：

- 幾篇、幾句引用句、幾類；Zotero 分類名稱與筆數，讀回核對是否通過。
- 這個題目的證據樣貌，三到五句。
- 引用前最容易出錯的幾點。
- 交叉評審：Codex 提了幾條、採納幾條、最重要的修訂是什麼。
- 沒有涵蓋的範圍（沒查的資料庫、沒取得的全文與指引原文）。臨床題目只查了 PubMed 與 Europe PMC 時，
  要明講沒查 Embase 與中文資料庫，不能據此說「沒有相關研究」。
- 需要使用者動手的事（合併重複項目等）。
- 存檔路徑（有同步 Obsidian vault 時，本機與 vault 兩邊都給）。

這份引用庫是寫稿用的引用來源整理，不是系統性回顧的正式檢索；產出的每份文件開頭都要保留「AI 產出、未經人工核對」的標示。

## 續作與修訂

- **補文獻**：`add` → `annotate` → `verify` → 只把新增的列送審（文獻少時可以自己對照摘要複核，並在紀錄註明沒有送 Codex）
  → `export` → `zotero_import.py import --batch-tag "<TAG>/補入-<日期>"`。
- **已匯入的附註要改**：改 bank 後 `import --replace-stale`（或 `--replace 引用鍵,…`）。腳本會匯入修訂版、產生一個
  `.url` 捷徑讓 Zotero 選取舊版，由使用者移到垃圾桶，之後跑 `zotero_import.py verify` 確認。步驟與注意事項見
  `references/zotero.md`。
- **核對現況**：`zotero_import.py verify --bank <bank>`（唯讀）。

## 參考檔

| 檔案 | 什麼時候讀 |
|---|---|
| `references/bank-schema.md` | 編輯 bank.json 或寫標註檔之前 |
| `references/cross-review.md` | 送審、等待、失敗處理、寫裁定紀錄 |
| `references/zotero.md` | 匯入出問題、要取代已匯入的項目、想知道連接器的限制 |
| `references/deliverables.md` | 寫總結段落、search log、README、同步到 Obsidian vault |
