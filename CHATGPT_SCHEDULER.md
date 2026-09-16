# ChatGPT Scheduler — Concall Tracker

## Scope and branch lock
- Repository: `psuyog910/Concall_tracker`.
- This workflow operates **only** on `chatgpt-tracker`.
- Never read, compare against, create commits on, or modify `main`.
- Never modify `quarterly_monitor.py`, `concall_monitor.py`, `last_quarterly.json`, `last_concall.json`, or other legacy application files unless explicitly requested.

## Required run audit
At the beginning of every run, record:
- Run timestamp in IST/ISO-8601.
- Unique run ID.
- Target branch: `chatgpt-tracker`.

At the end, audit:
1. Scheduler prompt execution reached.
2. `stocks.txt` read successfully.
3. `CHATGPT_TRACKER_STATE.json` read successfully.
4. Research attempted.
5. Consolidated-first rule applied.
6. Exact-key deduplication applied.
7. Report generation status.
8. Report delivery status.
9. GitHub archival/status-update status.

If any checkpoint fails, return `ERROR` naming the failed step, likely reason, and whether changes were safely persisted. Do not claim success.

## Required inputs
Before research, fetch both files explicitly with `ref=chatgpt-tracker`:
- `stocks.txt`
- `CHATGPT_TRACKER_STATE.json`

If either cannot be read, stop and report the inaccessible file. Never infer or fabricate watchlist/state data.

## State and deduplication
Use `CHATGPT_TRACKER_STATE.json` as persistent memory.

Canonical key:

`symbol + update_type + reporting_period_or_date`

Rules:
- Quarterly results and concall updates are independent update types.
- Never regenerate an exact key already in `completed_updates`.
- Deduplicate all candidates before generating reports.
- A new reporting period, actual new call/transcript, material correction/restatement, or meaningful new disclosure may create a new key.
- Do not mark an update complete until its report is generated, archived, and verified.

## Research and evidence
Research the current watchlist for genuinely new or materially changed:
- Quarterly results.
- Earnings releases and investor presentations.
- Actual earnings/conference-call transcripts or recordings.
- Official company and stock-exchange disclosures.

Prefer primary sources. Separate reported facts from interpretation. Every displayed number must be traceable to current evidence. Do not use hard-coded figures or prior-run report content as evidence.

### Consolidated-first rule
For quarterly financials:
1. Find and evaluate consolidated results first.
2. Use consolidated figures whenever available.
3. Use standalone figures only when consolidated results are unavailable.
4. Never substitute standalone figures when consolidated results exist.

A quarterly result is not a concall update unless an actual concall, earnings-call transcript, recording, or management-call source is available.

## Report generation and delivery — PDF always
For each genuinely new material update, create a professional, phone-readable report card as one page of a PDF. The PDF is the only delivered report format whenever there is at least one new update.

Quarterly page:
1. Company and quarter.
2. KPI strip.
3. Current / previous-quarter / year-ago financial comparison where available.
4. Positives.
5. Risks.
6. Management commentary.
7. Outlook/guidance.
8. Investor takeaway.
9. Primary-source verification note.

Concall page:
1. Company, quarter, and call date.
2. Headline management message.
3. Management takeaways.
4. Demand and outlook.
5. Margins and costs.
6. Deals, pipeline, or order book where relevant.
7. Capital allocation, capex, or M&A where relevant.
8. Positives.
9. Risks/concerns raised in the call.
10. Investor takeaway.
11. Primary-source verification note.

PDF rules:
- 1 new update: generate and deliver one phone-optimized PDF with one page.
- 2 new updates: generate and deliver one phone-optimized PDF with two pages.
- More than 2 new updates: generate and deliver one phone-optimized PDF with one page per update.
- Do not deliver individual PNGs, separate cards, ZIP files, or a prose digest.
- PNGs may be used as intermediate artwork, but they are not the delivery artifact.
- 0 new updates: reply exactly `No new material updates.` and generate no report file.
- If any workflow step fails, return a concise error instead of claiming a report was delivered.

## GitHub binary archival — mandatory implementation
GitHub's UTF-8 Contents API is not suitable for PNG/PDF uploads. Binary reports must be archived through the Git Database API using a real base64 payload.

Use this exact sequence on `chatgpt-tracker`:

1. Read the current `chatgpt-tracker` branch ref and obtain its current commit SHA.
2. Generate the final PDF locally and confirm the file exists, is non-empty, and has the expected page count.
3. Read the actual PDF bytes and base64-encode them. Do not base64-encode a filesystem path, JSON wrapper, or Markdown link.
4. Call `create_blob` with `encoding: "base64"` and `content: <complete base64 of the actual PDF bytes>`.
5. Collect the returned blob SHA.
6. Create a Git tree based on the current commit's tree, with an entry containing `path`, `mode: "100644"`, `type: "blob"`, and the returned blob SHA.
7. Create a commit with `create_commit`, using the current branch commit as the parent.
8. Move only `refs/heads/chatgpt-tracker` with `update_ref` to the new commit SHA. Never update `main`.
9. Verify archival by fetching the exact path with `ref=chatgpt-tracker`, or by fetching the committed blob and confirming its SHA/content. A successful `create_blob` alone is not archival.
10. Only after the PDF is verified on the branch, update `CHATGPT_TRACKER_STATE.json` sequentially using its current SHA.
11. Finally write `run_status/YYYY-MM-DD.json` on `chatgpt-tracker`, then verify that status-file commit/path exists.

### Binary archival failure handling
- If any Git Database API step is unavailable, fails, or cannot be verified, do not update tracker state.
- Do not claim the report was archived or committed.
- Return `ERROR` with the exact failed step and whether any earlier changes were safely persisted.
- Do not fall back to `update_file` for binary files; it is a UTF-8 text-file operation.
- Never claim a local sandbox path is a GitHub archive.

## Final quality-control gate
Before responding, verify:
- The delivered PDF page count equals the number of new material updates.
- Every page corresponds to a genuinely new, deduplicated update.
- Consolidated-first selection was followed.
- State was updated only after successful PDF generation, archival, and verification.
- No forbidden branch or legacy file was touched.
- The final response follows the PDF-only delivery contract exactly.
