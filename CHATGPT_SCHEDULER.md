# ChatGPT Scheduler — Concall Tracker

## Source of truth
- Read `stocks.txt` for the current Indian-stock watchlist.
- Read `CHATGPT_TRACKER_STATE.json` before every run. It is the scheduler's persistent memory.
- `last_quarterly.json` and `last_concall.json` belong to the legacy Python app; use them only as supporting context.

## Anti-duplication
An update is uniquely identified by **symbol + update type + reporting period/date**.

If the exact update is already in `CHATGPT_TRACKER_STATE.json` under `completed_updates`, do not regenerate it.

Create a new update only for:
1. a new reporting period;
2. a genuinely new concall transcript;
3. a material correction/restatement; or
4. a meaningful new disclosure that changes the previous conclusion.

After delivering a new update, record it in `CHATGPT_TRACKER_STATE.json` so future runs know it is complete.

## Quarterly result image
Use the established Concall Tracker quarterly-card hierarchy:
1. Company + quarter
2. KPI strip
3. Current / previous quarter / year-ago financial table with QoQ and YoY
4. Key positives
5. Key negatives / risks
6. Management commentary
7. Guidance / outlook
8. Investor takeaway
9. Source / verification note

## Concall image
Use the same visual language, typography, spacing, dark theme, rounded cards and information hierarchy as the quarterly result image. Use:
1. Company + quarter + call date
2. Headline management message
3. Key management takeaways
4. Demand & business outlook
5. Margins & costs
6. Deals / pipeline / order book where relevant
7. Capital allocation / capex / M&A where relevant
8. Key positives
9. Key concerns / risks raised in the call
10. Overall investor takeaway
11. Primary source links

Generate a clean, professional PNG image for both quarterly and concall updates.

## Evidence rules
Prefer official company filings, investor presentations, stock-exchange disclosures and official transcripts. Clearly separate reported facts from interpretation. Never invent figures, expectations or guidance.

## Scheduler workflow
Each day, scan the watchlist for meaningful new quarterly results, earnings releases, investor presentations, concall transcripts and official disclosures. Only report items not already completed in the state file. For each new item, deliver the appropriate PNG card plus a concise text summary and primary source links, then persist the completion record.

## Safety boundary
The ChatGPT scheduler workspace is the `chatgpt-tracker` branch. Do not modify the legacy Python application files or its state unless explicitly instructed. `main` is protected and should remain the production/original branch.
