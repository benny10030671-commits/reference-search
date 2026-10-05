# Zotero 匯入：能做什麼、不能做什麼、出問題怎麼辦

`zotero_import.py` 對執行中的 Zotero 桌面程式講兩種介面。需要 Zotero 7 以上（本機 API 從 7 開始才有），在 Zotero 9 上測過：

| 介面 | 位址 | 能力 |
|---|---|---|
| 連接器伺服器 | `http://127.0.0.1:23119/connector/*` | 唯一的寫入途徑。只能**新增**項目與空分類 |
| 本機 API | `http://127.0.0.1:23119/api/users/0/*` | 唯讀（每個端點只支援 GET）。用來查重與讀回核對 |

## 做不到的事

讀過 Zotero 9 原始碼確認，不用再試：

- 不能修改已存在的項目或附註。
- 不能刪除項目。
- 不能把已存在的項目加進另一個分類。

所以附註要在匯入前定稿（先交叉評審），既有文獻要進新分類只能另存一筆再由使用者合併。

## import 實際做的事

1. `GET /connector/ping`。連不上就啟動 Zotero 並等待（Zotero 首次啟動後可能自己重啟一次）。程式位置依作業系統找預設
   安裝處（Windows `Program Files\Zotero`、macOS `/Applications/Zotero.app`、Linux 的 `zotero` 指令）；
   裝在別處就在 `config.json` 設 `zotero_exe`。
2. 本機 API 讀出文獻庫全部頂層項目，以 DOI、PMID（都沒有時用標題）比對 bank 的每一篇，分成：
   新增／已在目標分類／文獻庫別處已有。垃圾桶裡的項目不在清單內。
3. 目標分類不存在時建立：先確認 Zotero 視窗選在「我的文獻庫」根目錄，否則新分類會掛在被選取的分類底下。
   選取不在根目錄時，腳本會開啟 `zotero://select/library/items/<某個不在目前分類裡的項目>` 把選取切回根目錄，
   然後 `POST /connector/import` 送一段只含 `<z:Collection>` 的 RDF。這會動到使用者的 Zotero 視窗選取位置。
4. 依「標籤組合」把文獻分組，一組一個工作階段（每個最多 20 筆）：`POST /connector/saveItems`（201）→
   `POST /connector/updateSession`，把這一批移進目標分類並套上標籤（200）。標籤是整個工作階段共用的，所以才要分組。
5. 讀回分類內容，逐筆確認：找得到、標籤齊全、有子附註。item key 寫回 bank 的 `zotero` 欄。
6. 全程寫匯入紀錄 `<date>-<slug>-zotero-import-log.json`（每個工作階段的狀態碼、略過與重複的清單、核對結果）。

工作階段只存在 Zotero 的記憶體裡。中途失敗時，已存入的批次留在 Zotero；修正後重跑 `import`，已在分類裡的會被跳過。

## 選項

| 選項 | 用途 |
|---|---|
| `--dry-run <檔>` | 不寫入 Zotero，把要送出的項目與標籤寫成 JSON 檢視 |
| `--on-duplicate copy`（預設）／`skip` | 文獻庫別處已有同一篇：另存一筆進分類並標 `<TAG>/重複-待合併`，或略過 |
| `--batch-tag "<TAG>/補入-<日期>"` | 這一批額外加一個標籤 |
| `--replace-stale`／`--replace 鍵,鍵` | 以修訂版取代已匯入的項目（見下） |
| `--allow-unreviewed` | bank 尚未標記評審完成也匯入。只有使用者明講時才用 |
| `--no-launch` | Zotero 沒開時不要自動啟動 |

## 取代已匯入的項目

bank 記著每筆匯入時的附註指紋（`zotero.noteHash`）。之後改了引用句或備註，`check` 會把它列為「附註內容已和 bank 不同」。

1. `python "$RS/zotero_import.py" import --bank <bank> --replace-stale`。腳本匯入修訂版（多一個標籤 `<TAG>/修訂版-<日期>`），
   並在主題資料夾產生 `刪除舊版-<日期>.url`。
2. 請使用者開啟那個捷徑（macOS／Linux 用腳本印出的 `open "zotero://…"`／`xdg-open "zotero://…"` 指令）。
   Zotero 會在「我的文獻庫」選取舊版項目。項目有沒有真的被選取無法從 API 驗證，請使用者確認
   右側顯示的選取數量，然後按右鍵選「將項目移到垃圾桶」。
3. `python "$RS/zotero_import.py" verify --bank <bank>`：確認分類裡每篇各一筆、舊版已不在文獻庫。通過後刪掉 `.url` 捷徑。

**為什麼捷徑指向文獻庫根目錄，而且要說「移到垃圾桶」。** 在分類檢視裡按 Delete 只會把項目移出分類，文獻仍留在文獻庫。
實際發生過：使用者照指示刪了 67 筆舊版，分類的筆數對了，但那 67 筆其實還在文獻庫裡、不屬於任何分類，
當時的核對只看分類所以沒發現。現在 `verify` 會另外檢查「帶有本庫標籤但不在分類裡」的項目，找到時產生
`選取分類外的<TAG>項目.url`，由使用者決定要不要移到垃圾桶。

## 疑難排解

| 症狀 | 原因與處理 |
|---|---|
| 連線直接被關閉，看起來像伺服器沒起來 | User-Agent 以 `Mozilla/` 開頭會被 Zotero 當成瀏覽器擋掉。PowerShell 的 `Invoke-WebRequest` 就是這樣；一律用這支 Python 腳本 |
| 本機 API 回 403 或非 JSON | Zotero「設定 → 進階」要勾選「允許此電腦上的其他應用程式與 Zotero 通訊」 |
| 切不回文獻庫根目錄 | 請使用者在 Zotero 左欄點一下「我的文獻庫」後重跑 |
| `saveItems` 回 500 | 多半是某筆項目的欄位有問題。用 `--dry-run` 輸出檢視該批內容；紀錄檔裡看得到是哪個工作階段 |
| `updateSession` 回 400 `SESSION_NOT_FOUND` | Zotero 在 saveItems 之後重啟過。該批已存入但沒進分類、沒標籤；回報使用者，請他在 Zotero 用「加入日期」排序後手動拖進分類 |
| 讀回筆數不對 | 不要宣稱完成。把 `problems` 清單原樣回報 |
| `extra` 欄讀回是空的 | 正常。新版 Zotero 會把 `PMID:`、`PMCID:`、`Citation Key:` 拆進原生欄位 |
| macOS／Linux 開不了 `.url` 捷徑 | 改用腳本印出的 `open`／`xdg-open` 指令（網址也記在匯入紀錄的 `delete_old_url`） |

## 使用者的文獻庫

只動這次的目標分類。使用者既有的分類與項目一律不碰；新主題建自己的分類。Zotero 有設定同步時，匯入的內容會同步到
zotero.org。
