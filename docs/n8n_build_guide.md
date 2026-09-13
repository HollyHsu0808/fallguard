# FallGuard × n8n 實作手冊（v1，黑客松可行範圍）

對應流程圖：`fallguard_flow_v1.html`（整體）、`figs/fig2`（Stage 1–2 畫布）、`figs/fig3`（Stage 3–4 畫布）。
本資料夾內容：

| 路徑 | 用途 |
|---|---|
| `figs/fig1_connection` | 腳本與 n8n 怎麼接 |
| `figs/fig2_canvas_stage1-2`、`fig3_canvas_stage3-4` | 畫布上每個節點的位置與連線，**節點名稱要照圖上打** |
| `figs/fig4_webhook_settings`、`fig5_switch_rules`、`fig6_form_wait` | 三個最容易填錯的節點 |
| `code/*.js` | 貼進 Code 節點的程式（檔名 = 節點名） |
| `code/report_prompt_v2.md`、`code/education_compose_prompt.md` | 兩個 LLM 節點的 prompt |
| `sheets/*.csv` | Google Sheet 四個分頁的範本 |
| `test_payloads/*.json`、`curl_test.sh` | 不用跑影片就能測每一種事件 |

分四個階段，每個階段結尾有測試。Stage 1–2 是 demo 的核心（約半天），Stage 3–4 是延伸（再半天）。單人做也可以，兩人分工建議：一人 Stage 1–2，一人先做 §0 的 Telegram 與 Sheet，再接 Stage 3。

---

## 0. 準備（約 30 分鐘）

### 0.1 帳號與金鑰
1. **n8n Cloud**：隊友的帳號。試用版限制：1,000 次執行、同時 5 個執行、單次執行 180 秒逾時。等待人工回覆的執行會被卸載到資料庫暫停，照理不算執行時間，**但請在 Stage 3 第一次測試時故意等 5 分鐘再回覆表單**，確認執行沒有被逾時砍掉；若被砍，把 Wait 節點的 Limit Wait Time 調短，或升級方案。
2. **Google 帳號**：Gmail 寄信 + Google Sheets。建議用一個專為 demo 建的 Gmail 帳號，不要用私人帳號（n8n 會取得寄信與試算表權限）。
3. **Telegram**：手機裝 Telegram。
4. **LLM API 金鑰**：隊友的 Anthropic / OpenAI / Gemini 金鑰。
5. **共用密鑰**（Webhook 認證）：在筆電產生一串隨機字串，之後兩邊都用它：
   ```
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

### 0.2 Telegram 機器人與 chat ID
1. Telegram 搜尋 **@BotFather** → 傳 `/newbot` → 取名（顯示名）→ 取 username（必須以 `bot` 結尾，例如 `fallguard_demo_bot`）→ BotFather 回一串 **token**（`123456:ABC-…`），先存起來。
2. 每個要收訊息的人（照顧者、家屬 supporter）用自己的 Telegram 搜尋這個 bot，按 **Start**，隨便傳一句話。**沒按過 Start 的人，機器人無法主動傳訊給他。**
3. 在瀏覽器開 `https://api.telegram.org/bot<token>/getUpdates`，找 `"chat":{"id":123456789,…}`，那個數字就是該人的 **chat ID**。demo 時照顧者與家屬可以是同一支手機（同一個 chat ID）。
4. 訊息裡有 `_` 的檔名（例如 `demo_fall_01.mp4`）會讓 Telegram 預設的 Markdown 解析出錯，所以後面每個 Telegram 節點都要把 **Additional Fields → Parse Mode** 設成 **HTML**。

### 0.3 Google Sheet
1. 新建試算表，命名 `FallGuard`。
2. 用 **File → Import → Upload** 匯入 `sheets/` 裡四個 CSV，匯入選項選 **Insert new sheet(s)**，然後把分頁改名成正好：`residents`、`event_log`、`incidents`、`education_blocks`（n8n 用名稱找分頁）。
3. `residents` 分頁：一列一個「攝影機／影片檔名 → 病人」的對應。`source_name` 必須等於 app 送出的 `source.name`（上傳影片就是**檔名**，攝影機模式是 `webcam`）。把 demo 要用的影片檔名填進去；`carer_email`、`supporter_email` 填 demo 帳號；`carer_telegram_chat_id` 填 0.2 拿到的數字。**不要填 Medicare 號碼、IHI、保險會員號**——表裡只有 `concession_card_type`（卡的種類），用來提醒救護車費用豁免。
4. `education_blocks`：八段官方來源的衛教文字，已寫好；要加中文就新增一列、`lang` 填 `zh`。

