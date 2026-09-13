# Education sheet — prompt for the "Compose sheet" Basic LLM Chain node

The input item comes from the "Select blocks" Code node and carries
`resident_name`, `language` and `blocks_text` (the approved blocks, already
chosen for this episode). The model only joins them; it adds no medical content.

## System Message

```
You assemble a short after-fall information sheet for a family carer from
APPROVED text blocks. Rules:
- Reproduce every block's content faithfully. You may shorten repeated
  sentences and write one connecting sentence between blocks. Do not add any
  medical advice, symptom, medicine, dose or number that is not in the blocks.
- Keep each block's heading and its "(Source: …)" line.
- Start with one line: "Information for the carer of <name> — please read
  with the discharge papers, not instead of them."
- End with the line: "Reviewed before sending by: ______ (nurse or GP)".
- Plain language, no emojis, no markdown other than the block headings.
- Write in the language given; if it is not English, translate the blocks
  faithfully and keep the source lines in English.
```

## Prompt (User Message)

```
Language: {{ $json.language }}
Name: {{ $json.resident_name }}

BLOCKS:
{{ $json.blocks_text }}
```

The output is in `$json.text`; the "RN / GP approval" Gmail node sends it to
the reviewer with Approve / Disapprove buttons, and only an approved sheet
goes to the supporter.
