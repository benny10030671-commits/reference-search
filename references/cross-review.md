# 交叉評審：送審、等待、裁定

對象是引用庫，不是程式碼，所以不用 cross-review 的 diff 模式，而是把整份材料內嵌成封包送給 Codex。

## 為什麼要內嵌

- Codex 在唯讀沙箱裡跑，讀不到本機檔案；封包必須自給自足。
- Codex 預設會上網逐句查證，200 KB 的封包會跑到逾時。封包開頭已寫明「不要讀磁碟、不要上網」，它會遵守。
- Codex 只能拿論點對照封包裡的文字。沒附摘要的文獻它一律標「材料不足」，所以封包預設內嵌每一篇的完整摘要，
  全文引用句會附上前後文各約 500 字元。

## 送審

```bash
python "$RS/refbank.py" packet --bank <bank> --out-dir <scratchpad>/xr --narrative <scratchpad>/narrative.md
```

產生 `xr/packet.md`（審查指示＋總結段落＋逐篇材料）與 `xr/scope.md`（給 `--path` 用的短說明，讓 helper 判定
prompt 是自給自足的）。腳本會印出完整的送審指令，形如：

```bash
bash "<cross-review 的 scripts/run-cross-review.sh>" \
  --repo "<xr 目錄>" --path scope.md --context-file "<xr 目錄>/packet.md" \
  --output-dir "<xr 目錄>/run" --timeout-seconds 2400 --question '…'
```

用 Bash 工具、`run_in_background: true` 執行。注意：

- cross-review 的位置由腳本自動尋找（`~/.claude/skills/cross-review/`、本 skill 旁邊、或 `config.json` 的
  `cross_review_script`）。找不到時 `packet` 會提醒，見下方失敗處理。
- Windows 上路徑用 `/c/Users/…` 形式（腳本印出來的就是），因為 helper 在 Git Bash 裡跑。
- 模型與 reasoning effort 沿用 `~/.codex/config.toml`，不要加 `--model`。啟動後看 `xr/run/assessor-run.log` 開頭的橫幅，
  把 Codex 版本、模型、effort 記下來，裁定紀錄要寫。
- 封包大小：實測 163 KB 約 11 分鐘、233 KB 約 14 分鐘（reasoning effort xhigh）。超過 320 KB 時腳本會提醒；可用 `--abstracts core`
  （只附核心文獻的摘要），或分兩批送（先核心與重要佐證）。
- 可以先加 `--dry-run` 看 `xr/run/prompt.md` 長什麼樣，不會呼叫 Codex。

## 等待與失敗

背景工作結束時會收到通知，不要輪詢。這段時間做 Zotero 唯讀查重、README 草稿、search log。

| 情況 | 處理 |
|---|---|
| 正常完成 | 讀 `xr/run/assessment.md` |
| 逾時 | 看 `run-summary.txt` 與 `assessor-run.log`；log 尾端通常有中途訊息可用。改用 `--abstracts core` 或分批重送一次 |
| 被系統以記憶體不足中止（通知寫 `killed … running low on memory`） | 不要自行重跑。用 log 尾端的中途訊息做能做的修訂，停下來回報，由使用者決定是否重跑；這種情況下不要匯入 Zotero |
| `codex` CLI 不存在或未登入、或找不到 cross-review skill | 如實回報，並指向 README 的安裝說明（`doctor.py` 會列出缺什麼）。不要改用別的方式假裝做過交叉評審；未評審就不匯入，除非使用者明講要 |

## 裁定

逐條處理，三種結果：

- **成立**：照改。
- **部分成立**：寫清楚採納哪一部分。
- **不採納**：寫理由。最常見的理由是 Codex 沒看到完整材料而存疑，而你對照摘要或全文後確認有依據。這時通常該做的是
  補一句引用句，讓依據出現在引用庫裡。

「材料不足」的項目一律自己再查一次，不要因為它沒說錯就略過。

修訂方式：寫新的標註檔重跑 `annotate`（整篇的 `quotes` 重送，或用 `add_quotes` 補句），或直接 Edit bank.json 改個別
`claim`。改完跑 `verify`，必須再次全數通過。總結段落的問題改 `narrative.md`。

## 裁定紀錄

存成 `<YYYY-MM-DD>-<slug>-cross-review.md`（日期是評審當天）。結構：

```markdown
# 交叉評審紀錄：<題目> 引用庫

日期：…｜外部評審：Codex CLI <版本>（<模型>，reasoning effort <effort>）｜裁定與整理：Claude Code（AI）｜未經人工核對

## 經過
送審的是什麼（幾篇、幾列、封包大小、是否含總結段落）、跑了多久、有沒有中斷。
Codex 只看得到內嵌材料；裁定是我對照摘要與手上的全文做的。

## Codex 的總評
> 原文引用兩三句。

## 修訂後的數字
幾篇、幾句；這一輪改寫幾句論點、新增／置換幾句引用句、幾篇有變動。（數字用 `refbank.py stats` 重新取。）

## 逐條裁定
| 位置（引用鍵#列，或總結的哪一段） | Codex 的意見 | 裁定 | 處理 |

## 不採納或只部分採納的項目
逐條寫理由。

## 仍未解決的問題
需要全文或指引原文才能確認的項目。
```

最後一句固定寫：兩個 AI 互相評審不能取代對照原文 PDF 的核對。

寫完後：

```bash
python "$RS/refbank.py" mark-reviewed --bank <bank> --reviewer "Codex CLI <版本>（<模型>，<effort>）" --record <紀錄檔名>
```

評審失敗而使用者仍要匯入時，用 `--status failed` 記錄實情，匯入時加 `--allow-unreviewed`；Zotero 附註的頁尾會如實寫
「尚未經過交叉評審」。