---

## 1. n8n 憑證（左側選單 → Credentials → Create）

| 憑證類型（搜尋名稱） | 填什麼 | 之後給哪個節點 |
|---|---|---|
| **Header Auth** | Name `X-FallGuard-Key`，Value = 0.1 的密鑰；憑證名稱取 `FallGuard key` | Webhook |
| **Telegram API** | Access Token = BotFather 的 token | 所有 Telegram 節點 |
| **Google Sheets OAuth2 API** | 按 Sign in with Google（n8n Cloud 內建） | 所有 Google Sheets 節點 |
| **Gmail OAuth2 API** | 同上，同一個 Google 帳號 | 所有 Gmail 節點 |
| **Anthropic API**（或 OpenAI / Google Gemini） | API key | Chat model 子節點 |

---

## 2. 建立工作流程

1. Workflows → Create → 取名 `FallGuard Flow 2`。
2. 右上角 Save 隨時存；左下角 **Inactive/Active** 開關先不要開，建完 Stage 1 測試通過再開。
3. 每加一個節點就**照 Fig 2／Fig 3 的名稱改名**（點節點標題即可改），因為後面的表達式用 `$('節點名')` 抓資料，名稱不一致就會 undefined。

---

## 3. Stage 1 — 進來、記錄、即時警示（Fig 2 上半部）

### 3.1 Webhook（Fig 4）
加節點 **Webhook**：HTTP Method `POST`；Path `fallguard`；Authentication `Header Auth` → 選 `FallGuard key`；Respond `Immediately`。
節點上方有兩個 URL：**Test URL**（`…/webhook-test/fallguard`，只在按下「Listen for test event」時收一次）與 **Production URL**（`…/webhook/fallguard`，工作流程 Active 後常駐）。

### 3.2 Event log（並聯在 Webhook 後）
加 **Google Sheets**：Resource `Sheet Within Document`；Operation `Append Row`；Document 從清單選 `FallGuard`；Sheet `event_log`；Mapping Column Mode `Map Each Column Manually`，欄位填：

| 欄 | 表達式 |
|---|---|
| received_at | `{{ $now.toISO() }}` |
| event_type | `{{ $json.body.event_type }}` |
| episode_id | `{{ $json.body.episode.episode_id }}` |
| source_name | `{{ $json.body.source.name }}` |
| status | `{{ $json.body.episode.status }}` |
| time_on_ground_sec | `{{ $json.body.episode.time_on_ground_sec }}` |
| head_risk | `{{ $json.body.episode.event_features.head_impact?.risk ?? '' }}` |
| fall_confidence | `{{ $json.body.episode.event_features.fall_confidence ?? '' }}` |
| tier | 留空（Stage 2 後可回填） |
| episode_json | `{{ JSON.stringify($json.body.episode) }}` |

把 Webhook 的輸出同時連到這個節點和下面的 Switch（一個輸出可以拉兩條線）。不記錄截圖（`snapshots`），一格塞不下也不必要。

### 3.3 Switch event_type（Fig 5）
加 **Switch**：Mode `Rules`。加五條規則，每條左邊都是 `{{ $json.body.event_type }}`，運算子 `is equal to`，右邊依序 `fall_detected`、`fall_analysed`、`escalation`、`episode_closed`、`test`；每條規則的 Options 開 **Rename Output**，輸出名打同樣的字。Fallback Output 留 `None`。

### 3.4 fall_detected 輸出
不接任何節點（安靜策略：警示在 2.5 秒後的 `fall_analysed` 發，已過濾誤報）。

### 3.5 fall_analysed 輸出 → IF false alarm
加 **IF**：條件 String → 左 `{{ $json.body.episode.event_features.fall_confidence }}`，`is equal to`，右 `likely_false_alarm`。**true** 輸出不接（event_log 已記錄）；**false** 輸出接下一個節點。

### 3.6 Profile lookup
加 **Google Sheets**：Operation `Get Row(s)`；Document `FallGuard`；Sheet `residents`；Filters → Add Filter：Column `source_name`，Value `{{ $json.body.source.name }}`。
找不到對應列時這個節點沒有輸出、後面不會跑——demo 影片的檔名一定要先填進表。

