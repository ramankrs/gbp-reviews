#!/usr/bin/env python3
"""
Google Business Profile Reviews → Slack Alerts + Google Sheets Log

Fetches new reviews from all GBP locations under your account,
posts formatted alerts to two Slack channels via webhook,
and logs each review to a Google Sheet.

Usage:
    python gbp_reviews.py                # Normal run
    python gbp_reviews.py --reset        # Clear tracking data and re-fetch last 7 days
    python gbp_reviews.py --test         # Post dummy reviews + summary to verify layout
    python gbp_reviews.py --daily-summary  # Post daily review summary to Slack
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# ---------------------------------------------------------------------------
# Configuration — edit these to match your setup
# ---------------------------------------------------------------------------

# OAuth scopes — GBP for reviews, Sheets for logging
SCOPES = [
    "https://www.googleapis.com/auth/business.manage",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Slack incoming-webhook URLs — both channels receive every review
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_WEBHOOK_URL_2 = os.getenv("SLACK_WEBHOOK_URL_2", "")

# Google Sheet for review logging
SPREADSHEET_ID = "1tuxKTxlyQP99v0ELIfDul514CLiIiJSamVLJo0qUN9A"
SHEET_NAME = "Reviews"

# File paths (stored next to this script)
SCRIPT_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = SCRIPT_DIR / "credentials.json"
TOKEN_FILE = SCRIPT_DIR / "token.json"
PROCESSED_FILE = SCRIPT_DIR / "processed_reviews.json"
LOG_FILE = SCRIPT_DIR / "reviews_log.txt"

# How far back to look on the very first run (days)
INITIAL_LOOKBACK_DAYS = 7

# Google Business Profile API base URL
GBP_API_BASE = "https://mybusinessbusinessinformation.googleapis.com/v1"
GBP_ACCOUNT_API = "https://mybusinessaccountmanagement.googleapis.com/v1"
GBP_REVIEWS_API = "https://mybusiness.googleapis.com/v4"
SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"

REVIEW_REPLY_URL = "https://business.google.com/reviews"

STAR_COUNTS = {
    "STAR_RATING_UNSPECIFIED": 0,
    "ONE": 1,
    "TWO": 2,
    "THREE": 3,
    "FOUR": 4,
    "FIVE": 5,
}

IST = timezone(timedelta(hours=5, minutes=30))

# Keyword → short display name for the 7 clinics in the daily summary.
# First matching keyword wins, so order matters (longer/specific first).
CLINIC_KEYWORDS = [
    ("Kasavanahalli", "Kasavanahalli"),
    ("Hosa Road",     "Kasavanahalli"),
    ("Kasav",         "Kasavanahalli"),
    ("Electronic City", "Electronic City"),
    ("Bellandur",     "Bellandur"),
    ("Varthur",       "Varthur"),
    ("HSR",           "HSR Layout"),
    ("Yelahanka",     "Yelahanka"),
    ("Thanisandra",   "Thanisandra"),
]

# Display order in the summary table
SUMMARY_CLINICS = [
    "HSR Layout",
    "Bellandur",
    "Varthur",
    "Kasavanahalli",
    "Electronic City",
    "Yelahanka",
    "Thanisandra",
]

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger = logging.getLogger("gbp_reviews")
logger.setLevel(logging.INFO)

# Console handler
console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
logger.addHandler(console)

# File handler (append mode so we keep history)
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(
    logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
)
logger.addHandler(file_handler)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def authenticate():
    """
    Authenticate with Google using OAuth 2.0.

    On the first run this opens a browser window so you can sign in.
    After that, the token is saved locally and refreshed automatically.
    """
    creds = None

    # Load saved token if it exists
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    # If no valid credentials, run the OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired token...")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                logger.error(
                    "credentials.json not found! "
                    "Download it from Google Cloud Console and place it next to this script."
                )
                sys.exit(1)

            # In CI (GitHub Actions) there is no browser — exit with a clear message
            if os.getenv("CI"):
                logger.error(
                    "Running in CI but no valid token found. "
                    "Run the script locally first to generate token.json, "
                    "then save its contents as the GOOGLE_TOKEN GitHub Secret."
                )
                sys.exit(1)

            logger.info("No saved token — opening browser for Google sign-in...")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Save the token for future runs
        TOKEN_FILE.write_text(creds.to_json())
        logger.info("Token saved to %s", TOKEN_FILE)

    return creds


# ---------------------------------------------------------------------------
# Google Business Profile API helpers
# ---------------------------------------------------------------------------


def get_accounts(creds):
    """Fetch all GBP accounts accessible by this Google account."""
    headers = {"Authorization": f"Bearer {creds.token}"}
    url = f"{GBP_ACCOUNT_API}/accounts"
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    accounts = data.get("accounts", [])
    if not accounts:
        logger.error("No GBP accounts found for this Google account.")
        sys.exit(1)
    return accounts


def get_locations(creds, account_name):
    """
    Fetch all locations (clinic listings) under a GBP account.

    account_name looks like 'accounts/123456789'.
    """
    headers = {"Authorization": f"Bearer {creds.token}"}
    locations = []
    page_token = None

    while True:
        params = {"readMask": "name,title", "pageSize": 100}
        if page_token:
            params["pageToken"] = page_token

        url = f"{GBP_API_BASE}/{account_name}/locations"
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        locations.extend(data.get("locations", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return locations


def get_reviews(creds, account_name, location_name):
    """
    Fetch reviews for a single location.

    Returns all reviews (paginated). We filter by date later.
    location_name looks like 'locations/456'.
    """
    headers = {"Authorization": f"Bearer {creds.token}"}
    reviews = []
    page_token = None

    while True:
        params = {"pageSize": 50, "orderBy": "updateTime desc"}
        if page_token:
            params["pageToken"] = page_token

        url = f"{GBP_REVIEWS_API}/{account_name}/{location_name}/reviews"
        resp = requests.get(url, headers=headers, params=params, timeout=30)

        if resp.status_code == 403:
            logger.error(
                "    ⚠ 403 Forbidden when fetching reviews for %s/%s",
                account_name,
                location_name,
            )
            logger.error(
                "    FIX: Enable the 'Google My Business API' in Google Cloud Console:\n"
                "         1. Go to https://console.cloud.google.com/apis/library\n"
                "         2. Search for 'Google My Business API'\n"
                "         3. Click Enable\n"
                "         4. Make sure your OAuth credentials belong to the same project"
            )
            raise requests.exceptions.HTTPError(response=resp)

        resp.raise_for_status()
        data = resp.json()

        batch = data.get("reviews", [])
        if not batch:
            break

        reviews.extend(batch)
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return reviews


def get_clinic_short_name(location_title):
    """Return the short display name for a location, or None if not one of the 7 clinics."""
    title_lower = location_title.lower()
    for keyword, short_name in CLINIC_KEYWORDS:
        if keyword.lower() in title_lower:
            return short_name
    return None


def get_location_avg_rating(creds, account_name, location_name):
    """Fetch the overall average star rating for a location from the GBP API."""
    headers = {"Authorization": f"Bearer {creds.token}"}
    url = f"{GBP_REVIEWS_API}/{account_name}/{location_name}/reviews"
    try:
        resp = requests.get(url, headers=headers, params={"pageSize": 1}, timeout=30)
        if resp.status_code == 200:
            return resp.json().get("averageRating")
    except requests.exceptions.RequestException:
        pass
    return None


def fetch_reviews_since(creds, account_name, location_name, since_dt):
    """
    Fetch reviews for a location, stopping once we've collected enough
    to cover the window starting at since_dt. Capped at 1 000 reviews
    (20 pages × 50) to prevent runaway pagination on busy locations.
    """
    headers = {"Authorization": f"Bearer {creds.token}"}
    reviews = []
    page_token = None
    page_count = 0

    while page_count < 20:
        page_count += 1
        params = {"pageSize": 50, "orderBy": "updateTime desc"}
        if page_token:
            params["pageToken"] = page_token

        url = f"{GBP_REVIEWS_API}/{account_name}/{location_name}/reviews"
        resp = requests.get(url, headers=headers, params=params, timeout=30)

        if resp.status_code == 403:
            raise requests.exceptions.HTTPError(response=resp)
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("reviews", [])
        if not batch:
            break

        reviews.extend(batch)

        # Stop paginating once the oldest createTime in this batch is before our window.
        # (updateTime ordering means a very old review with a recent reply may appear
        # near the top — we still collect it and let the caller filter by createTime.)
        oldest_str = batch[-1].get("createTime", "")
        try:
            oldest_dt = datetime.fromisoformat(oldest_str.replace("Z", "+00:00"))
            if oldest_dt < since_dt:
                break
        except (ValueError, AttributeError):
            pass

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return reviews


# ---------------------------------------------------------------------------
# Processed-reviews tracker (prevents duplicate Slack posts)
# ---------------------------------------------------------------------------


def load_processed():
    """Load the set of already-processed review IDs and the last-run timestamp."""
    if PROCESSED_FILE.exists():
        data = json.loads(PROCESSED_FILE.read_text())
        return {
            "review_ids": set(data.get("review_ids", [])),
            "last_run": data.get("last_run"),
        }
    return {"review_ids": set(), "last_run": None}


def save_processed(state):
    """Persist the tracking state to disk."""
    data = {
        "review_ids": sorted(state["review_ids"]),
        "last_run": state["last_run"],
    }
    PROCESSED_FILE.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Slack posting
# ---------------------------------------------------------------------------


def _build_slack_payload(location_title, review):
    """Build the Block Kit payload for Channel 1 (#google-reviews)."""
    star_count = STAR_COUNTS.get(review.get("starRating", ""), 0)
    stars = "\u2b50\ufe0f" * star_count if star_count else "\u2606"
    reviewer = review.get("reviewer", {}).get("displayName", "Anonymous")
    comment = review.get("comment", "").strip()
    create_time = review.get("createTime", "")

    try:
        dt = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
        dt_ist = dt.astimezone(IST)
        time_display = dt_ist.strftime("%b %d, %Y at %I:%M %p") + " IST"
    except (ValueError, AttributeError):
        time_display = create_time

    header = "\U0001f4e2 *New Review*"

    detail_lines = [
        f"\U0001f4cd {location_title}",
        f"\u23f0 {time_display}",
        "",
        stars,
        "",
        f"\U0001f464 {reviewer}",
    ]
    if comment:
        detail_lines.append(f'\U0001f4ac "{comment}"')

    footer = f"<{REVIEW_REPLY_URL}|Reply to this review \u2934\ufe0f>"

    return {
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(detail_lines)}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": footer}},
            {"type": "divider"},
        ],
        "text": f"New review for {location_title} by {reviewer}",
    }


