# FallGuard Flow 3 — n8n + app 內決策（v10）

| 在 n8n 上 | ID | 狀態 |
|---|---|---|
| **FallGuard Flow 3**（主流程，64 節點） | `2SLBAAPf3ltj6aRh` https://hollyhsu.app.n8n.cloud/workflow/2SLBAAPf3ltj6aRh | **Active**，Production URL `https://hollyhsu.app.n8n.cloud/webhook/fallguard` |
| **FallGuard pending actions**（app 用的兩個小端點，5 節點） | `0KIMYU1XyOKjmN1O` | **Active**，`GET /webhook/fallguard-pending`（待辦）、`GET /webhook/fallguard-profile?source=<檔名>`（會通知誰） |
| FallGuard Flow 2（舊版，決策用 email 表單） | `cQKrctBdeAfQDsbe` | Inactive，可刪 |
| FallGuard workflow（最早手動建的 11 節點） | `dmbJ7HEspJpnvuKK` | 可刪 |

2026-09-13 全流程實測通過：`episode_closed` → 報告信 → app 內 post-fall check（Call 000）→ 轉院信 + 家屬通知核准（app）→ 家屬通知 → Outcome（app，Admitted）→ 住院通知、事件草稿、`incidents` 一列 → 衛教單核准（app）→ 衛教單寄家屬 → Follow-up。

## 0. 本資料夾

| 路徑 | 用途 |
|---|---|
| `fallguard_v9/` | **app v10**：`fall_episode.py`、`app_v9.py`（跌倒短片 + 新的 **Actions** 頁）、`make_real_clip_payload.py`、測試 |
| `FallGuard Flow 3.json`、`FallGuard pending actions.json` | 跟 n8n 上一致的兩個 workflow（備份／匯入用） |
| `build_workflow.py` | 產生上面兩個 JSON；改 Code、prompt、信件文字後重跑 |
| `n8n_build/n8n_build/code/*.js`、`*.md` | Code 節點與 prompt（四個 each-item 腳本已修成 `return {…}`） |
| `sheets/*.csv` | 五個分頁範本：`residents`（含 chat ID）、`event_log`、`incidents`、`education_blocks`、**`pending_actions`（新）** |
| `test_payloads/` | 六個手冊 payload + 含合成／真實 mp4 的三個 + `sample_clip.mp4` |
| `docs/` | 設計說明 |
| `_backup_v9_before_clip/` | 改動前的原始檔 |

## 1. 架構（跟手冊不同的四點）

**(a) Telegram 傳影片**
```
Build alert / Build escalation ─> IF has clip ─true─> Clip to file ─> Telegram video
                                         └─false─> IF has snapshot ─true─> Snapshot to file ─> Telegram photo
                                                                  └─false─> Telegram text
```
**(b) LLM**：`Report LLM`、`Compose LLM` 是 Anthropic 節點（`claude-opus-5`，n8n Gateway credits），後面各接一個 Set 節點（`Report`／`Compose sheet`）把回覆放到 `$json.text`。手冊的 Basic LLM Chain 組合 Gateway 不支援。

**(d) 病人資料查詢有退路**：`Profile lookup` 讀整個 `residents` 分頁，`Pick profile`（Code）依 `source_name` 找列，找不到就用名為 `default` 的列，再找不到就用第一列（輸出多一個 `profile_match` = exact / fallback）。所以不用每支影片都先加一列；只要第一列（或 `default` 列）的 chat ID 與 email 正確即可。

**(c) 決策在 app 裡做，email 只通知**。四個決策點各是四個節點：
```
<通知 email> ─> Queue <step>（寫一列到 pending_actions）─> Wait <step>（等 webhook）─> Done <step>（標 done）─> <adapter Code>
```
adapter 節點沿用手冊的名字（`Caregiver form`、`Supporter approval`、`Outcome form`、`RN / GP approval`），輸出跟原本一樣的欄位，所以下游節點都沒改。app 的 **Actions** 頁每次開啟會 GET `fallguard-pending` 拿 `status=open` 的列，填完直接 POST 到該列的 `resume_url`，並附 `decided_by`、`decided_at`（n8n 那邊會存在 `Caregiver form` 等節點的輸出裡）。

| step | app 上的表單 | 回傳給 n8n |
|---|---|---|
| `post_fall_check` | 六個下拉 + Notes | `Decision` 等欄位 |
| `supporter_notice_approval` | Approve / Decline | `approved` |
| `outcome` | Outcome / Hospital / Notes | 三欄 |
| `education_sheet_approval` | Approve / Decline | `approved` |

## 2. Email（都只是通知，HTML 版型）

每封都是同一個版型：深綠標題列「FallGuard」＋右上角步驟徽章（Step 1 of 4…）、大標題、紅色「ACTION NEEDED」框（或藍色「FOR YOUR INFORMATION」框）寫明要做什麼、下面才是報告／草稿全文（Claude 的 **粗體** 會轉成真正的粗體）、頁尾免責聲明。版型在 `build_workflow.py` 的 `html_mail()`，改一處全部生效。

