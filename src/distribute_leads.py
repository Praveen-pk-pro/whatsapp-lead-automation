#!/usr/bin/env python3
"""
WhatsApp Lead Distribution Automation
--------------------------------------
Runs on a schedule (GitHub Actions cron, every 5 hours). Each run:
  1. Checks how many leads have already been sent today (resets at midnight
     automatically because the counter is keyed by calendar date).
  2. If the daily cap (20) is already hit, exits gracefully.
  3. Picks up to 5 (AVAILABLE lead, active client-without-a-lead-today) pairs.
  4. Sends each via WhatsApp Business Cloud API with a random 20-60s delay
     between sends, retries failed sends up to 3x, and logs everything.

Designed to be safe to run concurrently / re-run: lead assignment is done
with a row lock (SELECT ... FOR UPDATE SKIP LOCKED) so two overlapping runs
can never assign the same lead twice.
"""

import os
import sys
import time
import random
import logging
from datetime import date

import psycopg2
import psycopg2.extras
import requests

# ---------------------------------------------------------------------------
# Config (from environment / GitHub Actions secrets)
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ["DATABASE_URL"]  # postgres://user:pass@host:port/db
WA_PHONE_NUMBER_ID = os.environ["WA_PHONE_NUMBER_ID"]
WA_ACCESS_TOKEN = os.environ["WA_ACCESS_TOKEN"]
WA_API_VERSION = os.environ.get("WA_API_VERSION", "v20.0")

LEADS_PER_RUN = int(os.environ.get("LEADS_PER_RUN", "5"))
DAILY_LEAD_CAP = int(os.environ.get("DAILY_LEAD_CAP", "20"))
MIN_DELAY_SECONDS = int(os.environ.get("MIN_DELAY_SECONDS", "20"))
MAX_DELAY_SECONDS = int(os.environ.get("MAX_DELAY_SECONDS", "60"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))

WA_API_URL = f"https://graph.facebook.com/{WA_API_VERSION}/{WA_PHONE_NUMBER_ID}/messages"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("lead_distributor")


def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


# ---------------------------------------------------------------------------
# Daily cap logic (resets automatically at midnight, no cron job needed)
# ---------------------------------------------------------------------------
def get_today_sent_count(cur) -> int:
    cur.execute(
        "SELECT sent_count FROM daily_send_counter WHERE send_date = %s",
        (date.today(),),
    )
    row = cur.fetchone()
    return row[0] if row else 0


def increment_today_sent_count(cur, by: int = 1):
    cur.execute(
        """
        INSERT INTO daily_send_counter (send_date, sent_count)
        VALUES (%s, %s)
        ON CONFLICT (send_date)
        DO UPDATE SET sent_count = daily_send_counter.sent_count + EXCLUDED.sent_count
        """,
        (date.today(), by),
    )


# ---------------------------------------------------------------------------
# Lead assignment — duplicate-proof
# ---------------------------------------------------------------------------
def claim_batch(cur, batch_size: int):
    """
    Atomically pairs up to `batch_size` AVAILABLE prospects (oldest-imported
    first, i.e. strict CSV upload order) with active clients who haven't
    received a prospect yet, and flips those prospects to SENT in the same
    transaction. FOR UPDATE SKIP LOCKED means concurrent runs never race on
    the same rows, and a prospect can never end up assigned twice.
    """
    cur.execute(
        """
        WITH clients_without_lead AS (
            SELECT c.client_id, c.client_name, c.whatsapp_number
            FROM clients c
            WHERE c.active = TRUE
              AND NOT EXISTS (
                  SELECT 1 FROM prospects p
                  WHERE p.assigned_client_id = c.client_id AND p.dist_status = 'SENT'
              )
            ORDER BY c.client_id
            LIMIT %s
        ),
        available_prospects AS (
            SELECT prospect_id
            FROM prospects
            WHERE dist_status = 'AVAILABLE'
            ORDER BY imported_at ASC, prospect_id ASC   -- strict upload order
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        ),
        pairs AS (
            SELECT
                row_number() OVER () AS rn,
                prospect_id
            FROM available_prospects
        ),
        clients_ranked AS (
            SELECT row_number() OVER () AS rn, client_id, client_name, whatsapp_number
            FROM clients_without_lead
        )
        SELECT p.prospect_id, c.client_id, c.client_name, c.whatsapp_number
        FROM pairs p
        JOIN clients_ranked c ON c.rn = p.rn
        """,
        (batch_size, batch_size),
    )
    pairs = cur.fetchall()  # [(prospect_id, client_id, client_name, whatsapp_number), ...]

    if not pairs:
        return []

    prospect_ids = [p[0] for p in pairs]
    cur.execute(
        """
        UPDATE prospects
        SET dist_status = 'SENT', dist_sent_at = now(),
            assigned_client_id = data.client_id,
            outreach_status = 'CONTACTED'
        FROM (VALUES %s) AS data(prospect_id, client_id)
        WHERE prospects.prospect_id = data.prospect_id
        """
        % ",".join(cur.mogrify("(%s,%s)", (p[0], p[1])).decode() for p in pairs),
    )

    # Return full prospect detail for the assigned rows
    cur.execute(
        """
        SELECT prospect_id, title, phone, email, address, city, state,
               category, website, google_maps_url, rating, reviews_count,
               notes, dist_sent_at
        FROM prospects WHERE prospect_id = ANY(%s)
        """,
        (prospect_ids,),
    )
    prospect_details = {row[0]: row for row in cur.fetchall()}

    result = []
    for prospect_id, client_id, client_name, whatsapp_number in pairs:
        result.append(
            {
                "client_id": client_id,
                "client_name": client_name,
                "whatsapp_number": whatsapp_number,
                "lead": prospect_details[prospect_id],
            }
        )
    return result


