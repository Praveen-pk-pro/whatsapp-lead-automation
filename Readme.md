# WhatsApp Lead Distribution Automation

Sends leads to clients over WhatsApp on a schedule, with human-like random
delays, a hard daily cap, and duplicate-proof assignment. Runs on **GitHub
Actions** with a **Supabase (Postgres)** backend.

## Current architecture (updated)

The source of truth for what gets sent is the **`prospects`** table — the
Google Places / Apify CSV data you upload through `web/upload.html`. There
is no separate `leads` table in the live setup; `prospects` does double duty
as both your outreach/CRM list (`outreach_status`) and the distribution
queue (`dist_status`).

**Setup order matters — run these in sequence:**

1. `prospects_schema.sql` — creates the `prospects` table + RLS policies
   (lets the upload page insert rows with just the anon key).
2. `prospects_dist_migration.sql` — adds `assigned_client_id`, `dist_status`,
   `dist_sent_at` to `prospects` so the distribution script can use it as a
   queue, plus a unique index guaranteeing a prospect is never sent twice.
3. `schema.sql` — creates `clients`, `send_log`, and `daily_send_counter`
   (still used as-is; only the lead source changed, not client/logging
   tables). **Skip the `leads` table section of this file** — it's unused
   in the current setup, kept only for reference if you ever want a
   separate real-lead-form intake instead of prospect data.

```bash
psql "$DATABASE_URL" -f prospects_schema.sql
psql "$DATABASE_URL" -f prospects_dist_migration.sql
psql "$DATABASE_URL" -f schema.sql   # creates clients / send_log / daily_send_counter
```

Then add your clients:
```sql
INSERT INTO clients (client_name, whatsapp_number) VALUES
  ('Acme Renovations', '+919812345670'),
  ('Coimbatore Cleaners', '+919812345671');
```

And load prospect data via `web/upload.html` (drop your Google Places CSV,
paste in your Supabase Project URL + anon key, upload).

## 1. Two separate Supabase credentials — don't mix them up

- **`web/upload.html`** uses your **Project URL + anon public key**
  (Settings → API). This is what's safe to paste into the browser page.
- **`distribute_leads.py`** (run via GitHub Actions) uses the
  **Postgres connection string** (Settings → Database → Connection string),
  set as the `DATABASE_URL` secret. This is a different credential — it
  connects directly to Postgres, bypassing RLS, so the distribution script
  can read/update rows the anon key can't.

## 2. Ordering

`distribute_leads.py` claims prospects with:
```sql
ORDER BY imported_at ASC, prospect_id ASC
```
i.e. strictly in the order they were uploaded — oldest CSV row first, no
randomization of *which* prospect goes out, only *when* (the delay between
sends is randomized, not the order).

## 3. Random delay implementation

Between every send except the last one in a batch:
```python
delay = random.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)  # 20–60s
time.sleep(delay)
```
Called fresh per message, so no two delays in a run match, and there's no
fixed pattern across runs.

## 4. Daily limit logic

- `daily_send_counter` is keyed by date, so it "resets" automatically —
  a new day just has no row yet, nothing to manually clear at midnight.
- Batch size per run is `min(5, 20 - already_sent_today)`.
- Only successful sends increment the counter; failed sends (even after
  retries) don't eat into the cap.

## 5. Duplicate prevention logic

- `claim_batch()` only selects `clients` with no `dist_status = 'SENT'`
  prospect assigned, and only `prospects` with `dist_status = 'AVAILABLE'`.
- A partial unique index (`idx_prospects_single_assignment`) makes it
  physically impossible for a prospect to be marked `SENT` twice, even if
  application logic has a bug.
- `FOR UPDATE SKIP LOCKED` means overlapping runs never race on the same
  rows.
- Prospects are flipped to `SENT` in the *same transaction* as being
  claimed — before the WhatsApp send even happens — so nothing can be
  double-assigned even under a crash mid-run.

## 6. Error handling & retry logic

`send_with_retry()`: up to `MAX_RETRIES` (default 3) attempts, `3 * attempt`
second backoff between tries, logs each failed attempt. One prospect's
total failure never stops the run — it moves to the next one in the batch.

## 7. Logging

Every send attempt writes a row to `send_log` (client, prospect id, number,
timestamp, status, attempt count, error). Query it directly:
```sql
SELECT client_name, status, COUNT(*)
FROM send_log
WHERE sent_time::date = CURRENT_DATE
GROUP BY client_name, status;
```
GitHub Actions also retains run logs (stdout) separately.

## 8. Environment variables (GitHub repo secrets)

| Secret | Description |
|---|---|
| `DATABASE_URL` | Supabase **Postgres connection string** (not the API URL) |
| `WA_PHONE_NUMBER_ID` | Meta App dashboard → WhatsApp → API Setup |
| `WA_ACCESS_TOKEN` | **Permanent** token via a System User — a 24h temp token will silently break the cron, same failure class as the YouTube OAuth issue |

See `.env.example` for local testing.

## 9. Deployment & testing

1. Run the three SQL files in order (see above) against your Supabase DB.
2. Insert your clients.
3. Upload a prospects CSV via `web/upload.html`.
4. Set the three GitHub secrets.
5. Push. Cron is `0 */5 * * *` (UTC) in
   `.github/workflows/lead-distribution.yml` — adjust if you want runs
   anchored to IST hours.
6. **Test manually first**: Actions tab → "WhatsApp Lead Distribution" →
   "Run workflow" before trusting the cron.
7. Confirm `send_log` rows appear, `daily_send_counter` increments, and
   `prospects.dist_status` flips to `SENT` for the ones sent.

## 10. Scaling / follow-ups

- If a client's assigned prospect send permanently fails, it stays
  `dist_status = 'SENT'` (not re-queued) — add a follow-up job if you want
  failed sends released back to `AVAILABLE` for reassignment.
- To promote a `prospects` row to a real paying client relationship, use
  `outreach_status` (`NEW` → `CONTACTED` → `REPLIED` → `CONVERTED`) —
  that field is independent of `dist_status`, so distribution and
  outreach tracking don't interfere with each other.

---

## n8n equivalent (if you prefer n8n over the GitHub Actions script)

Same logic, mapped to nodes — just swap every reference to `leads` below
for `prospects`, and `status` for `dist_status`:

1. **Cron node** — every 5 hours (`0 */5 * * *`).
2. **Postgres node** — `SELECT sent_count FROM daily_send_counter WHERE send_date = CURRENT_DATE`.
3. **IF node** — stop if `sent_count >= 20`.
4. **Postgres node** — run the `claim_batch` CTE query from `distribute_leads.py` (batch size `LEAST(5, 20 - sent_count)`).
5. **IF node** — stop gracefully if no rows.
6. **Split In Batches node** — iterate claimed pairs one at a time.
7. **Function node** — build the message text (same template as `build_message()`).
8. **HTTP Request node** — POST to `https://graph.facebook.com/v20.0/{{WA_PHONE_NUMBER_ID}}/messages`, retry-on-fail = 3.
9. **Postgres node** — insert into `send_log`, increment `daily_send_counter`.
10. **Wait node** — expression-based random 20–60s delay, then loop back to step 6.
11. **Error Workflow** — attached in workflow settings so one failure logs and continues instead of halting.

The GitHub Actions/Python version is what's actually deployed and what I'd
recommend sticking with — easier to version-control and test locally.