def _build_slack_payload_plain(location_title, review):
    """Build a plain-text payload for Channel 2 (#gmb-reviews)."""
    star_count = STAR_COUNTS.get(review.get("starRating", ""), 0)
    reviewer = review.get("reviewer", {}).get("displayName", "Anonymous")
    comment = review.get("comment", "").strip()
    create_time = review.get("createTime", "")

    try:
        dt = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
        dt_ist = dt.astimezone(IST)
        time_display = dt_ist.strftime("%b %d %Y %H:%M:%S")
    except (ValueError, AttributeError):
        time_display = create_time

    review_text = comment if comment else "No written review"

    message = (
        "\U0001f31f New Google Review!\n"
        "\n"
        f"Clinic: {location_title}\n"
        f"City: Bengaluru\n"
        f"Reviewer: {reviewer}\n"
        f"Rating: {star_count}\n"
        f"Review: {review_text}\n"
        f"Received at: {time_display}"
    )

    return {"text": message}


def post_to_slack(location_title, review):
    """Send a review alert to all configured Slack channels."""
    reviewer = review.get("reviewer", {}).get("displayName", "Anonymous")

    channels = [
        ("Channel 1", SLACK_WEBHOOK_URL, _build_slack_payload),
        ("Channel 2", SLACK_WEBHOOK_URL_2, _build_slack_payload_plain),
    ]

    for label, url, builder in channels:
        if not url:
            continue
        payload = builder(location_title, review)
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code != 200:
                logger.warning(
                    "  Slack %s failed (%s): %s", label, resp.status_code, resp.text
                )
            else:
                logger.info("  \u2192 Slack %s alert sent for review by %s", label, reviewer)
        except requests.exceptions.RequestException as e:
            logger.warning("  Slack %s request error: %s", label, e)


