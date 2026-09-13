// Code node "Build escalation"  (Mode: Run Once for Each Item)
// Input item = residents sheet row (from "Profile lookup 2"); payload via $('Webhook').
const row  = $input.item.json;
const body = $('Webhook').first().json.body;
const ep   = body.episode;
const pb   = ep.posture_breakdown_sec || {};
const level = body.alert_level;                         // 2 or 3 (top-level field on escalation events)
const head  = (ep.event_features && ep.event_features.head_impact) ? ep.event_features.head_impact.risk : 'unknown';

const lead = level === 3
  ? `🚨 EMERGENCY — no recovery after ${ep.thresholds.level3_sec} s`
  : `⚠️ STILL DOWN — ${ep.thresholds.level2_sec} s on the floor`;

const text =
  `${lead}\n` +
  `${row.resident_name || body.source.name} · ${body.source.name}\n` +
  `On the floor: ${Math.round(ep.time_on_ground_sec)} s (lying ${Math.round(pb.lying || 0)} s, motionless ${Math.round(ep.time_motionless_sec || 0)} s, out of view ${Math.round(pb.not_visible || 0)} s)\n` +
  `Head-impact risk: ${head}\n` +
  (level === 3
    ? `Check on them now. If not responsive or unable to get up: call 000.`
    : `Please check on them now.`);

return { json: { chat_id: row.carer_telegram_chat_id, text } };
