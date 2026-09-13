// n8n Code node — Mode: "Run Once for Each Item", Language: JavaScript
// Input : one item from the Webhook node (POST body is under $input.item.json.body)
// Output: the same item with a `grading` object added, used by the LLM prompt
//         and by the Switch node that picks the delivery channel.
//
// PLACEHOLDER RULES. Every threshold lives in the block below so the team's
// own grading criteria can replace them in one place. Numbers are DEMO scale:
// Level 2 = 10 s and Level 3 = 30 s come from the FallGuard sidebar (echoed in
// episode.thresholds). For reference, the literature's "long lie" is one hour
// or more on the floor, and Apple Watch auto-calls emergency services after
// about one minute of immobility plus a 30-second countdown.

const MOTIONLESS_ALERT_SEC = 60;   // deployment value; use ~15 for demo videos
const LONG_LIE_SEC = 3600;         // one hour = clinical "long lie"

const body = $input.item.json.body;
const ep = body.episode;
const T = ep.thresholds || {};
const pb = ep.posture_breakdown_sec || {};
const ef = ep.event_features || {};
const head = ef.head_impact || {};
const pre = ef.prefall || {};

const onGround   = Number(ep.time_on_ground_sec || 0);
const lying      = Number(pb.lying || 0);
const partial    = Number(pb.partially_upright || 0);
const notVisible = Number(pb.not_visible || 0);
const still      = Number(ep.time_motionless_sec || 0);
const stillUnknown = Number(ep.time_motion_unknown_sec || 0);
const visibleTime = lying + partial;

// ---- 0. Was it a fall at all? --------------------------------------------
// fall_confidence comes from the app: "confirmed" (body settled lying or
// head reached floor level), "unconfirmed" (neither seen, still down or a
// posture the rules cannot read), "likely_false_alarm" (neither seen and
// upright again within a few seconds: a crouch, bend or sit tripped the
// detector). A likely false alarm is logged, nobody is messaged.
const confidence = ef.fall_confidence || 'unconfirmed';
if (confidence === 'likely_false_alarm') {
  $input.item.json.grading = {
    tier: 0, tier_label: 'NONE', report_type: 'false_alarm_log', channels: ['log'],
    flags: ['likely_false_alarm'], fall_confidence: confidence,
    summary_numbers: {}, head_risk: head.risk || 'unknown', head_reasons: '',
    fall_height_words: '', fall_height_cm: '',
  };
  return $input.item;
}

// ---- 1. Base tier from what the state machine already decided ------------
// 3 HIGH     : Level 3 fired (no recovery within level3_sec) OR the stream
//              ended while the person was still down (status = unresolved)
// 2 MODERATE : Level 2 fired (down longer than level2_sec) but recovered
// 1 LOW      : recovered before Level 2
let tier;
if (ep.status === 'unresolved' || ep.max_alert_level >= 3) tier = 3;
else if (ep.max_alert_level >= 2) tier = 2;
else tier = 1;

// ---- 2. Modifiers (each one is explained to the caregiver in the report) --
const flags = [];

// Motionless for most of the visible time (and the movement measure was
// available for most of it) -> raise one tier.
if (visibleTime > 0 &&
    still >= 0.5 * visibleTime &&
    stillUnknown <= 0.3 * visibleTime &&
    onGround >= (T.level2_sec || 10)) {
  flags.push('mostly_motionless');
  if (tier < 3) tier += 1;
}

// Motionless beyond the absolute threshold -> HIGH regardless of anything else.
if (still >= MOTIONLESS_ALERT_SEC) { flags.push('motionless_over_threshold'); tier = 3; }

// Head-impact risk from the fall analysis. "high" means the head came down
// fast and reached floor level (an estimate from 2-D keypoints, not a
// detected contact). Even with a quick recovery this warrants a medical
// check, so it lifts the tier to at least MODERATE.
if (head.risk === 'high')   { flags.push('head_impact_risk_high');   if (tier < 2) tier = 2; }
if (head.risk === 'medium') { flags.push('head_impact_risk_medium'); }
if (head.risk === 'unknown' || ef.analysis_status !== 'done') flags.push('head_impact_unknown');

// Fall from an elevated position (feet estimated > 1 m above the floor):
// NICE head-injury guidance treats a fall from more than 1 m or 5 stairs as
// a dangerous mechanism.
if (pre.fall_height_category === 'elevated_over_1m') { flags.push('elevated_fall'); if (tier < 2) tier = 2; }

// Person out of view for a large share of the episode: the durations are a
// lower bound and someone should physically check.
if (onGround > 0 && notVisible >= 0.3 * onGround) flags.push('person_lost_from_view');

if (ep.status === 'unresolved') flags.push('no_recovery_observed');

// Fall called by pose rules / temporal change only, without the trained
// detector: lower confidence in the fall itself (rule precision is low).
const sig = ep.trigger_signals || {};
if (sig.detector === false && (sig.pose_rule || sig.temporal)) flags.push('rule_only_trigger');

if (lying >= LONG_LIE_SEC) flags.push('long_lie');

if (confidence === 'unconfirmed') flags.push('fall_unconfirmed');

// ---- 3. Report type per tier -----------------------------------------------
// Every form contains the four required items (time of event, time on the
// ground, head-impact risk, fall height); they differ in length and urgency.
// 1 -> activity_note          : short, goes into the daily log/digest
// 2 -> caregiver_notification : email, suggested check-in / medical check
// 3 -> incident_report        : structured report by email + immediate
//                               emergency message (Telegram)
const tierLabel = { 1: 'LOW', 2: 'MODERATE', 3: 'HIGH' }[tier];
const reportType = { 1: 'activity_note', 2: 'caregiver_notification', 3: 'incident_report' }[tier];
const channels = { 1: ['email_digest'], 2: ['email'], 3: ['telegram', 'email'] }[tier];

// Human-readable values for the prompt (avoid making the LLM do arithmetic)
function fmt(s) {
  s = Math.round(s);
  return s >= 60 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s} s`;
}
const heightWords = {
  standing_height: 'from standing height',
  seated_height: 'from a seated position (chair or bed edge)',
  bed_or_sofa_height: 'from a lying position (bed or sofa)',
  elevated_over_1m: 'from an elevated position, estimated more than 1 m above the floor',
  unknown: 'from an unknown height (the person was not clearly visible before the fall)',
}[pre.fall_height_category || 'unknown'];

$input.item.json.grading = {
  tier, tier_label: tierLabel, report_type: reportType, channels, flags,
  fall_confidence: confidence,
  summary_numbers: {
    time_on_ground: fmt(onGround),
    time_lying: fmt(lying),
    time_partially_upright: fmt(partial),
    time_not_visible: fmt(notVisible),
    time_motionless: fmt(still),
    motion_measure_unavailable: fmt(stillUnknown),
  },
  fall_height_words: heightWords,
  fall_height_cm: pre.hip_height_cm != null
    ? `hip about ${Math.round(pre.hip_height_cm)} cm and head about ${Math.round(pre.head_height_cm)} cm above the floor before the fall (rough single-camera estimate)`
    : 'no centimetre estimate (patient height not set)',
  head_risk: head.risk || 'unknown',
  head_reasons: (head.reasons || []).join('; ') || 'no analysis available',
};
return $input.item;