# ---------------------------------------------------------------------------
# WhatsApp message
# ---------------------------------------------------------------------------
def build_message(item) -> str:
    (prospect_id, title, phone, address, city, state, category,
     website, google_maps_url, rating, reviews_count, notes, sent_at) = item["lead"]

    location = ", ".join(x for x in [address, city, state] if x)

    lines = [
        f"Hi {item['client_name']}, you have a new lead:",
        "",
        f"*Business:* {title}",
    ]
    if phone:
        lines.append(f"*Phone:* {phone}")
    if location:
        lines.append(f"*Location:* {location}")
    if category:
        lines.append(f"*Category:* {category}")
    if website:
        lines.append(f"*Website:* {website}")
    if rating is not None and reviews_count is not None:
        lines.append(f"*Google Rating:* {rating} ({reviews_count} reviews)")
    if google_maps_url:
        lines.append(f"*Maps Link:* {google_maps_url}")
    lines.append(f"*Date & Time:* {sent_at.strftime('%d %b %Y, %I:%M %p')}")
    if notes:
        lines.append(f"*Notes:* {notes}")
    lines.append("")
    lines.append("Please reach out to the customer promptly. Thank you!")
    return "\n".join(lines)


def send_whatsapp_message(to_number: str, body: str):
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": body},
    }
    resp = requests.post(WA_API_URL, headers=headers, json=payload, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"WhatsApp API error {resp.status_code}: {resp.text}")
    return resp.json()


def send_with_retry(to_number: str, body: str, max_retries: int):
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            send_whatsapp_message(to_number, body)
            return True, attempt, None
        except Exception as e:
            last_error = str(e)
            log.warning(f"Attempt {attempt}/{max_retries} failed for {to_number}: {e}")
            if attempt < max_retries:
                time.sleep(3 * attempt)  # small backoff between retries
    return False, max_retries, last_error


def log_send(cur, client_id, client_name, prospect_id, whatsapp_number,
             status, attempt_number, error_message=None):
    cur.execute(
        """
        INSERT INTO send_log
            (client_id, client_name, prospect_id, whatsapp_number,
             status, attempt_number, error_message)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (client_id, client_name, prospect_id, whatsapp_number,
         status, attempt_number, error_message),
    )


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------
def main():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sent_today = get_today_sent_count(cur)
            remaining_today = DAILY_LEAD_CAP - sent_today

            if remaining_today <= 0:
                log.info(f"Daily cap of {DAILY_LEAD_CAP} already reached ({sent_today} sent). Skipping run.")
                conn.commit()
                return

            batch_size = min(LEADS_PER_RUN, remaining_today)
            batch = claim_batch(cur, batch_size)
            conn.commit()  # commit the assignment immediately so leads are locked in

        if not batch:
            log.info("No available prospects or no clients without an assigned lead. Skipping run gracefully.")
            return

        log.info(f"Assigned {len(batch)} lead(s) this run. Sending with randomized delays...")

        successes = 0
        for i, item in enumerate(batch):
            message = build_message(item)
            success, attempts, error = send_with_retry(
                item["whatsapp_number"], message, MAX_RETRIES
            )

            with conn.cursor() as cur:
                log_send(
                    cur,
                    item["client_id"],
                    item["client_name"],
                    item["lead"][0],
                    item["whatsapp_number"],
                    "SUCCESS" if success else "FAILED",
                    attempts,
                    error,
                )
                if success:
                    increment_today_sent_count(cur, 1)
                    successes += 1
                else:
                    log.error(
                        f"Giving up on lead {item['lead'][0]} -> {item['client_name']} "
                        f"after {attempts} attempts: {error}"
                    )
                conn.commit()

            if success:
                log.info(f"Sent lead {item['lead'][0]} to {item['client_name']} ({item['whatsapp_number']})")
            else:
                log.error(f"FAILED lead {item['lead'][0]} to {item['client_name']}")

            # Random human-like delay between messages (skip after the last one)
            if i < len(batch) - 1:
                delay = random.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
                log.info(f"Waiting {delay}s before next message...")
                time.sleep(delay)

        log.info(f"Run complete. {successes}/{len(batch)} sent successfully.")

    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception(f"Fatal error in distribution run: {e}")
        sys.exit(1)
