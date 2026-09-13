#!/usr/bin/env python3
"""
Generate "FallGuard Flow 2.json" — the complete n8n workflow from
n8n_build_guide.md (Stages 1-4), with Telegram alerts that carry the fall
clip (video) or the fall frame (photo) instead of plain text.

Run:  python build_workflow.py          -> writes "FallGuard Flow 2.json"
Then in n8n: Workflows -> Create -> ... -> Import from File.

Node names follow Appendix B of the guide, except the Telegram alert section,
which becomes one shared media chain used by both Build alert and Build
escalation:

  Build alert ------\\                        true  Clip to file -> Telegram video
                      IF has clip -----------<
  Build escalation --/                        false IF has snapshot --true--> Snapshot to file -> Telegram photo
                                                                    \\-false--> Telegram text
"""
import json
import os
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_DIR = os.path.join(HERE, "code")
OUT = os.path.join(HERE, "FallGuard Flow 3.json")

# Credential names: n8n matches credentials by name on import. Rename yours to
# these (or pick them in each node after import).
# IDs are the credentials already created on hollyhsu.app.n8n.cloud (2026-09-13,
# via list_credentials on the instance MCP). On another instance leave id "".
CRED = {
    "httpHeaderAuth":       {"id": "4EuS3tmbFBnPmM8x", "name": "FallGuard key"},
    "telegramApi":          {"id": "89wGlQIFPckQF840", "name": "Telegram account"},
    "googleSheetsOAuth2Api": {"id": "x2dEvSLUIVHXIczV", "name": "Google Sheets account"},
    "gmailOAuth2":          {"id": "goKcKrkl9KKHEZsq", "name": "Gmail account"},
    # Anthropic: no own credential; n8n auto-assigns "Gateway credits" on that instance
}
ANTHROPIC_MODEL = "claude-opus-5"
SHEET_DOC_NAME = "Fall Response Report"
SHEET_DOC_ID = "1jwn3vxmVjdjMdBPQc2A77imNXHxz-uU8WzdFke6YiWU"   # the team's spreadsheet


