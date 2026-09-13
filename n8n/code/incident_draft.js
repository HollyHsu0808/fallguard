// Code node "Incident draft"  (Mode: Run Once for Each Item)
// Runs after the outcome form. Pre-fills what the camera record knows and leaves the
// clinical fields for a person. Output: { subject, text, row } — text for Gmail, row for Sheets.
const a    = $('Assemble').first().json;
const body = a.body, p = a.profile || {};
const ep   = body.episode, pb = ep.posture_breakdown_sec || {};
const g    = $('Grading').first().json.grading || {};
const f1   = $('Caregiver form').first().json || {};
const f2   = $('Outcome form').first().json || {};
const ef   = ep.event_features || {}, pre = ef.prefall || {}, head = ef.head_impact || {};

const alerts = (ep.alerts || []).map(x => `  - Level ${x.level} ${x.label} at ${x.time} (${x.time_on_ground_sec} s on floor)`).join('\n');
const text = [
  `INCIDENT REPORT — DRAFT (pre-filled by FallGuard; fields marked [ ] need a person)`,
  ``,
  `Person: ${p.resident_name || ''}   DOB: ${p.dob || ''}   Location: ${p.address || body.source.name}`,
  `Event: fall detected ${ep.fall_time} (video time ${ep.fall_video_time_sec} s), episode ${ep.episode_id}`,
  `Witnessed: camera record exists (source ${body.source.name}); human witness: [ ]`,
  ``,
  `What the camera recorded`,
  `  Fell ${g.fall_height_words || 'from ' + (pre.fall_height_category || 'unknown')}; pre-fall posture ${pre.posture || 'unknown'}.`,
  `  Time on the floor ${Math.round(ep.time_on_ground_sec)} s: lying ${Math.round(pb.lying || 0)} s, partially upright ${Math.round(pb.partially_upright || 0)} s, out of view ${Math.round(pb.not_visible || 0)} s; motionless ${Math.round(ep.time_motionless_sec || 0)} s.`,
  `  Outcome of episode: ${ep.status} (${ep.status === 'recovered' ? 'got up and stayed upright' : 'recording ended while still down'}).`,
  `  Head-impact risk rating: ${head.risk || 'unknown'} — ${(head.reasons || []).join('; ')}`,
  `  Fall confirmed by camera: ${ef.fall_confidence || 'unconfirmed'}`,
  `  Alerts sent:\n${alerts || '  - none'}`,
  ``,
  `Carer assessment at scene: injury ${f1['Injury seen'] || '[ ]'}; alert ${f1['Alert and responsive'] || '[ ]'}; pain ${f1['Pain'] || '[ ]'}; can stand ${f1['Can stand and walk'] || '[ ]'}; ACD reviewed ${f1['ACD reviewed'] || '[ ]'}; decision ${f1['Decision'] || '[ ]'}; notes ${f1['Notes'] || ''}`,
  `Outcome: ${f2['Outcome'] || '[ ]'}; hospital ${f2['Hospital'] || ''}; notes ${f2['Notes'] || ''}`,
  ``,
  `Fields a person must complete`,
  `  [ ] Injuries found by a clinician (site, severity)`,
  `  [ ] Immediate actions taken (how lifted, first aid)`,
  `  [ ] Likely cause and environment (floor, footwear, lighting, medication changes, night toileting)`,
  `  [ ] Who was notified and when (family, GP, ambulance) — camera log: see event_log sheet`,
  `  [ ] Meets the QI Program "major injury" definition? (fracture, dislocation, closed head injury with altered consciousness, subdural haematoma)`,
  `  [ ] SIRS reportable category, priority and reasoning — provider decision; NOT automated`,
  `  [ ] Care-plan changes`,
].join('\n');

const row = {
  episode_id: ep.episode_id, resident: p.resident_name || '', fall_time: ep.fall_time,
  time_on_ground_sec: ep.time_on_ground_sec, head_risk: head.risk || 'unknown',
  fall_height: pre.fall_height_category || 'unknown', fall_confidence: ef.fall_confidence || '',
  decision: f1['Decision'] || '', outcome: f2['Outcome'] || '', tier: g.tier_label || '',
  draft_created_at: new Date().toISOString(),
};
return { json: { subject: `Incident report draft — ${p.resident_name || body.source.name} — ${ep.fall_time}`, text, row } };
