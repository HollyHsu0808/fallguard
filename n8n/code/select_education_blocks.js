// Code node "Select blocks"  (Mode: Run Once for All Items)
// Input items = ALL rows of the education_blocks sheet (columns: key, lang, title, text, source).
// Picks the blocks that match the episode's state and returns ONE item with the ordered blocks.
const rows = $input.all().map(i => i.json);
const a    = $('Assemble').first().json;
const p    = a.profile || {};
const ep   = a.body.episode;
const ef   = ep.event_features || {}, head = ef.head_impact || {};
const f2   = $('Outcome form').first().json || {};
const lang = (p.language || 'en').toLowerCase();

const keys = ['after_a_fall_basics'];
if (['high', 'medium'].includes(head.risk)) keys.push('head_injury_watch');
if (String(p.anticoagulant).toLowerCase().startsWith('y')) keys.push('anticoagulant_warning');
if (ep.time_on_ground_sec >= (ep.thresholds?.level2_sec || 10) || ep.status !== 'recovered') keys.push('cannot_get_up');
if (['Admitted', 'ED then home'].includes(f2['Outcome'] || '')) keys.push('after_hospital');   // discharge / after-hospital care for both ways home
keys.push('prevent_next_fall', 'when_to_call');

const pick = k => rows.find(r => r.key === k && (r.lang || 'en').toLowerCase() === lang)
                || rows.find(r => r.key === k && (r.lang || 'en').toLowerCase() === 'en');
const blocks = keys.map(pick).filter(Boolean);

return [{ json: {
  resident_name: p.resident_name || '', language: lang, keys,
  blocks_text: blocks.map(b => `## ${b.title}\n${b.text}\n(Source: ${b.source})`).join('\n\n'),
} }];