| 主旨 | 收件人 | 內容 |
|---|---|---|
| `FallGuard · Step 1 of 4 · Fall report for <name> — action needed` | 照顧者 | 第一段寫「先去看人，然後到 app → Actions 填 post-fall check」，接著 Claude 的四項報告 |
| `FallGuard · Transfer pack and ISBAR handover for <name>` | 照顧者 | Call 000 才有；轉院清單 + ISBAR，不用做任何動作 |
| `FallGuard · Step 2 of 4 · Approve the family notice for <supporter> — action needed` | 照顧者 | 附將寄給家屬的草稿，請到 app 核准 |
| `FallGuard · Fall notice for <name>` | 家屬 | 核准後才寄 |
| `FallGuard · Fall — <name> — camera record for GP review` | GP | 選 GP 分支才有 |
| `FallGuard · Step 3 of 4 · Record the outcome for <name> — action needed` | 照顧者 | 請到 app 記錄結果 |
| `FallGuard · Hospital admission — <name>` | 家屬 | Outcome = Admitted 才有 |
| `FallGuard · Incident report DRAFT — <name> (not submitted)` | 照顧者 | 草稿，`[ ]` 欄位要人填，不用做 app 動作 |
| `FallGuard · Step 4 of 4 · Nurse/GP approval of the information sheet for <name> — action needed` | 照顧者（扮演 RN） | 附衛教單全文，請到 app 核准 |
| `FallGuard · After a fall — information for the carer of <name>` | 家屬 | 核准後才寄 |

要改文字：`build_workflow.py` 裡搜尋 `gmail_send(`（框內文字是 `html_mail()` 的第三個參數，可用 `<b>`），改完 `python build_workflow.py`，再用 MCP `updateNodeParameters` 推上去（或匯入 JSON 取代）。

## 3. Demo 步驟

1. 試算表「Fall Response Report」要有五個分頁；`pending_actions` 分頁第一列是 `created_at, episode_id, resident, step, detail, resume_url, status`；`residents` **每一列**的 chat ID 都填 `<carer chat id>`、email 填 `<demo gmail address>`（尤其 `webcam` 列，目前還是佔位值）。影片檔名不在表裡時會自動用第一列。
2. `cd fallguard_v9 && streamlit run app_v9.py`。側欄：勾 `Send events to n8n`、`Include fall clip`；Webhook URL `https://hollyhsu.app.n8n.cloud/webhook/fallguard`；Auth key = `Webhook.txt`（**Actions 頁也用這兩個欄位**，沒填會顯示 0 pending 加紅字錯誤）。
   外接攝影機：Live Camera 模式先按 **Detect cameras**，把 **Camera index** 設成有開成功的號碼（外接通常是 1），再 Start。Windows 上會先用 DirectShow 開，打不開會顯示原因。
3. Dashboard 上傳 `video (13).avi` → 畫面先出現綠色「Notifications ready: video (13).avi → resident Mrs A. Chen … Telegram chat <carer chat id> …」（黃色表示 chat ID 是佔位值或查不到，先去改表）→ 2.5 秒後 Telegram 收到影片 → 影片結束送 `episode_closed` → 信箱收到「Step 1 of 4」報告信。
4. 切到 **Actions** 頁 → 填名字 → 出現「Post-fall check — Mrs …」→ 填完 Submit。之後每收到一封 `action needed` 的信就回到 Actions 頁 Refresh，依序做 2/4、3/4、4/4。
5. 不跑影片時：Settings 頁「Send test payload」或 `test_payloads/curl_test.sh episode_closed`（用 Production URL，不必按 Listen）。

## 4. 已知限制

- `pending_actions` 裡逾時（Wait 2 小時）的列會一直 open，app 會顯示；demo 前把 `status` 手動改 `done` 或刪掉舊列。
- 決策人身分靠 app 的名字欄位，沒有登入；正式版接機構 SSO／臨床系統（見合規報告 §3、§6）。
- Telegram 帶影片只適用居家、家屬已同意的情境；機構情境要改成 SMS／推播 + 登入後看影片。
- n8n Cloud 試用：1,000 次執行、單次 180 秒、等待中的執行會被卸載；Wait 逾時設 2 小時。

## 5. 除錯

| 現象 | 處理 |
|---|---|
| Actions 頁 0 pending（但 curl API 有資料） | 側欄 Webhook URL 或 Auth key 沒填／填錯；標題下方會寫 `N rows returned` 與實際查的 URL |
| Actions 頁顯示 `Could not load pending actions` | `fallguard-pending` workflow 沒 Active，或 Auth key 錯（403），或 `pending_actions` 分頁不存在 |
| Actions 頁一直沒有新待辦 | 看 n8n Executions：卡在 `Queue …` 表示分頁欄名不對；卡在 `Report LLM` 表示 Gateway 額度用完 |
| 按 Submit 回 404 | 該執行已逾時（2 小時）或已被回覆過；重新送事件 |
| 攝影機打不開 | 其他程式佔用（Teams／Zoom／瀏覽器）、Windows 隱私設定沒開相機、或 index 錯；按 Detect cameras |
| 所有 Telegram 節點 `chat not found` | Telegram 憑證 token 不是 `@FallGuard42028Bot` 的；或 residents 沒有 chat ID |
| Telegram 收到純文字沒影片 | payload 沒 `clip`：app 沒勾 `Include fall clip` 或沒裝 `imageio-ffmpeg` |
| `A 'json' property isn't an object` | each-item Code 回傳陣列；用修正後的 js |
