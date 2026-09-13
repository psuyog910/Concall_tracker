# ChatGPT Scheduler — Concall Tracker

## Scope and branch lock
- This workflow operates **only** on the `chatgpt-tracker` branch.
- Never read from, write to, create commits on, or compare against `main`.
- The repository is `psuyog910/Concall_tracker`.
- Never modify `quarterly_monitor.py`, `concall_monitor.py`, `last_quarterly.json`, `last_concall.json`, or other legacy application files unless explicitly requested.

## Required run audit
At the beginning of every run, record:
- Run timestamp in IST/ISO-8601.
- Unique run ID.
- Target branch: `chatgpt-tracker`.

At the end, audit these exact checkpoints:
1. Scheduler prompt execution reached.
2. `stocks.txt` read successfully.
3. `CHATGPT_TRACKER_STATE.json` read successfully.
4. Research attempted.
5. Consolidated-first rule applied.
6. Exact-key deduplication applied.
7. Report generation status.
8. Report delivery status.
9. GitHub archival/status-update status.

If any required checkpoint fails, return `ERROR` with the failed step, likely reason, and whether any changes were safely persisted. Do not claim success.

## Required inputs
Before research, fetch both files from **`ref=chatgpt-tracker`**:
- `stocks.txt`
- `CHATGPT_TRACKER_STATE.json`

If either file cannot be read, stop research and report the inaccessible file. Never infer or fabricate watchlist/state data.

## State and deduplication
Use `CHATGPT_TRACKER_STATE.json` as the persistent memory.

The canonical deduplication key is:

`symbol + update_type + reporting_period_or_date`

Rules:
- Quarterly results and concall updates are independent update types.
- Never regenerate an exact key already present in `completed_updates`.
- A new reporting period is a new update.
- A new actual concall/transcript is a new update even for the same quarter.
- A material correction/restatement or meaningful new disclosure may create a new update with a distinct date/type key.
- Deduplicate candidates before generating any report.
- Do not mark an update complete until its report has been generated, archived, and verified.

## Research and evidence
Research the current watchlist for genuinely new or materially changed:
- Quarterly results.
- Earnings releases and investor presentations.
- Actual earnings/conference-call transcripts or recordings.
- Official company and stock-exchange disclosures.

Prefer primary sources. Separate reported facts from interpretation. Do not invent unavailable figures, commentary, guidance, dates, or source links.

### Consolidated-first rule
For quarterly financials:
1. Find and evaluate consolidated results first.
2. Use consolidated figures whenever available.
3. Use standalone figures only if consolidated results are unavailable.
4. Never substitute standalone figures when consolidated results exist.

A quarterly result must not be classified as a concall update unless an actual concall, earnings-call transcript, recording, or management-call source is available.

## Report generation
For each genuinely new material update, generate a professional, phone-readable PNG card in the established Concall Tracker style.

Quarterly card must contain:
1. Company and quarter.
2. KPI strip.
3. Current / previous-quarter / year-ago financial comparison where available.
4. Positives.
5. Risks.
6. Management commentary.
7. Outlook/guidance.
8. Investor takeaway.
9. Primary-source verification note.

Concall card must contain:
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

Do not use hard-coded or previously generated figures as evidence for a new run. Every displayed number must be traceable to the current research evidence.

## Delivery contract
- 1 new update: deliver only its PNG card.
- 2 new updates: deliver only the two PNG cards.
- More than 2 new updates: deliver only one phone-optimized PDF with one page per new card. Do not deliver individual cards, ZIP files, or a prose digest.
- 0 new updates: reply exactly `No new material updates.` and generate no report file.
- If any workflow step fails, return a concise error instead of claiming a report was delivered.

## GitHub persistence
If GitHub write tools are available:
1. Save actual generated PNGs under `reports/YYYY/MM/DD/` on `chatgpt-tracker`.
2. If there are more than two updates, save the actual combined PDF under the same date directory.
3. Verify each saved report exists on `chatgpt-tracker` before changing state.
4. Update `CHATGPT_TRACKER_STATE.json` only after successful report generation, archival, and verification.
5. Write `run_status/YYYY-MM-DD.json` as the final step, containing factual run timestamp, run ID, status (`success`, `no_updates`, or `error`), counts, report paths, checkpoint results, and error details.
6. Verify the status-file commit before claiming it was written.

If binary upload or state writing is unavailable, clearly report that limitation. Do not claim archival, state persistence, or status-file creation. Never modify `main`.

## Final quality-control gate
Before responding, verify:
- Delivered report count equals new-update count, subject to the PDF delivery rule.
- Every report corresponds to a genuinely new, deduplicated update.
- Consolidated-first selection was followed.
- State was updated only after successful report generation and archival.
- No forbidden branch or legacy file was touched.
- The final response follows the delivery contract exactly.