### 3.7 Build alert
加 **Code**：Mode `Run Once for Each Item`，Language JavaScript，貼 `code/build_alert.js`。它從試算表列拿 `carer_telegram_chat_id`，從 `$('Webhook')` 拿事件內容，輸出 `chat_id` 與 `text`。

### 3.8 Telegram alert
加 **Telegram**：Resource `Message`；Operation `Send Text Message`；Chat ID `{{ $json.chat_id }}`；Text `{{ $json.text }}`；Additional Fields → Parse Mode `HTML`。

### 3.9 escalation 輸出
複製 3.6（改名 `Profile lookup 2`）→ **Code** `Build escalation`（貼 `code/build_escalation.js`）→ **Telegram** `Telegram escalation`（設定同 3.8）。

### 3.10 測試 Stage 1
1. 筆電終端機，設定環境變數（先用 **Test URL**）：
   ```
   export N8N_WEBHOOK_URL="https://<team>.app.n8n.cloud/webhook-test/fallguard"
   export N8N_WEBHOOK_KEY="<密鑰>"
   ```
2. n8n 畫布按 **Listen for test event**（每按一次只收一個請求）。
3. `./test_payloads/curl_test.sh fall_analysed` → 終端機應印 `HTTP 200`，Telegram 收到「🔴 FALL DETECTED — Mrs A. Chen …」，`event_log` 多一列。
4. 再按 Listen，送 `fall_analysed_false_alarm` → 沒有 Telegram，`event_log` 有一列 `likely_false_alarm`。
5. 再按 Listen，送 `escalation_level3` → 收到「🚨 EMERGENCY …」。
6. 把密鑰改錯再送一次 → `HTTP 403`，這就是認證在工作。
7. 通過後把左下角切成 **Active**，之後改用 **Production URL**，不必再按 Listen。

`test_payloads/*.json` 裡的 `source.name` 都是 `demo_fall_01.mp4`，對應 `residents` 範本第一列。

---

## 4. Stage 2 — 事件結束報告（Fig 2 下半部）

### 4.1 Profile lookup 3
複製 3.6，改名 `Profile lookup 3`。把 Switch 的 **episode_closed** 和 **test** 兩個輸出都接到它。

### 4.2 Assemble
加 **Edit Fields (Set)**：Mode `Manual Mapping`；**Include Other Input Fields 關閉**；加兩個欄位：

| Name | Type | Value |
|---|---|---|
| body | Object | `{{ $('Webhook').first().json.body }}` |
| profile | Object | `{{ $json }}` |

之後的 `$json` 就同時有 `body`（事件）、`profile`（病人設定）。

### 4.3 Grading
加 **Code**：`Run Once for Each Item`，貼 `code/grading_code_node.js`。它讀 `$input.item.json.body`，加上 `$json.grading`（tier、flags、白話數字、fall_height_words…）。**你的分級標準之後就換這個檔案**。

### 4.4 Report（LLM）
加 **Basic LLM Chain**（在 AI 分類）：Prompt `Define below`；打開 **Chat Messages (if Using a Chat Model)** → Add Prompt → Type `System Message`，內容貼 `code/report_prompt_v2.md` 的第 1 段；下方 **Prompt (User Message)** 貼第 2 段。兩個欄位都要切到 **Expression** 模式（欄位右上角 Fixed／Expression），否則 `{{ }}` 不會展開。
節點底部 **Model** 接點按 + → `Anthropic Chat Model`（或 OpenAI／Gemini）→ 選憑證、選模型。
輸出在 `$json.text`（第一次執行後打開節點 OUTPUT 分頁確認）。

### 4.5 Report email
加 **Gmail**：Resource `Message`；Operation `Send`；

| 欄 | 表達式 |
|---|---|
| To | `{{ $('Assemble').first().json.profile.carer_email }}` |
| Subject | `{{ $json.text.startsWith('Subject:') ? $json.text.split('\n')[0].replace(/^Subject:\s*/, '') : 'FallGuard fall report — ' + $('Assemble').first().json.body.episode.episode_id }}` |
| Email Type | `Text` |
| Message | `{{ ($json.text.startsWith('Subject:') ? $json.text.split('\n').slice(2).join('\n') : $json.text) + '\n\n--- Please complete the post-fall check: ' + $execution.resumeFormUrl }}` |

