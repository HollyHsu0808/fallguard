// Code node "Build alert"  (Mode: Run Once for Each Item)
// Input item = one row of the residents sheet (from "Profile lookup").
// The webhook payload is reached through $('Webhook').
// Output: { chat_id, text } for the Telegram node. (Each-item mode: return ONE object, not an array.)
const row  = $input.item.json;                       // residents sheet row
const body = $('Webhook').first().json.body;
const ep   = body.episode;
const ef   = ep.event_features || {};
const pre  = ef.prefall || {};
const head = ef.head_impact || {};

const fellFrom = (pre.fall_height_category || 'unknown').replace(/_/g, ' ');
const conf = ef.fall_confidence || 'unconfirmed';
const confWords = {
  confirmed: 'confirmed — person seen on the floor',
  unconfirmed: 'not confirmed — check in person',
  likely_false_alarm: 'likely false alarm',
}[conf] || conf;

const text =
  `🔴 FALL DETECTED — ${row.resident_name || body.source.name}\n` +
  `Time: ${ep.fall_time}\n` +
  `Camera: ${body.source.name}\n` +
  `Fell from: ${fellFrom}\n` +
  `Head-impact risk: ${head.risk || 'unknown'}\n` +
  `Status: ${confWords}\n` +
  `On the floor so far: ${Math.round(ep.time_on_ground_sec)} s\n` +
  `A full report follows when the episode ends.`;

return { json: { chat_id: row.carer_telegram_chat_id, text } };
