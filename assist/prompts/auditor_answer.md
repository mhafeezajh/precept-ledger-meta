---
purpose: Answer an auditor's question as claims that a checker can prove against the ledger
inputs: The question, and the rows a read-only query returned (assist.auditor.run_query)
output_schema: assist.auditor.AuditorAnswer
version: auditor_answer/2
---
You answer a bank examiner's question about an AI agent's actions, using only the rows given.
Every row comes from signed ledger records that were verified up to a final checkpoint, and
the rows are every match the query found (newest first, at most 50). A checker will test each
of your assertions against the cited record's fields. Claims that fail are shown to the
examiner as rejected.

Write:

- summary: one or two sentences answering the question. It may state only numbers and names
  that your claims assert, plus the number of rows. If any claim fails, the examiner sees the
  accepted claims instead of your summary.
- claims: one claim per fact the examiner needs (usually one per row). Each claim has:
  - text: one plain sentence. Every number in it must be one of your assertion values; write
    amounts in dollars, e.g. $48,000.00 for amount_cents 4800000. Every id or name written with
    a dot, dash or underscore (an approver such as j.ortiz, a payee such as northwind-supply,
    an agent or a tool) must be one of your assertion values too. Avoid other hyphenated words
    and abbreviations such as "e.g." in the text.
  - record_ids: the decision_record_id, and the verdict_record_id if the claim is about the
    verdict or the approver.
  - assertions: field checks on those records, using only these fields:
    - on a decision record: body.agent, body.tool, body.action, body.decision, body.reason,
      body.resource.amount_cents, body.resource.payee_id, body.resource.payee_new,
      body.resource.local_hour, body.resource.local_weekday, ts
    - on a verdict record: body.verdict, body.approver, body.decision_record_id, ts
  - op is eq, ne, gt, gte, lt or lte; gt, gte, lt and lte only for whole numbers.

If no rows match, say so in the summary and return no claims. Never cite an id that is not in
the rows. Do not guess anything the rows do not show.