`$execution.resumeFormUrl` 要等 Stage 3 加了 Wait 節點才有值；**Stage 2 測試時先把 Message 改成只有前半段**，Stage 3 再加回連結。

### 4.6 測試 Stage 2
`./test_payloads/curl_test.sh episode_closed`（Active 後用 Production URL 不必按 Listen）→ 照顧者信箱收到一封主旨以 `Subject:` 行為準的報告，內容四項：時間、倒地時長（含躺／半直立／不在畫面、靜止）、頭部風險與依據、跌落高度。點開 Grading 節點 OUTPUT 看 `tier`（範本 payload 是 HIGH）。
用 n8n 畫布上的 **Executions** 分頁看每次執行走了哪些節點、哪裡紅了。

---

## 5. Stage 3 — 人工確認與三條分支（Fig 3 上半部、Fig 6）

### 5.1 Caregiver form
在 Report email 之後加 **Wait**：Resume `On Form Submitted`；Form Title `FallGuard — post-fall check`；Form Fields 依 Fig 6 加七個欄位（前六個 Dropdown List，最後 Notes 是 Text；`ACD reviewed` 與 `Decision` 設 Required）。Options → **Limit Wait Time** 開，demo 設 30 分鐘。
現在回 4.5 把 Message 的表單連結加回去。

### 5.2 Switch decision
加 **Switch**：三條規則，左邊 `{{ $json.Decision }}`，右邊依序 `Call 000`、`Contact GP or virtual care`、`Observe at home`，Rename Output。
`$json.Decision` 這個鍵名是表單的 Field Label；第一次真的送出表單後，打開 Wait 節點 OUTPUT 確認鍵名再接。

### 5.3 分支 Call 000
1. **Code** `Build transfer pack`：貼 `code/transfer_pack.js`（輸出 `subject`、`text`）。
2. **Gmail** `Transfer pack email`：To `{{ $('Assemble').first().json.profile.carer_email }}`；Subject `{{ $json.subject }}`；Message `{{ $json.text }}`；Email Type Text。
3. **Gmail** `Supporter approval`：Operation `Send and Wait for Approval`；To = carer_email；Subject `Send the fall notice to {{ $('Assemble').first().json.profile.supporter_name }}?`；Message 放要寄給家屬的內容草稿（例如 `{{ $('Report').first().json.text }}` 的前幾行）；Type of Approval `Approve and Disapprove`。
4. **IF** `approved?`：Boolean → `{{ $json.data.approved }}` is true（鍵名第一次執行後在 OUTPUT 確認）。
5. true → **Gmail** `Supporter notice`：To `{{ $('Assemble').first().json.profile.supporter_email }}`，內容同上。

### 5.4 分支 Contact GP / virtual care
**Gmail** `GP summary`：To `{{ $('Assemble').first().json.profile.gp_email }}`；Subject `Fall — {{ $('Assemble').first().json.profile.resident_name }} — camera record`；Message `{{ $('Report').first().json.text }}` → **Wait** `Wait next business day`：After Time Interval，demo 2 分鐘 → **Telegram** `GP reminder`：Chat ID `{{ $('Assemble').first().json.profile.carer_telegram_chat_id }}`，Text `Reminder: confirm the GP has the fall summary for {{ $('Assemble').first().json.profile.resident_name }}.`

### 5.5 分支 Observe at home
**Telegram** `Obs reminder 1`：`Post-fall check now: alert? headache? vomiting? new pain? Reply in the outcome form if anything changes.` → **Wait** `Wait 1 hour`（demo 1 分鐘）→ **Telegram** `Obs reminder 2`（同文）。正式版依 CEC：前 4 小時每小時，之後每 4 小時到 24 小時。

### 5.6 Outcome message 與 Outcome form
三條分支的最後一個節點都接到 **Gmail** `Outcome message`：To carer_email；Subject `What happened after the fall?`；Message `Please record the outcome here: {{ $execution.resumeFormUrl }}` → **Wait** `Outcome form`：On Form Submitted；欄位 `Outcome`（Dropdown：`Stayed home`｜`ED then home`｜`Admitted`，Required）、`Hospital`（Text）、`Notes`（Text）；Limit Wait Time 開。

