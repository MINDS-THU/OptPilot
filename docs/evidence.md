# Evidence and provenance

Use `optpilot experiment graph`, `optpilot experiment detail RUN_ID`, and `optpilot experiment version VERSION_ID` to inspect the same facts shown by Desktop. Every Trial records actual input versions once submitted, metrics, state and Attempts. Failed Attempts retain their errors even without scores. Aggregates show valid and total counts separately for each Task, so a missing score is never presented as zero.

Select a version or evaluation in the relationship map to see related conversation positions. Links come from real saves, launches, imports, verified edits, successful public tool receipts and sent attachments. Zero related positions is valid. Draft attachments do not count. The position list jumps to the original message across conversations.

The evaluation detail also shows each Generator call's permitted feedback and returned Candidate versions, per-Instance Trial evidence, resource reservations and measured use, and external Harbor or imported job IDs. Imported results retain source Trial and Attempt IDs and report whether coverage is summary-only, partial or complete. Add a comparison or one Trial to a conversation draft and send it to continue from measured results.
