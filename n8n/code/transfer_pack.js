// Code node "Build transfer pack"  (Mode: Run Once for Each Item)
// Runs on the "Call 000" branch after the caregiver form.
// Reads: $('Assemble') (body + profile), $('Grading'), $('Caregiver form').
// Output: { subject, text } for the Gmail node.
const a    = $('Assemble').first().json;
const body = a.body, p = a.profile || {};
const ep   = body.episode, pb = ep.posture_breakdown_sec || {};
const g    = $('Grading').first().json.grading || {};
const f    = $('Caregiver form').first().json || {};
const ef   = ep.event_features || {}, pre = ef.prefall || {}, head = ef.head_impact || {};

const isbar = [
  'ISBAR HANDOVER — FALL (from FallGuard camera record)',
  '',
  `I  Identify: ${p.resident_name || '(name)'}, DOB ${p.dob || '(dob)'}, ${p.address || body.source.name}. Caller: carer.`,
  `S  Situation: Fall detected ${ep.fall_time}. Outcome so far: ${ep.status}. On the floor ${g.summary_numbers?.time_on_ground || Math.round(ep.time_on_ground_sec) + ' s'}` +
    ` (lying ${g.summary_numbers?.time_lying || Math.round(pb.lying || 0) + ' s'}, motionless ${g.summary_numbers?.time_motionless || Math.round(ep.time_motionless_sec || 0) + ' s'}).`,
  `B  Background: Fell ${g.fall_height_words || 'from ' + (pre.fall_height_category || 'unknown').replace(/_/g, ' ')}.` +
    ` Anticoagulant / antiplatelet: ${p.anticoagulant || 'unknown'}. ACD on file: ${p.acd_on_file || 'unknown'} (carer reviewed: ${f['ACD reviewed'] || 'n/a'}). GP: ${p.gp_name || ''} ${p.gp_phone || ''}.`,
  `A  Assessment (carer, at scene): injury ${f['Injury seen'] || 'n/a'}; alert ${f['Alert and responsive'] || 'n/a'}; pain ${f['Pain'] || 'n/a'}; can stand ${f['Can stand and walk'] || 'n/a'}.` +
    ` Camera analysis rates the risk that the head struck the floor as ${head.risk || 'unknown'} (${(head.reasons || []).join('; ') || 'no basis recorded'}). This is an estimate, not a diagnosis.`,
  `R  Recommendation: carer decided "${f['Decision'] || 'Call 000'}". Please assess for head injury and long-lie complications as clinically indicated.`,
  '',
  'TRANSFER PACK — take with the person:',
  '  [ ] Medicare card  [ ] concession card (Pensioner / Health Care / Seniors Health / DVA)  [ ] private-insurance card if any',
  '  [ ] current medication list or the medications themselves  [ ] allergy list',
  '  [ ] advance care directive copy  [ ] enduring guardian / guardianship papers',
  `  [ ] GP details: ${p.gp_name || ''} ${p.gp_phone || ''}  [ ] emergency contact: ${p.supporter_name || ''} ${p.supporter_email || ''}`,
  '  [ ] glasses, hearing aids, dentures, walking aid  [ ] phone and charger',
  '  [ ] this page',
  '',
  'Do NOT write the Medicare number, IHI or insurance membership number in messages; the cards go with the person.',
].join('\n');

return { json: { subject: `FALL — ${p.resident_name || body.source.name}: transfer pack and ISBAR`, text: isbar } };