def read(name):
    with open(os.path.join(CODE_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def prompt_blocks(md):
    """The two ``` blocks of a prompt .md file (system, user)."""
    parts = md.split("```")
    blocks = [parts[i] for i in range(1, len(parts), 2)]
    assert len(blocks) >= 2, "prompt file needs two code blocks"
    return blocks[0].strip("\n"), blocks[1].strip("\n")


def uid():
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# node builders
# ---------------------------------------------------------------------------
NODES = []
CONN = {}
X0, Y0, DX, DY = 0, 0, 260, 170


def node(name, ntype, version, params, col, row, creds=None, extra=None):
    n = {
        "id": uid(),
        "name": name,
        "type": ntype,
        "typeVersion": version,
        "position": [X0 + col * DX, Y0 + row * DY],
        "parameters": params,
    }
    if creds:
        n["credentials"] = {c: dict(CRED[c]) for c in creds if c in CRED}
    if extra:
        n.update(extra)
    assert all(x["name"] != name for x in NODES), "duplicate node name " + name
    NODES.append(n)
    return name


def link(src, dst, out=0, kind="main", dst_index=0):
    CONN.setdefault(src, {}).setdefault(kind, [])
    outs = CONN[src][kind]
    while len(outs) <= out:
        outs.append([])
    outs[out].append({"node": dst, "type": kind, "index": dst_index})


def cond_string(left, right, op="equals"):
    c = {"id": uid(), "leftValue": left, "rightValue": right,
         "operator": {"type": "string", "operation": op}}
    if op in ("empty", "notEmpty"):
        c["rightValue"] = ""
        c["operator"]["singleValue"] = True
    return c


def conditions(*conds):
    return {"options": {"caseSensitive": True, "leftValue": "",
                        "typeValidation": "strict", "version": 2},
            "conditions": list(conds), "combinator": "and"}


def sheets_read(name, sheet, col, row, filters=None):
    p = {"resource": "sheet", "operation": "read",
         "documentId": {"__rl": True, "mode": "list", "value": SHEET_DOC_ID,
                        "cachedResultName": SHEET_DOC_NAME},
         "sheetName": {"__rl": True, "mode": "name", "value": sheet},
         "options": {}}
    if filters:
        p["filtersUI"] = {"values": [{"lookupColumn": k, "lookupValue": v} for k, v in filters]}
        p["combineFilters"] = "AND"
    return node(name, "n8n-nodes-base.googleSheets", 4.5, p, col, row, ["googleSheetsOAuth2Api"])


def sheets_append(name, sheet, columns, col, row):
    p = {"resource": "sheet", "operation": "append",
         "documentId": {"__rl": True, "mode": "list", "value": SHEET_DOC_ID,
                        "cachedResultName": SHEET_DOC_NAME},
         "sheetName": {"__rl": True, "mode": "name", "value": sheet},
         "columns": {"mappingMode": "defineBelow", "value": dict(columns),
                     "matchingColumns": [],
                     "schema": [{"id": k, "displayName": k, "required": False,
                                 "defaultMatch": False, "display": True,
                                 "type": "string", "canBeUsedToMatch": True}
                                for k in columns]},
         "options": {}}
    return node(name, "n8n-nodes-base.googleSheets", 4.5, p, col, row, ["googleSheetsOAuth2Api"])


def each_item_return(js):
    """n8n's "Run Once for Each Item" mode must return ONE object ({ json: ... }),
    not an array. The guide's scripts end with `return [{ json: ... }];`, which
    n8n rejects with "A 'json' property isn't an object" — rewrite the final
    return statement."""
    i = js.rfind("return [{")
    if i < 0:
        return js
    head, tail = js[:i], js[i:]
    j = tail.rfind("}];")
    if j < 0:
        return js
    return head + "return ({" + tail[len("return [{"):j] + "});" + tail[j + 3:]


def code(name, js, col, row, each_item=True):
    p = {"jsCode": each_item_return(js) if each_item else js}
    if each_item:
        p["mode"] = "runOnceForEachItem"
    return node(name, "n8n-nodes-base.code", 2, p, col, row)


def if_node(name, cond, col, row):
    return node(name, "n8n-nodes-base.if", 2.2, {"conditions": conditions(cond), "options": {}}, col, row)


def telegram_text(name, chat_id, text, col, row):
    p = {"resource": "message", "operation": "sendMessage", "chatId": chat_id, "text": text,
         "additionalFields": {"appendAttribution": False, "parse_mode": "HTML"}}
    return node(name, "n8n-nodes-base.telegram", 1.2, p, col, row, ["telegramApi"],
                {"webhookId": uid()})


def telegram_media(name, operation, upstream, col, row):
    # "Convert to File" empties the item's json, so chat_id / text are read
    # from the IF node in front of it (one item per execution -> .first()).
    src = "$('%s').first().json" % upstream
    p = {"resource": "message", "operation": operation, "chatId": "={{ %s.chat_id }}" % src,
         "binaryData": True, "binaryPropertyName": "data",
         "additionalFields": {"caption": "={{ %s.text }}" % src, "parse_mode": "HTML"}}
    return node(name, "n8n-nodes-base.telegram", 1.2, p, col, row, ["telegramApi"],
                {"webhookId": uid()})


def to_file(name, source_prop, file_name, mime, col, row):
    p = {"operation": "toBinary", "sourceProperty": source_prop, "binaryPropertyName": "data",
         "options": {"fileName": file_name, "mimeType": mime}}
    return node(name, "n8n-nodes-base.convertToFile", 1.1, p, col, row)


def gmail_send(name, to, subject, message, col, row, html=False):
    p = {"sendTo": to, "subject": subject, "emailType": "html" if html else "text", "message": message,
         "options": {"appendAttribution": False}}
    return node(name, "n8n-nodes-base.gmail", 2.1, p, col, row, ["gmailOAuth2"], {"webhookId": uid()})


def gmail_approval(name, to, subject, message, col, row):
    p = {"operation": "sendAndWait", "sendTo": to, "subject": subject, "message": message,
         "responseType": "approval",
         "approvalOptions": {"values": {"approvalType": "double"}},
         "options": {}}
    return node(name, "n8n-nodes-base.gmail", 2.1, p, col, row, ["gmailOAuth2"], {"webhookId": uid()})


def wait_form(name, title, fields, col, row, minutes=30, description=""):
    values = []
    for label, ftype, options, required in fields:
        f = {"fieldLabel": label, "fieldType": ftype}
        if options:
            f["fieldOptions"] = {"values": [{"option": o} for o in options]}
        if required:
            f["requiredField"] = True
        values.append(f)
    p = {"resume": "form", "formTitle": title, "formDescription": description,
         "formFields": {"values": values},
         "limitWaitTime": True, "limitType": "afterTimeInterval",
         "resumeAmount": minutes, "resumeUnit": "minutes"}
    return node(name, "n8n-nodes-base.wait", 1.1, p, col, row, None, {"webhookId": uid()})


def wait_interval(name, amount, unit, col, row):
    p = {"resume": "timeInterval", "amount": amount, "unit": unit}
    return node(name, "n8n-nodes-base.wait", 1.1, p, col, row, None, {"webhookId": uid()})


def llm_text(name, system_msg, user_msg, col, row):
    """Anthropic node (resource text / message) — covered by n8n Gateway
    credits, unlike Basic LLM Chain + chat-model subnode ("nodeNotCovered")."""
    p = {"resource": "text", "operation": "message",
         "modelId": {"__rl": True, "mode": "id", "value": ANTHROPIC_MODEL},
         "messages": {"values": [{"content": "=" + user_msg, "role": "user"}]},
         "simplify": True,
         "options": {"system": "=" + system_msg, "includeMergedResponse": True}}
    return node(name, "@n8n/n8n-nodes-langchain.anthropic", 1, p, col, row)


# merged_response = all text blocks joined (options.includeMergedResponse);
# arrow functions are rejected by n8n's expression parser, keep it simple.
# .replaceAll('**', ''): the model still emits markdown bold; the mail is plain text.
LLM_TEXT_EXPR = "={{ $json.merged_response || $json.content.last().text || '' }}"


def llm_text_out(name, col, row):
    """Set node that exposes the model's answer as $json.text (the name the
    rest of the workflow reads via $('Report') / $('Compose sheet'))."""
    p = {"mode": "manual", "includeOtherFields": False,
         "assignments": {"assignments": [{"id": uid(), "name": "text", "value": LLM_TEXT_EXPR, "type": "string"}]},
         "options": {}}
    return node(name, "n8n-nodes-base.set", 3.4, p, col, row)


# ---------------------------------------------------------------------------
# Telegram media: the modified Build alert / Build escalation scripts
# ---------------------------------------------------------------------------
MEDIA_TAIL = """
// ---- v10: media for Telegram -------------------------------------------
// The app adds body.clip.mp4_base64 (short H.264 clip around the fall) and
// body.snapshots.fall_frame_jpeg_base64 when those options are ticked.
// The chain after this node sends video > photo > plain text, whichever exists.
const clipB64  = (body.clip && body.clip.mp4_base64) || null;
const photoB64 = (body.snapshots && body.snapshots.fall_frame_jpeg_base64) || null;
const clipNote = body.clip
  ? `\\nClip: ${body.clip.pre_fall_sec != null ? body.clip.pre_fall_sec + ' s before' : ''}` +
    `${body.clip.post_fall_sec != null ? ' to ' + body.clip.post_fall_sec + ' s after the fall' : ''}`
  : '';
"""


def escape_html_js():
    return """
// Telegram parse mode is HTML: escape the three characters that break it.
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
"""


def build_alert_js():
    src = read("build_alert.js")
    src = src.replace("return { json: { chat_id: row.carer_telegram_chat_id, text } };", "")
    return (src.rstrip() + "\n" + escape_html_js() + MEDIA_TAIL + """
return [{ json: {
  chat_id: row.carer_telegram_chat_id,
  text: esc(text + clipNote).slice(0, 1000),          // caption limit is 1024 chars
  clip_b64: clipB64,
  photo_b64: photoB64,
} }];
""")


def build_escalation_js():
    src = read("build_escalation.js")
    src = src.replace("return { json: { chat_id: row.carer_telegram_chat_id, text } };", "")
    return (src.rstrip() + "\n" + escape_html_js() + MEDIA_TAIL + """
return [{ json: {
  chat_id: row.carer_telegram_chat_id,
  text: esc(text + clipNote).slice(0, 1000),
  clip_b64: clipB64,
  photo_b64: photoB64,
} }];
""")



PICK_PROFILE_JS = """// Match the camera / file name to a residents row; fall back to the row named
// 'default', then to the first row, so a demo video never gets lost because
// its file name is missing from the sheet. profile_match says which one hit.
const src = String($('Webhook').first().json.body.source.name || '').trim().toLowerCase();
const rows = $input.all().map(i => i.json);
const norm = v => String(v || '').trim().toLowerCase();
const row = rows.find(r => norm(r.source_name) === src)
         || rows.find(r => norm(r.source_name) === 'default')
         || rows[0];
if (!row) return [];
return [{ json: { ...row, profile_match: norm(row.source_name) === src ? 'exact' : 'fallback' } }];
"""


def profile_lookup(lookup_name, pick_name, col, row):
    """Google Sheets read of ALL residents rows + a Code node that picks one."""
    sheets_read(lookup_name, "residents", col, row)
    code(pick_name, PICK_PROFILE_JS, col + 0.5, row, each_item=False)
    link(lookup_name, pick_name)
    return pick_name

# ---------------------------------------------------------------------------
# Stage 1 — intake, log, immediate alerts
# ---------------------------------------------------------------------------
node("Webhook", "n8n-nodes-base.webhook", 2.1,
     {"httpMethod": "POST", "path": "fallguard", "authentication": "headerAuth",
      "responseMode": "onReceived", "options": {}},
     0, 2, ["httpHeaderAuth"], {"webhookId": uid()})

sheets_append("Event log", "event_log", {
    "received_at": "={{ $now.toISO() }}",
    "event_type": "={{ $json.body.event_type }}",
    "episode_id": "={{ $json.body.episode.episode_id }}",
    "source_name": "={{ $json.body.source.name }}",
    "status": "={{ $json.body.episode.status }}",
    "time_on_ground_sec": "={{ $json.body.episode.time_on_ground_sec }}",
    "head_risk": "={{ $json.body.episode.event_features.head_impact?.risk ?? '' }}",
    "fall_confidence": "={{ $json.body.episode.event_features.fall_confidence ?? '' }}",
    "tier": "",
    "episode_json": "={{ JSON.stringify($json.body.episode) }}",
}, 1, 0)
link("Webhook", "Event log")

EVENT_TYPES = ["fall_detected", "fall_analysed", "escalation", "episode_closed", "test"]
node("Switch event_type", "n8n-nodes-base.switch", 3.2,
     {"rules": {"values": [
         {"conditions": conditions(cond_string("={{ $json.body.event_type }}", et)),
          "renameOutput": True, "outputKey": et} for et in EVENT_TYPES]},
      "options": {}},
     1, 2)
link("Webhook", "Switch event_type")
OUT_IDX = {et: i for i, et in enumerate(EVENT_TYPES)}

# fall_detected (output 0): nothing — the alert goes out on fall_analysed

# fall_analysed -> IF false alarm -> Profile lookup -> Build alert
if_node("IF false alarm",
        cond_string("={{ $json.body.episode.event_features.fall_confidence }}", "likely_false_alarm"),
        2, 1)
link("Switch event_type", "IF false alarm", OUT_IDX["fall_analysed"])
profile_lookup("Profile lookup", "Pick profile", 3, 1)
link("IF false alarm", "Profile lookup", 1)          # false output = a real fall
code("Build alert", build_alert_js(), 4, 1)
link("Pick profile", "Build alert")

# escalation -> Profile lookup 2 -> Build escalation
profile_lookup("Profile lookup 2", "Pick profile 2", 3, 2)
link("Switch event_type", "Profile lookup 2", OUT_IDX["escalation"])
code("Build escalation", build_escalation_js(), 4, 2)
link("Pick profile 2", "Build escalation")

# shared media chain: video > photo > text
if_node("IF has clip", cond_string("={{ $json.clip_b64 }}", "", "notEmpty"), 5, 1)
link("Build alert", "IF has clip")
link("Build escalation", "IF has clip")
to_file("Clip to file", "clip_b64", "fall_clip.mp4", "video/mp4", 6, 0)
link("IF has clip", "Clip to file", 0)
telegram_media("Telegram video", "sendVideo", "IF has clip", 7, 0)
link("Clip to file", "Telegram video")
if_node("IF has snapshot", cond_string("={{ $json.photo_b64 }}", "", "notEmpty"), 6, 2)
link("IF has clip", "IF has snapshot", 1)
to_file("Snapshot to file", "photo_b64", "fall_frame.jpg", "image/jpeg", 7, 1)
link("IF has snapshot", "Snapshot to file", 0)
telegram_media("Telegram photo", "sendPhoto", "IF has snapshot", 8, 1)
link("Snapshot to file", "Telegram photo")
telegram_text("Telegram text", "={{ $json.chat_id }}", "={{ $json.text }}", 7, 2)
link("IF has snapshot", "Telegram text", 1)

# ---------------------------------------------------------------------------
# Stage 2 — episode report
# ---------------------------------------------------------------------------
profile_lookup("Profile lookup 3", "Pick profile 3", 2, 4)
link("Switch event_type", "Profile lookup 3", OUT_IDX["episode_closed"])
link("Switch event_type", "Profile lookup 3", OUT_IDX["test"])

node("Assemble", "n8n-nodes-base.set", 3.4,
     {"mode": "manual", "includeOtherFields": False,
      "assignments": {"assignments": [
          {"id": uid(), "name": "body", "value": "={{ $('Webhook').first().json.body }}", "type": "object"},
          {"id": uid(), "name": "profile", "value": "={{ $json }}", "type": "object"}]},
      "options": {}},
     3, 4)
link("Pick profile 3", "Assemble")

code("Grading", read("grading_code_node.js"), 4, 4)
link("Assemble", "Grading")

sys_msg, user_msg = prompt_blocks(read("report_prompt_v2.md"))
llm_text("Report LLM", sys_msg, user_msg, 5, 4)
link("Grading", "Report LLM")
llm_text_out("Report", 5, 5)
link("Report LLM", "Report")

P = "$('Assemble').first().json.profile"
EP = "$('Assemble').first().json.body.episode"
G = "$('Grading').first().json.grading"
REPORT_TEXT = "$('Report').first().json.text"
REPORT_BODY_ONLY = ("(%s.startsWith('Subject:') ? %s.split('\\n').slice(2).join('\\n') : %s)"
                    % (REPORT_TEXT, REPORT_TEXT, REPORT_TEXT))

# ---------------------------------------------------------------------------
# Decisions happen in the FallGuard app ("Actions" page), not in email:
#   Queue <step>  -> append a row to the pending_actions sheet (with the
#                    execution's resume URL)
#   Wait <step>   -> Wait node, resume on webhook; the app POSTs the answer
#   Done <step>   -> mark the row done
#   <adapter>     -> Code node that exposes the answer under the field names
#                    the rest of the workflow already reads
# Emails are notifications only: what happened + what to do in the app.
# ---------------------------------------------------------------------------
APP_STEP = "Open the FallGuard app -> Actions"


def queue_action(name, step, detail, col, row):
    return sheets_append(name, "pending_actions", {
        "created_at": "={{ $now.toISO() }}",
        "episode_id": "={{ %s.episode_id }}" % EP,
        "resident": "={{ %s.resident_name }}" % P,
        "step": step,
        "detail": detail,
        # '#<step>' makes the value unique per row (four rows share one execution);
        # a URL fragment is never sent to the server, so the app can POST it as is.
        "resume_url": "={{ $execution.resumeUrl }}#" + step,
        "status": "open",
    }, col, row)


def wait_app(name, col, row):
    p = {"resume": "webhook", "httpMethod": "POST", "options": {},
         "limitWaitTime": True, "limitType": "afterTimeInterval",
         "resumeAmount": 2, "resumeUnit": "hours"}
    return node(name, "n8n-nodes-base.wait", 1.1, p, col, row, None, {"webhookId": uid()})


def mark_done(name, step, col, row):
    cols = {"resume_url": "={{ $execution.resumeUrl }}#" + step, "status": "done"}
    p = {"resource": "sheet", "operation": "appendOrUpdate",
         "documentId": {"__rl": True, "mode": "list", "value": SHEET_DOC_ID,
                        "cachedResultName": SHEET_DOC_NAME},
         "sheetName": {"__rl": True, "mode": "name", "value": "pending_actions"},
         "columns": {"mappingMode": "defineBelow", "value": cols,
                     "matchingColumns": ["resume_url"],
                     "schema": [{"id": k, "displayName": k, "required": False,
                                 "defaultMatch": k == "resume_url", "display": True,
                                 "type": "string", "canBeUsedToMatch": True}
                                for k in ("created_at", "episode_id", "resident", "step",
                                          "detail", "resume_url", "status")]},
         "options": {}}
    return node(name, "n8n-nodes-base.googleSheets", 4.5, p, col, row, ["googleSheetsOAuth2Api"])


def app_answer(name, wait_node, js_return, col, row):
    js = ("// Answer POSTed by the FallGuard app (Actions page) to the Wait node's resume URL.\n"
          "const b = $('%s').first().json.body || {};\n%s\n" % (wait_node, js_return))
    return code(name, js, col, row)


APPROVED_JS = "return { json: { data: { approved: b.approved === true || String(b.approved).toLowerCase() === 'true', decided_by: b.decided_by || '', decided_at: b.decided_at || '' } } };"


# ---------------------------------------------------------------------------
# HTML email template (inline CSS; renders in Gmail / Outlook / phones)
# ---------------------------------------------------------------------------
def html_mail(step, title, note, body_expr, kind="action"):
    """step: badge text ('Step 1 of 4' / 'Information'); title: heading;
    note: plain text for the coloured box (may contain {{ }} expressions);
    body_expr: an n8n JS expression (no braces) that yields the body text.
    **bold** in the model's text becomes <b>; newlines are kept (pre-wrap)."""
    if kind == "action":
        label, color, bg = "ACTION NEEDED", "#c92a2a", "#fff5f5"
    else:
        label, color, bg = "FOR YOUR INFORMATION", "#1864ab", "#eef4ff"
    body = "{{ (%s).replace(/\\*\\*(.+?)\\*\\*/g, '<b>$1</b>') }}" % body_expr
    return (
        '=<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f2f4f7;'
        'font-family:Arial,Helvetica,sans-serif">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f2f4f7">'
        '<tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="640" cellspacing="0" cellpadding="0" '
        'style="max-width:640px;width:100%;background:#ffffff;border:1px solid #e3e6ea;border-radius:8px">'
        '<tr><td style="background:#0f3d3e;color:#ffffff;padding:16px 24px;border-radius:8px 8px 0 0">'
        '<span style="font-size:20px;font-weight:700;letter-spacing:.02em">FallGuard</span>'
        '<span style="float:right;font-size:12px;padding:4px 10px;border:1px solid rgba(255,255,255,.5);'
        'border-radius:12px">' + step + '</span></td></tr>'
        '<tr><td style="padding:22px 24px 8px 24px">'
        '<h1 style="margin:0;font-size:21px;line-height:1.3;color:#111827">' + title + '</h1></td></tr>'
        '<tr><td style="padding:0 24px">'
        '<div style="padding:14px 16px;border-left:5px solid ' + color + ';background:' + bg + ';border-radius:4px">'
        '<div style="font-size:11px;font-weight:700;letter-spacing:.1em;color:' + color + '">' + label + '</div>'
        '<div style="margin-top:6px;font-size:15px;line-height:1.5;color:#1f2937">' + note + '</div></div></td></tr>'
        '<tr><td style="padding:18px 24px 24px 24px">'
        '<div style="white-space:pre-wrap;font-size:14px;line-height:1.6;color:#374151">' + body + '</div></td></tr>'
        '<tr><td style="padding:12px 24px;background:#f7f8fa;border-radius:0 0 8px 8px;font-size:11px;'
        'line-height:1.5;color:#6b7280">Sent automatically by FallGuard. The report records what the camera '
        'measured; it is not a diagnosis and never replaces a clinical assessment. Do not reply to this email.'
        '</td></tr></table></td></tr></table></body></html>')


# ---- Report email + post-fall check ----------------------------------------
REPORT_SUBJECT = "=[FallGuard 1/4] Fall report - {{ %s.resident_name }} - action needed: post-fall check" % P
REPORT_BODY = ("=ACTION NEEDED (carer): check on {{ %s.resident_name }} now. Then %s -> 'Post-fall check' "
               "and answer six questions (ACD reviewed, injury seen, alert, pain, can stand, decision). "
               "Your decision - Call 000 / Contact GP or virtual care / Observe at home - sets the next steps. "
               "Nothing goes to the hospital or the family until you decide.\n\n"
               "--- Fall report (written from the camera record; facts only, no diagnosis) ---\n"
               "{{ %s }}") % (P, APP_STEP, REPORT_BODY_ONLY.replace("$('Report').first().json.text", "$json.text"))
gmail_send("Report email", "={{ %s.carer_email }}" % P,
           "=FallGuard · Step 1 of 4 · Fall report for {{ %s.resident_name }} — action needed" % P,
           html_mail("Step 1 of 4", "Fall detected — {{ %s.resident_name }}" % P,
                     "Check on {{ %s.resident_name }} now. Then open the <b>FallGuard app → Actions</b> and complete the "
                     "<b>Post-fall check</b> (six questions). Your decision — <b>Call 000</b>, <b>Contact GP or virtual care</b> "
                     "or <b>Observe at home</b> — sets the next steps. Nothing goes to the hospital or the family until you decide." % P,
                     "$json.text.startsWith('Subject:') ? $json.text.split('\\n').slice(2).join('\\n') : $json.text"),
           6, 4, html=True)
link("Report", "Report email")

queue_action("Queue post-fall check", "post_fall_check",
             ("=Fall at {{ %s.fall_time }} ({{ $('Assemble').first().json.body.source.name }}). "
              "Time on the floor {{ %s.summary_numbers.time_on_ground }}, head-impact risk {{ %s.head_risk }}, "
              "severity {{ %s.tier_label }}. Outcome so far: {{ %s.status }}. Full report was emailed to the carer.")
             % (EP, G, G, G, EP), 7, 4)
link("Report email", "Queue post-fall check")
wait_app("Wait post-fall check", 8, 4)
link("Queue post-fall check", "Wait post-fall check")
mark_done("Done post-fall check", "post_fall_check", 9, 4)
link("Wait post-fall check", "Done post-fall check")
app_answer("Caregiver form", "Wait post-fall check", "return { json: b };", 10, 4)
link("Done post-fall check", "Caregiver form")

DECISIONS = ["Call 000", "Contact GP or virtual care", "Observe at home"]
node("Switch decision", "n8n-nodes-base.switch", 3.2,
     {"rules": {"values": [
         {"conditions": conditions(cond_string("={{ $json.Decision }}", d)),
          "renameOutput": True, "outputKey": d} for d in DECISIONS]},
      "options": {}},
     11, 4)
link("Caregiver form", "Switch decision")

# --- Call 000 --------------------------------------------------------------
code("Build transfer pack", read("transfer_pack.js"), 12, 3)
link("Switch decision", "Build transfer pack", 0)
gmail_send("Transfer pack email", "={{ %s.carer_email }}" % P,
           "=FallGuard · Transfer pack and ISBAR handover for {{ %s.resident_name }}" % P,
           html_mail("Call 000", "Transfer pack — {{ %s.resident_name }}" % P,
                     "You chose <b>Call 000</b>. Below are the transfer checklist and the ISBAR handover for the paramedics "
                     "and the emergency department. Keep it on your phone or print it. No app action for this email.",
                     "$json.text", kind="info"),
           13, 3, html=True)
link("Build transfer pack", "Transfer pack email")
gmail_send("Supporter approval email", "={{ %s.carer_email }}" % P,
           "=FallGuard · Step 2 of 4 · Approve the family notice for {{ %s.supporter_name }} — action needed" % P,
           html_mail("Step 2 of 4", "Approve the notice to {{ %s.supporter_name }}" % P,
                     "Before the fall notice is sent to <b>{{ %s.supporter_name }}</b> ({{ %s.supporter_email }}), a person must approve it. "
                     "Open the <b>FallGuard app → Actions → Approve family notice</b> and press Approve or Decline. "
                     "The draft that would be sent is below." % (P, P),
                     REPORT_BODY_ONLY),
           14, 3, html=True)
link("Transfer pack email", "Supporter approval email")
queue_action("Queue supporter approval", "supporter_notice_approval",
             "=Draft notice for {{ %s.supporter_name }} ({{ %s.supporter_email }}):\n\n{{ %s }}" % (P, P, REPORT_BODY_ONLY), 15, 3)
link("Supporter approval email", "Queue supporter approval")
wait_app("Wait supporter approval", 16, 3)
link("Queue supporter approval", "Wait supporter approval")
mark_done("Done supporter approval", "supporter_notice_approval", 17, 3)
link("Wait supporter approval", "Done supporter approval")
app_answer("Supporter approval", "Wait supporter approval", APPROVED_JS, 18, 3)
link("Done supporter approval", "Supporter approval")
node("IF approved?", "n8n-nodes-base.if", 2.2,
     {"conditions": conditions({"id": uid(), "leftValue": "={{ $json.data.approved }}", "rightValue": "",
                                "operator": {"type": "boolean", "operation": "true", "singleValue": True}}),
      "options": {}},
     19, 3)
link("Supporter approval", "IF approved?")
gmail_send("Supporter notice", "={{ %s.supporter_email }}" % P,
           "=FallGuard · Fall notice for {{ %s.resident_name }}" % P,
           html_mail("Family notice", "Fall notice — {{ %s.resident_name }}" % P,
                     "Dear {{ %s.supporter_name }}, this notice comes from the carer of <b>{{ %s.resident_name }}</b> about a fall recorded by the "
                     "FallGuard camera system. The carer has reviewed and approved it. Decision taken: "
                     "<b>{{ $('Caregiver form').first().json.Decision }}</b>. Please contact the carer for more information." % (P, P),
                     REPORT_BODY_ONLY, kind="info"),
           20, 3, html=True)
link("IF approved?", "Supporter notice", 0)

# --- Contact GP / virtual care ----------------------------------------------
gmail_send("GP summary", "={{ %s.gp_email }}" % P,
           "=FallGuard · Fall — {{ %s.resident_name }} — camera record for GP review" % P,
           html_mail("GP summary", "Fall — {{ %s.resident_name }}" % P,
                     "Dr {{ %s.gp_name }}, the carer of <b>{{ %s.resident_name }}</b> has chosen to contact you after a fall recorded by the "
                     "FallGuard camera system. The report below records what the camera measured; it is not a diagnosis." % (P, P),
                     REPORT_TEXT, kind="info"),
           12, 4, html=True)
link("Switch decision", "GP summary", 1)
wait_interval("Wait next business day", 2, "minutes", 13, 4)
link("GP summary", "Wait next business day")
telegram_text("GP reminder", "={{ %s.carer_telegram_chat_id }}" % P,
              "=Reminder: confirm the GP has the fall summary for {{ %s.resident_name }}." % P, 14, 4)
link("Wait next business day", "GP reminder")

# --- Observe at home ----------------------------------------------------------
OBS_TEXT = ("=Post-fall check for {{ %s.resident_name }} now: alert? headache? vomiting? new pain? "
            "If anything changes, record it in the app (Actions -> Record outcome) or call 000.") % P
telegram_text("Obs reminder 1", "={{ %s.carer_telegram_chat_id }}" % P, OBS_TEXT, 12, 5)
link("Switch decision", "Obs reminder 1", 2)
wait_interval("Wait 1 hour", 1, "minutes", 13, 5)
link("Obs reminder 1", "Wait 1 hour")
telegram_text("Obs reminder 2", "={{ %s.carer_telegram_chat_id }}" % P, OBS_TEXT, 14, 5)
link("Wait 1 hour", "Obs reminder 2")

# --- Outcome ----------------------------------------------------------------------
gmail_send("Outcome message", "={{ %s.carer_email }}" % P,
           "=FallGuard · Step 3 of 4 · Record the outcome for {{ %s.resident_name }} — action needed" % P,
           html_mail("Step 3 of 4", "How did it end for {{ %s.resident_name }}?" % P,
                     "The immediate steps are complete. Once things have settled, open the <b>FallGuard app → Actions → Record outcome</b> "
                     "and choose <b>Stayed home</b>, <b>ED then home</b> or <b>Admitted</b> (hospital and notes optional). "
                     "This closes the episode: it fills in the incident draft, selects the after-fall information sheet and, "
                     "if admitted, notifies {{ %s.supporter_name }}." % P,
                     "'Your decision at the post-fall check: ' + $('Caregiver form').first().json.Decision + '.\\nFall time: ' + %s.fall_time" % EP),
           21, 4, html=True)
link("Supporter notice", "Outcome message")
link("IF approved?", "Outcome message", 1)
link("GP reminder", "Outcome message")
link("Obs reminder 2", "Outcome message")
queue_action("Queue outcome", "outcome",
             "=Decision was '{{ $('Caregiver form').first().json.Decision }}'. Record how it ended: stayed home, ED then home, or admitted (hospital name and notes optional).",
             22, 4)
link("Outcome message", "Queue outcome")
wait_app("Wait outcome", 23, 4)
link("Queue outcome", "Wait outcome")
mark_done("Done outcome", "outcome", 24, 4)
link("Wait outcome", "Done outcome")
app_answer("Outcome form", "Wait outcome", "return { json: b };", 25, 4)
link("Done outcome", "Outcome form")

# ---------------------------------------------------------------------------
# Stage 4 — admission notice, incident draft, education sheet, follow-up
# ---------------------------------------------------------------------------
if_node("IF admitted", cond_string("={{ $json.Outcome }}", "Admitted"), 26, 4)
link("Outcome form", "IF admitted")
gmail_send("Admission notice", "={{ %s.supporter_email }}" % P,
           "=FallGuard · Hospital admission — {{ %s.resident_name }}" % P,
           html_mail("Family notice", "Hospital admission — {{ %s.resident_name }}" % P,
                     "Dear {{ %s.supporter_name }}, <b>{{ %s.resident_name }}</b> was admitted to hospital after a fall. "
                     "This is a notification only; please contact the carer for further information." % (P, P),
                     "'Hospital: ' + ($('Outcome form').first().json.Hospital || 'not recorded') + '\\nFall time: ' + %s.fall_time"
                     " + '\\nNotes from the carer: ' + ($('Outcome form').first().json.Notes || '-')" % EP, kind="info"),
           27, 3, html=True)
link("IF admitted", "Admission notice", 0)
code("Incident draft", read("incident_draft.js"), 28, 4)
link("Admission notice", "Incident draft")
link("IF admitted", "Incident draft", 1)

INCIDENT_COLS = ["episode_id", "resident", "fall_time", "time_on_ground_sec", "head_risk", "fall_height",
                 "fall_confidence", "decision", "outcome", "tier", "draft_created_at"]
sheets_append("Save incident", "incidents", {c: "={{ $json.row.%s }}" % c for c in INCIDENT_COLS}, 29, 4)
link("Incident draft", "Save incident")
gmail_send("Incident draft email", "={{ %s.carer_email }}" % P,
           "=FallGuard · Incident report DRAFT — {{ %s.resident_name }} (not submitted)" % P,
           html_mail("Incident draft", "Incident report draft — {{ %s.resident_name }}" % P,
                     "Pre-filled from the camera record and your answers. Fields marked <b>[ ]</b> need a person "
                     "(injuries found by a clinician, cause, actions taken, SIRS category). Copy it into your incident "
                     "management system — FallGuard never submits it. No app action for this email.",
                     "$('Incident draft').first().json.text", kind="info"),
           30, 4, html=True)
link("Save incident", "Incident draft email")

sheets_read("Education blocks", "education_blocks", 31, 4)
link("Incident draft email", "Education blocks")
code("Select blocks", read("select_education_blocks.js"), 32, 4, each_item=False)
link("Education blocks", "Select blocks")
sys2, user2 = prompt_blocks(read("education_compose_prompt.md"))
llm_text("Compose LLM", sys2, user2, 33, 4)
link("Select blocks", "Compose LLM")
llm_text_out("Compose sheet", 33, 5)
link("Compose LLM", "Compose sheet")

gmail_send("RN / GP approval email", "={{ %s.carer_email }}" % P,
           "=FallGuard · Step 4 of 4 · Nurse/GP approval of the information sheet for {{ %s.resident_name }} — action needed" % P,
           html_mail("Step 4 of 4", "Approve the after-fall information sheet" % (),
                     "The sheet below was assembled only from approved text blocks chosen for this fall (head-impact risk, "
                     "blood-thinning medication, time on the floor, admission, language). It goes to <b>{{ %s.supporter_name }}</b> "
                     "only after a nurse or GP approves it: open the <b>FallGuard app → Actions → Approve information sheet</b>." % P,
                     "$json.text"),
           34, 4, html=True)
link("Compose sheet", "RN / GP approval email")
queue_action("Queue sheet approval", "education_sheet_approval",
             "=Information sheet for {{ %s.supporter_name }} (assembled from approved blocks):\n\n{{ $('Compose sheet').first().json.text }}" % P, 35, 4)
link("RN / GP approval email", "Queue sheet approval")
wait_app("Wait sheet approval", 36, 4)
link("Queue sheet approval", "Wait sheet approval")
mark_done("Done sheet approval", "education_sheet_approval", 37, 4)
link("Wait sheet approval", "Done sheet approval")
app_answer("RN / GP approval", "Wait sheet approval", APPROVED_JS, 38, 4)
link("Done sheet approval", "RN / GP approval")
node("IF RN approved", "n8n-nodes-base.if", 2.2,
     {"conditions": conditions({"id": uid(), "leftValue": "={{ $json.data.approved }}", "rightValue": "",
                                "operator": {"type": "boolean", "operation": "true", "singleValue": True}}),
      "options": {}},
     39, 4)
link("RN / GP approval", "IF RN approved")
gmail_send("Send education sheet", "={{ %s.supporter_email }}" % P,
           "=FallGuard · After a fall — information for the carer of {{ %s.resident_name }}" % P,
           html_mail("Information sheet", "After a fall — {{ %s.resident_name }}" % P,
                     "Dear {{ %s.supporter_name }}, the information below was reviewed and approved by a nurse or GP. "
                     "Please read it together with any discharge papers, not instead of them." % P,
                     "$('Compose sheet').first().json.text", kind="info"),
           40, 3, html=True)
link("IF RN approved", "Send education sheet", 0)
wait_interval("Wait 1 day", 2, "minutes", 41, 4)
link("Send education sheet", "Wait 1 day")
link("IF RN approved", "Wait 1 day", 1)
telegram_text("Follow-up", "={{ %s.carer_telegram_chat_id }}" % P,
              ("={{ $('Caregiver form').first().json.Decision === 'Call 000' "
               "? 'Follow-up: if an ambulance attended, apply for the ambulance fee exemption with the ' "
               "+ (%s.concession_card_type || 'concession card') + ' for ' + %s.resident_name + '.' "
               ": 'Follow-up: please confirm the review appointment for ' + %s.resident_name "
               "+ ' after the fall, and check for new pain, confusion or unsteadiness.' }}") % (P, P, P),
              42, 4)
link("Wait 1 day", "Follow-up")



# ---------------------------------------------------------------------------
# "FallGuard pending actions" — tiny API the app polls:
#   GET /webhook/fallguard-pending  (header X-FallGuard-Key)  -> open rows
# ---------------------------------------------------------------------------
def build_pending_api():
    wh = {"id": uid(), "name": "Pending webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2.1,
          "position": [0, 0], "webhookId": uid(),
          "parameters": {"httpMethod": "GET", "path": "fallguard-pending", "authentication": "headerAuth",
                         "responseMode": "lastNode", "responseData": "allEntries", "options": {}},
          "credentials": {"httpHeaderAuth": dict(CRED["httpHeaderAuth"])}}
    rd = {"id": uid(), "name": "Open actions", "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
          "position": [260, 0],
          "parameters": {"resource": "sheet", "operation": "read",
                         "documentId": {"__rl": True, "mode": "list", "value": SHEET_DOC_ID, "cachedResultName": SHEET_DOC_NAME},
                         "sheetName": {"__rl": True, "mode": "name", "value": "pending_actions"},
                         "filtersUI": {"values": [{"lookupColumn": "status", "lookupValue": "open"}]},
                         "combineFilters": "AND", "options": {}},
          "credentials": {"googleSheetsOAuth2Api": dict(CRED["googleSheetsOAuth2Api"])}}
    # second endpoint: GET /webhook/fallguard-profile?source=<file or webcam>
    # -> the residents row that would be used (same fallback rule as the flow)
    pw = {"id": uid(), "name": "Profile webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2.1,
          "position": [0, 200], "webhookId": uid(),
          "parameters": {"httpMethod": "GET", "path": "fallguard-profile", "authentication": "headerAuth",
                         "responseMode": "lastNode", "responseData": "firstEntryJson", "options": {}},
          "credentials": {"httpHeaderAuth": dict(CRED["httpHeaderAuth"])}}
    rs = {"id": uid(), "name": "Residents", "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.5,
          "position": [260, 200],
          "parameters": {"resource": "sheet", "operation": "read",
                         "documentId": {"__rl": True, "mode": "list", "value": SHEET_DOC_ID, "cachedResultName": SHEET_DOC_NAME},
                         "sheetName": {"__rl": True, "mode": "name", "value": "residents"}, "options": {}},
          "credentials": {"googleSheetsOAuth2Api": dict(CRED["googleSheetsOAuth2Api"])}}
    pick = {"id": uid(), "name": "Pick resident", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [520, 200],
            "parameters": {"jsCode": PICK_PROFILE_JS.replace("$('Webhook').first().json.body.source.name",
                                                             "$('Profile webhook').first().json.query.source")
                           .replace("if (!row) return [];", "if (!row) return [{ json: { error: 'residents sheet is empty' } }];")}}
    return {"name": "FallGuard pending actions", "nodes": [wh, rd, pw, rs, pick],
            "connections": {"Pending webhook": {"main": [[{"node": "Open actions", "type": "main", "index": 0}]]},
                            "Profile webhook": {"main": [[{"node": "Residents", "type": "main", "index": 0}]]},
                            "Residents": {"main": [[{"node": "Pick resident", "type": "main", "index": 0}]]}},
            "settings": {"executionOrder": "v1"}, "pinData": {}}


PENDING_API = build_pending_api()
OUT_API = os.path.join(HERE, "FallGuard pending actions.json")

# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------
workflow = {
    "name": "FallGuard Flow 3",
    "nodes": NODES,
    "connections": CONN,
    "settings": {"executionOrder": "v1"},
    "pinData": {},
    "meta": {"instanceId": "fallguard-build-script"},
}

# sanity: every connection target exists
names = {n["name"] for n in NODES}
for src, kinds in CONN.items():
    assert src in names, src
    for outs in kinds.values():
        for out in outs:
            for c in out:
                assert c["node"] in names, c["node"]

if __name__ == "__main__":
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(workflow, fh, indent=2, ensure_ascii=False)
    print("wrote", OUT, "-", len(NODES), "nodes")
    with open(OUT_API, "w", encoding="utf-8") as fh:
        json.dump(PENDING_API, fh, indent=2, ensure_ascii=False)
    print("wrote", OUT_API)