def post_daily_summary_to_slack(date_display, clinic_rows, totals_row):
    """
    Format and post the daily summary table to both Slack channels.

    clinic_rows: list of dicts with keys:
        name, yesterday, s5, s4, s3, s2, s1, mtd, avg
    totals_row: same keys, representing column sums/weighted avg
    """
    C = 16  # clinic column width (longest name: "Electronic City" = 15 chars)

    header_row = (
        f"{'Clinic':<{C}}  {'Yday':>4}  {'5★':>3}  {'4★':>3}  "
        f"{'3★':>3}  {'2★':>3}  {'1★':>3}  {'MTD':>5}  {'Avg':>4}"
    )
    divider = "─" * len(header_row)

    def fmt_row(r):
        return (
            f"{r['name']:<{C}}  {r['yesterday']:>4}  {r['s5']:>3}  {r['s4']:>3}  "
            f"{r['s3']:>3}  {r['s2']:>3}  {r['s1']:>3}  {r['mtd']:>5}  {r['avg']:>4}"
        )

    table_lines = [header_row, divider]
    for row in clinic_rows:
        table_lines.append(fmt_row(row))
    table_lines.append(divider)
    table_lines.append(fmt_row({**totals_row, "name": "TOTAL"}))

    message = (
        f"📊 *Daily Reviews Summary — {date_display}*\n\n"
        "```\n" + "\n".join(table_lines) + "\n```\n\n"
        "_Posted daily at 8:00 AM IST by Verbato_"
    )

    payload = {"text": message}

    channels = [
        ("Channel 1 (#google-reviews)", SLACK_WEBHOOK_URL),
        ("Channel 2 (#gmb-reviews)",    SLACK_WEBHOOK_URL_2),
    ]

    for label, url in channels:
        if not url:
            logger.warning("Daily summary: %s webhook not set — skipped.", label)
            continue
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code != 200:
                logger.warning(
                    "Daily summary %s failed (%s): %s", label, resp.status_code, resp.text
                )
            else:
                logger.info("Daily summary posted to %s.", label)
        except requests.exceptions.RequestException as e:
            logger.warning("Daily summary %s request error: %s", label, e)


