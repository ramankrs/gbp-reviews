#!/usr/bin/env python3
"""
Google Business Profile Reviews → Slack Alerts

Fetches new reviews from all GBP locations under your account
and posts formatted alerts to a Slack channel via webhook.

Usage:
    python gbp_reviews.py              # Normal run
    python gbp_reviews.py --reset      # Clear tracking data and re-fetch last 7 days
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

# OAuth scope needed to read GBP data
SCOPES = ["https://www.googleapis.com/auth/business.manage"]

# Slack incoming-webhook URL — set this as an environment variable
# Example: export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

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

# Star-rating display map
STAR_MAP = {
    "STAR_RATING_UNSPECIFIED": "☆",
    "ONE": "⭐",
    "TWO": "⭐⭐",
    "THREE": "⭐⭐⭐",
    "FOUR": "⭐⭐⭐⭐",
    "FIVE": "⭐⭐⭐⭐⭐",
}

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


def post_to_slack(location_title, review):
    """Send a single formatted review alert to Slack."""
    rating = STAR_MAP.get(review.get("starRating", ""), "☆")
    reviewer = review.get("reviewer", {}).get("displayName", "Anonymous")
    comment = review.get("comment", "").strip()
    create_time = review.get("createTime", "")

    # Format the timestamp for display
    try:
        dt = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
        time_display = dt.strftime("%b %d, %Y at %I:%M %p %Z")
    except (ValueError, AttributeError):
        time_display = create_time

    # Handle reviews with no text (star-rating only)
    if comment:
        comment_line = f'💬 "{comment}"'
    else:
        comment_line = "💬 _(No written review — star rating only)_"

    message = (
        f"{rating} *New Review — {location_title}*\n"
        f"👤 {reviewer}\n"
        f"{comment_line}\n"
        f"🕐 {time_display}\n"
        f"👉 <https://business.google.com/reviews|Reply to this review>"
    )

    payload = {"text": message}
    resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=15)

    if resp.status_code != 200:
        logger.warning("Slack post failed (%s): %s", resp.status_code, resp.text)
    else:
        logger.info("  → Slack alert sent for review by %s", reviewer)


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="GBP Reviews → Slack Alerts")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear tracking data and re-fetch last 7 days",
    )
    args = parser.parse_args()

    logger.info("=" * 50)
    logger.info("GBP Reviews Slack Alert — run started")

    # Step 0: Verify Slack webhook is configured
    if not SLACK_WEBHOOK_URL:
        logger.error(
            "SLACK_WEBHOOK_URL environment variable is not set. "
            "Export it before running: export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'"
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
        logger.info("  Found %d location(s)", len(locations))
        total_locations += len(locations)

        # Step 4: Fetch and process reviews for each location
        for location in locations:
            loc_name = location["name"]
            loc_title = location.get("title", loc_name)
            logger.info("  Checking: %s", loc_title)

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

                # This is a new review — post to Slack!
                post_to_slack(loc_title, review)
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