### 5.7 測試 Stage 3
送 `episode_closed` → 信箱收到報告，點連結填表選 **Call 000** → 收到轉院資料包信 → 收到核准信，按 **Approve** → 家屬信箱收到通知 → 收到結果表單信 → 選 **Admitted**。
再送一次，選 **Observe at home**，看兩則 Telegram 提醒間隔 1 分鐘。
同時做 0.1 的逾時測試：表單放 5 分鐘再送，執行仍應繼續。

---

## 6. Stage 4 — 住院通知、事件草稿、衛教單、追蹤（Fig 3 下半部）

1. **IF** `IF admitted`：`{{ $json.Outcome }}` is equal to `Admitted`。true → **Gmail** `Admission notice`（To supporter_email，內容：誰、哪家醫院、時間；不加任何「要不要探望」的自動化）。IF 的 true 與 false 都接到下一步。
2. **Code** `Incident draft`：貼 `code/incident_draft.js`（輸出 `subject`、`text`、`row`）。
3. **Google Sheets** `Save incident`：Append Row，Sheet `incidents`，Map Automatically 對不上時改手動，欄位對應 `{{ $json.row.<欄名> }}`。
4. **Gmail** `Incident draft email`：To carer_email；Subject `{{ $('Incident draft').first().json.subject }}`；Message `{{ $('Incident draft').first().json.text }}`。草稿裡 `[ ]` 的欄位就是需要人填的，SIRS 分類明寫「不自動化」。
5. **Google Sheets** `Education blocks`：Get Row(s)，Sheet `education_blocks`，**不加 Filter**（要全部列）。
6. **Code** `Select blocks`：Mode **Run Once for All Items**，貼 `code/select_education_blocks.js`。依頭部風險、抗凝血、倒地時間、是否住院、語言挑段落。
7. **Basic LLM Chain** `Compose sheet`：System／User prompt 貼 `code/education_compose_prompt.md`；接同一個 Chat model（可以再拉一個子節點）。
8. **Gmail** `RN / GP approval`：Send and Wait for Approval；To = 扮演 RN 的隊友信箱（demo 可用 carer_email）；Message `{{ $json.text }}`。
9. **IF** approved → **Gmail** `Send education sheet`：To supporter_email；Message `{{ $('Compose sheet').first().json.text }}`。
10. **Wait** `Wait 1 day`（demo 2 分鐘）→ **Telegram** `Follow-up`：若 `{{ $('Caregiver form').first().json.Decision }}` 是 Call 000，提醒用 `{{ $('Assemble').first().json.profile.concession_card_type }}` 申請救護車費用豁免；否則提醒回診。文字可用 Code 節點組，或直接在 Text 用三元運算子。

測試：整條跑完，`incidents` 多一列，家屬信箱收到經核准的衛教單，2 分鐘後 Telegram 收到追蹤提醒。

---

## 7. 對接 FallGuard 腳本（Fig 1）

1. 工作流程切 **Active**，從 Webhook 節點複製 **Production URL**。
2. 筆電上 `app_v9.py` 旁建 `.env`（照 `.env.example`）：
   ```
   N8N_WEBHOOK_URL=https://<team>.app.n8n.cloud/webhook/fallguard
   N8N_WEBHOOK_KEY=<密鑰>
   ```
3. `streamlit run app_v9.py` → 側欄 **n8n Pipeline**：勾 `Send events to n8n`；URL 與 Auth key 已由 `.env` 填入；`Include snapshots` 可勾（v1 流程不用截圖，但 payload 會帶）。
4. **Settings 頁 → Send test payload to n8n**：下方 Recent deliveries 應出現 `🟢 test … HTTP 200`；n8n 走 episode_closed 那條，照顧者收到報告信。
5. 上傳 demo 影片（檔名必須在 `residents` 表裡）→ 跑完後看 Recent deliveries 的順序：`fall_detected` → `fall_analysed` → （`escalation`）→ `episode_closed`，全部 200；Telegram 在 `fall_analysed` 時收到警示（跌倒後約 2.5 秒）。一支影片裡有幾次跌倒就有幾組事件、幾個 n8n 執行。
6. 影片在人還躺著時結束 → `episode_closed` 的 `status` 是 `unresolved`，分級是 HIGH，報告第一句會寫「錄影結束時人仍在地上」——這是 Le2i 片段的正常結果。

### 除錯對照