# ---------------------------------------------------------------------------
# Google Sheets logging
# ---------------------------------------------------------------------------


def log_to_sheet(creds, location_title, review):
    """Append a review row to the Google Sheet."""
    star_count = STAR_COUNTS.get(review.get("starRating", ""), 0)
    reviewer = review.get("reviewer", {}).get("displayName", "Anonymous")
    comment = review.get("comment", "").strip()

    now_ist = datetime.now(timezone.utc).astimezone(IST)
    timestamp = now_ist.strftime("%b %d, %Y %I:%M %p")

    row = [
        timestamp,
        location_title,
        reviewer,
        star_count,
        comment,
        "Pending",
        REVIEW_REPLY_URL,
    ]

    url = f"{SHEETS_API_BASE}/{SPREADSHEET_ID}/values/{SHEET_NAME}!A:G:append"
    params = {"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }
    body = {"values": [row]}

    try:
        resp = requests.post(
            url, headers=headers, params=params, json=body, timeout=15
        )
        if resp.status_code == 200:
            logger.info("  → Logged to Google Sheet")
        else:
            logger.warning(
                "  Sheet append failed (%s): %s", resp.status_code, resp.text
            )
    except requests.exceptions.RequestException as e:
        logger.warning("  Sheet append error: %s", e)


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def send_daily_summary(creds, accounts):
    """
    Fetch review counts for the 7 clinics and post a summary table to Slack.

    Covers:
      Yesterday — reviews whose createTime fell in the previous IST calendar day
      MTD       — reviews since the 1st of the current month (IST) up to yesterday
      Avg ★     — overall average from the GBP API; falls back to MTD average
    """
    now_ist = datetime.now(timezone.utc).astimezone(IST)
    today_midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_midnight = today_midnight - timedelta(days=1)
    mtd_start = today_midnight.replace(day=1)
    date_display = yesterday_midnight.strftime("%-d %b %Y")

    logger.info("Daily summary: window %s → %s", yesterday_midnight.date(), today_midnight.date())

    # Initialise per-clinic accumulators
    stats = {
        name: {
            "yesterday": 0,
            "s1": 0, "s2": 0, "s3": 0, "s4": 0, "s5": 0,
            "mtd": 0, "mtd_sum": 0, "mtd_count": 0,
            "api_avg": None,
        }
        for name in SUMMARY_CLINICS
    }

    for account in accounts:
        account_name = account["name"]
        try:
            locations = get_locations(creds, account_name)
        except requests.exceptions.HTTPError as e:
            logger.warning("Could not fetch locations for %s: %s", account_name, e)
            continue

        for location in locations:
            loc_name = location["name"]
            loc_title = location.get("title", "")
            short_name = get_clinic_short_name(loc_title)
            if not short_name:
                continue

            logger.info("  Summary fetching: %s → %s", loc_title, short_name)
            s = stats[short_name]

            # Overall average from API (all-time, as shown on GBP)
            s["api_avg"] = get_location_avg_rating(creds, account_name, loc_name)

            # Reviews since start of month (covers both Yesterday and MTD windows)
            try:
                reviews = fetch_reviews_since(creds, account_name, loc_name, mtd_start)
            except requests.exceptions.HTTPError as e:
                logger.warning("    Could not fetch reviews for %s: %s", loc_title, e)
                continue

            for review in reviews:
                ct_str = review.get("createTime", "")
                try:
                    review_dt = datetime.fromisoformat(
                        ct_str.replace("Z", "+00:00")
                    ).astimezone(IST)
                except (ValueError, AttributeError):
                    continue

                star = STAR_COUNTS.get(review.get("starRating", ""), 0)

                if mtd_start <= review_dt < today_midnight:
                    s["mtd"] += 1
                    s["mtd_sum"] += star
                    s["mtd_count"] += 1

                if yesterday_midnight <= review_dt < today_midnight:
                    s["yesterday"] += 1
                    if 1 <= star <= 5:
                        s[f"s{star}"] += 1

    # Build per-clinic rows and running totals
    clinic_rows = []
    tot = {"yesterday": 0, "s1": 0, "s2": 0, "s3": 0, "s4": 0, "s5": 0,
           "mtd": 0, "mtd_sum": 0, "mtd_count": 0}

    for name in SUMMARY_CLINICS:
        s = stats[name]

        if s["api_avg"] is not None:
            avg_display = f"{s['api_avg']:.1f}"
        elif s["mtd_count"]:
            avg_display = f"{s['mtd_sum'] / s['mtd_count']:.1f}"
        else:
            avg_display = "—"

        clinic_rows.append({
            "name": name,
            "yesterday": s["yesterday"],
            "s5": s["s5"], "s4": s["s4"], "s3": s["s3"], "s2": s["s2"], "s1": s["s1"],
            "mtd": s["mtd"],
            "avg": avg_display,
        })

        tot["yesterday"] += s["yesterday"]
        for k in (1, 2, 3, 4, 5):
            tot[f"s{k}"] += s[f"s{k}"]
        tot["mtd"] += s["mtd"]
        tot["mtd_sum"] += s["mtd_sum"]
        tot["mtd_count"] += s["mtd_count"]

    # Total average: weighted by MTD review counts; fall back to simple mean of API avgs
    if tot["mtd_count"]:
        total_avg = f"{tot['mtd_sum'] / tot['mtd_count']:.1f}"
    else:
        api_avgs = [stats[n]["api_avg"] for n in SUMMARY_CLINICS if stats[n]["api_avg"] is not None]
        total_avg = f"{sum(api_avgs) / len(api_avgs):.1f}" if api_avgs else "—"

    totals_row = {
        "name": "TOTAL",
        "yesterday": tot["yesterday"],
        "s5": tot["s5"], "s4": tot["s4"], "s3": tot["s3"], "s2": tot["s2"], "s1": tot["s1"],
        "mtd": tot["mtd"],
        "avg": total_avg,
    }

    post_daily_summary_to_slack(date_display, clinic_rows, totals_row)
    logger.info("Daily summary complete.")


def _test_daily_summary():
    """Post a dummy daily summary to verify table formatting."""
    yesterday = (datetime.now(timezone.utc).astimezone(IST) - timedelta(days=1))
    date_display = yesterday.strftime("%-d %b %Y")

    dummy_rows = [
        {"name": "HSR Layout",       "yesterday": 3, "s5": 2, "s4": 1, "s3": 0, "s2": 0, "s1": 0, "mtd": 45,  "avg": "4.8"},
        {"name": "Bellandur",        "yesterday": 0, "s5": 0, "s4": 0, "s3": 0, "s2": 0, "s1": 0, "mtd": 32,  "avg": "4.6"},
        {"name": "Varthur",          "yesterday": 1, "s5": 1, "s4": 0, "s3": 0, "s2": 0, "s1": 0, "mtd": 18,  "avg": "4.7"},
        {"name": "Kasavanahalli",    "yesterday": 0, "s5": 0, "s4": 0, "s3": 0, "s2": 0, "s1": 0, "mtd": 28,  "avg": "4.5"},
        {"name": "Electronic City",  "yesterday": 2, "s5": 1, "s4": 0, "s3": 1, "s2": 0, "s1": 0, "mtd": 37,  "avg": "4.3"},
        {"name": "Yelahanka",        "yesterday": 1, "s5": 1, "s4": 0, "s3": 0, "s2": 0, "s1": 0, "mtd": 22,  "avg": "4.9"},
        {"name": "Thanisandra",      "yesterday": 0, "s5": 0, "s4": 0, "s3": 0, "s2": 0, "s1": 0, "mtd": 15,  "avg": "4.4"},
    ]
    dummy_totals = {
        "name": "TOTAL",
        "yesterday": 7, "s5": 5, "s4": 1, "s3": 1, "s2": 0, "s1": 0, "mtd": 197, "avg": "4.6",
    }

    logger.info("Sending test daily summary to Slack...")
    post_daily_summary_to_slack(date_display, dummy_rows, dummy_totals)


def send_test_message():
    """Post dummy reviews to Slack and Google Sheet to verify the full pipeline."""
    if not SLACK_WEBHOOK_URL and not SLACK_WEBHOOK_URL_2:
        logger.error(
            "No Slack webhook configured. Set SLACK_WEBHOOK_URL or SLACK_WEBHOOK_URL_2."
        )
        sys.exit(1)

    creds = authenticate()

    test_reviews = [
        (
            "BabyMD - The Children's Clinic - HSR Layout",
            {
                "starRating": "FIVE",
                "reviewer": {"displayName": "Priya Sharma"},
                "comment": "Wonderful experience. The doctor was very patient and thorough with my daughter.",
                "createTime": datetime.now(timezone.utc).isoformat(),
            },
        ),
        (
            "Anjana Child Care by BabyMD - Electronic City",
            {
                "starRating": "THREE",
                "reviewer": {"displayName": "Amit Patel"},
                "comment": "",
                "createTime": datetime.now(timezone.utc).isoformat(),
            },
        ),
    ]

    for clinic, review in test_reviews:
        logger.info("Sending test review to Slack: %s", clinic)
        post_to_slack(clinic, review)
        logger.info("Logging test review to Google Sheet: %s", clinic)
        log_to_sheet(creds, clinic, review)

    _test_daily_summary()

    logger.info("Done — check your Slack channels and Google Sheet.")


def main():
    parser = argparse.ArgumentParser(description="GBP Reviews → Slack Alerts")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear tracking data and re-fetch last 7 days",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Post dummy reviews + daily summary to Slack to verify layout",
    )
    parser.add_argument(
        "--daily-summary",
        action="store_true",
        help="Post daily review summary table to #google-reviews and exit",
    )
    args = parser.parse_args()

    if args.test:
        send_test_message()
        return

    if args.daily_summary:
        if not SLACK_WEBHOOK_URL:
            logger.error("SLACK_WEBHOOK_URL is not set — cannot post daily summary.")
            sys.exit(1)
        creds = authenticate()
        accounts = get_accounts(creds)
        send_daily_summary(creds, accounts)
        return

    logger.info("=" * 50)
    logger.info("GBP Reviews Slack Alert — run started")

    # Step 0: Verify at least one Slack webhook is configured
    if not SLACK_WEBHOOK_URL and not SLACK_WEBHOOK_URL_2:
        logger.error(
            "No Slack webhook configured. "
            "Set SLACK_WEBHOOK_URL and/or SLACK_WEBHOOK_URL_2."
        )
        sys.exit(1)

    # Step 1: Authenticate
    creds = authenticate()

    # Step 2: Load tracking state
    state = load_processed()
    if args.reset:
        logger.info("--reset flag used: clearing tracked reviews")
        state = {"review_ids": set(), "last_run": None}

    # Determine the cutoff date for "new" reviews
    if state["last_run"]:
        cutoff = datetime.fromisoformat(state["last_run"])
        logger.info("Last run: %s — fetching reviews newer than this", state["last_run"])
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=INITIAL_LOOKBACK_DAYS)
        logger.info("First run — fetching reviews from the last %d days", INITIAL_LOOKBACK_DAYS)

    # Step 3: Discover accounts and locations
    accounts = get_accounts(creds)
    total_locations = 0
    total_new_reviews = 0

    for account in accounts:
        account_name = account["name"]
        logger.info("Account: %s (%s)", account.get("accountName", ""), account_name)

        locations = get_locations(creds, account_name)
        logger.info("  Found %d location(s):", len(locations))
        for i, loc in enumerate(locations, 1):
            logger.info(
                "    [%d/%d] %s  (id: %s)",
                i,
                len(locations),
                loc.get("title", "—"),
                loc["name"],
            )
        total_locations += len(locations)

        # Step 4: Fetch and process reviews for each location
        for location in locations:
            loc_name = location["name"]
            loc_title = location.get("title", loc_name)
            logger.info("  Checking: %s (%s)", loc_title, loc_name)

            try:
                reviews = get_reviews(creds, account_name, loc_name)
            except requests.exceptions.HTTPError as e:
                logger.warning("    Failed to fetch reviews: %s", e)
                continue

            new_count = 0
            for review in reviews:
                review_id = review.get("reviewId") or review.get("name", "")

                # Skip if already processed
                if review_id in state["review_ids"]:
                    continue

                # Parse the review creation time
                create_time_str = review.get("createTime", "")
                try:
                    review_time = datetime.fromisoformat(
                        create_time_str.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    continue

                # Skip reviews older than our cutoff
                if review_time <= cutoff:
                    continue

                # This is a new review — post to Slack and log to Sheet!
                post_to_slack(loc_title, review)
                log_to_sheet(creds, loc_title, review)
                state["review_ids"].add(review_id)
                new_count += 1

            total_new_reviews += new_count
            if new_count:
                logger.info("    → %d new review(s) posted to Slack", new_count)
            else:
                logger.info("    → No new reviews")

    # Step 5: Update last-run timestamp and save state
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_processed(state)

    # Step 6: Log summary
    logger.info("-" * 40)
    logger.info(
        "Run complete: %d location(s) checked, %d new review(s) found",
        total_locations,
        total_new_reviews,
    )
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