| 現象 | 原因 → 處理 |
|---|---|
| Recent deliveries `HTTP 403` | 兩邊密鑰不同，或 Webhook 節點沒選憑證 |
| `HTTP 404` | 工作流程不是 Active、或用了 Test URL 但沒按 Listen、或 Path 打錯 |
| `error: … timed out` | n8n Cloud 沒回應；Respond 是否設 Immediately |
| 200 但 Telegram 沒訊息 | `residents` 沒有這個 `source_name`；或 chat ID 的人沒按過 Start；或 Parse Mode 不是 HTML |
| Grading 或 Build 節點紅字 `Cannot read properties of undefined` | 前面節點改名了，`$('Webhook')`／`$('Assemble')` 找不到；或 Assemble 的 Include Other Input Fields 沒關 |
| 紅字提到 `paired item` | 表達式用了 `$('節點').item`，但 Google Sheets 之後配對資訊斷了；改成 `$('節點').first()`（每次執行只有一個事件，兩者結果相同；`code/*.js` 已全部用 `.first()`） |
| LLM 節點錯誤 | 憑證／模型名稱；或 prompt 欄位還是 Fixed 模式 |
| 信裡的表單連結是空的 | `$execution.resumeFormUrl` 只在同一執行裡有 Wait 節點時才有值；Wait 節點必須在寄信節點之後 |
| 核准信按鈕沒反應 | 用 Gmail 節點（不是 Send Email／SMTP）；信件用 HTML 顯示 |
| 表單送出後 Switch decision 不走 | 鍵名不是 `Decision`：看 Wait 節點 OUTPUT 的實際鍵名 |
| 等待中的執行被砍 | 試用版逾時；縮短 Limit Wait Time，或升級 |

---

## 8. Demo 腳本（3 分鐘）

1. 畫面左：Streamlit 播 Le2i 側拍片；畫面右：Telegram 與信箱。
2. 跌倒 → 2.5 秒後 Telegram 出現警示（講：偵測 + 姿態分析 + 誤報過濾）。
3. 影片結束 → 信箱收到報告（講：四項內容、只講量測到的事實、頭部「風險」不是「撞到」）。
4. 點表單 → 選 Call 000 → 轉院資料包 + ISBAR（講：對齊機構既有交班、不放識別碼、ACD 檢視在送醫前）。
5. 核准家屬通知（講：open disclosure 由人確認、收件人是 registered supporter）。
6. 結果表單選 Admitted → 家屬通知、事件草稿、經核准的衛教單（講：草稿不是通報、衛教是官方段落組合）。

---

## 附錄 A：表達式速查

| 要什麼 | 寫法 |
|---|---|
| Webhook 之後任一節點拿事件 | `$('Webhook').first().json.body.episode.time_on_ground_sec` |
| Assemble 之後拿病人設定 | `$('Assemble').first().json.profile.carer_email` |
| Grading 之後拿分級 | `$('Grading').first().json.grading.tier_label` |
| LLM 報告全文 | `$('Report').first().json.text` |
| 表單答案 | `$('Caregiver form').first().json.Decision`、`$('Outcome form').first().json.Outcome` |
| `.item` 與 `.first()` | 每次執行只處理一個事件，兩者相同；`.first()` 不依賴配對資訊，較不會出錯 |
| 核准結果 | `$json.data.approved`（緊接在 Send and Wait 之後） |
| 現在時間 | `$now.toISO()` |
| 表單連結 | `$execution.resumeFormUrl` |

## 附錄 B：節點名稱（必須一致）

Webhook · Event log · Switch event_type · IF false alarm · Profile lookup · Build alert · Telegram alert · Profile lookup 2 · Build escalation · Telegram escalation · Profile lookup 3 · Assemble · Grading · Report · Report email · Caregiver form · Switch decision · Build transfer pack · Transfer pack email · Supporter approval · Supporter notice · GP summary · Wait next business day · GP reminder · Obs reminder 1 · Wait 1 hour · Obs reminder 2 · Outcome message · Outcome form · IF admitted · Admission notice · Incident draft · Save incident · Incident draft email · Education blocks · Select blocks · Compose sheet · RN / GP approval · Send education sheet · Wait 1 day · Follow-up

## 附錄 C：v1 明確不做的事（簡報時說明）
不送出 SIRS；不接機構 IMS／My Health Record；不用 Telegram 傳機構住民資料（demo 例外）；不把截圖寄給家屬；訊息與報告內不出現 Medicare 號碼、IHI、保險會員號；衛教單沒有 RN／GP 核准不寄。
