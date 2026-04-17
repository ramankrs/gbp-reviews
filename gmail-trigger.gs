/**
 * GBP Review Email → GitHub Actions Trigger
 *
 * Watches Gmail for Google Business Profile review notifications
 * and triggers the fetch-reviews workflow when one is detected.
 *
 * Setup:
 *   1. Paste this file into Google Apps Script (script.google.com)
 *   2. Fill in GITHUB_TOKEN and GITHUB_BRANCH below
 *   3. Run setupTrigger() once to install the 5-minute schedule
 *   4. Run checkReviewEmails() once manually to authorise Gmail access
 */

// ---------------------------------------------------------------------------
// Configuration — fill these in before running
// ---------------------------------------------------------------------------

var CONFIG = {
  // GitHub Personal Access Token (needs repo + workflow scope)
  // Create one at: https://github.com/settings/tokens/new
  GITHUB_TOKEN: "YOUR_GITHUB_PAT_HERE",

  // GitHub repo and workflow details
  GITHUB_OWNER: "ramankrs",
  GITHUB_REPO:  "gbp-reviews",
  GITHUB_WORKFLOW_FILE: "fetch-reviews.yml",

  // The branch where your workflow file lives
  GITHUB_BRANCH: "claude/google-reviews-slack-alerts-NWjvp",

  // Gmail sender address for GBP review notifications
  REVIEW_SENDER: "businessprofile-noreply@google.com",

  // Subject phrase to match (case-insensitive in Gmail search)
  SUBJECT_PHRASE: "left a review",

  // Gmail label applied to processed emails (prevents duplicate triggers)
  PROCESSED_LABEL: "GBP-Processed",

  // Only look at emails from the last N minutes
  // Should be slightly longer than your trigger interval (5 min) to avoid gaps
  LOOKBACK_MINUTES: 10,
};

// ---------------------------------------------------------------------------
// Main function — called every 5 minutes by the time-based trigger
// ---------------------------------------------------------------------------

function checkReviewEmails() {
  var startTime = new Date();
  Logger.log("=== GBP Review Email Check — " + startTime.toISOString() + " ===");

  // Ensure the processed label exists (creates it if not)
  var label = getOrCreateLabel(CONFIG.PROCESSED_LABEL);

  // Search Gmail for unread review notification emails
  var query = buildSearchQuery();
  Logger.log("Gmail query: " + query);

  var threads = GmailApp.search(query);
  Logger.log("Threads found: " + threads.length);

  if (threads.length === 0) {
    Logger.log("No new review emails — nothing to do.");
    Logger.log("=== Done ===");
    return;
  }

  // Trigger the GitHub Actions workflow once for this batch
  Logger.log("Review email(s) detected — triggering GitHub Actions workflow...");
  var triggered = triggerGitHubWorkflow();

  if (triggered) {
    Logger.log("Workflow triggered successfully.");

    // Mark every matched thread as processed so we don't fire again
    for (var i = 0; i < threads.length; i++) {
      var thread = threads[i];
      var subject = thread.getFirstMessageSubject();

      thread.addLabel(label);
      thread.markRead();

      Logger.log("Marked as processed: \"" + subject + "\"");
    }
  } else {
    Logger.log("WARNING: Workflow trigger failed — emails left unprocessed for retry.");
  }

  var elapsed = (new Date() - startTime) / 1000;
  Logger.log("Run complete in " + elapsed.toFixed(1) + "s");
  Logger.log("=== Done ===");
}

// ---------------------------------------------------------------------------
// GitHub API — trigger workflow_dispatch event
// ---------------------------------------------------------------------------

function triggerGitHubWorkflow() {
  var url = "https://api.github.com/repos/"
    + CONFIG.GITHUB_OWNER + "/"
    + CONFIG.GITHUB_REPO
    + "/actions/workflows/"
    + CONFIG.GITHUB_WORKFLOW_FILE
    + "/dispatches";

  var payload = JSON.stringify({ ref: CONFIG.GITHUB_BRANCH });

  var options = {
    method: "post",
    contentType: "application/json",
    headers: {
      "Authorization": "Bearer " + CONFIG.GITHUB_TOKEN,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    payload: payload,
    muteHttpExceptions: true, // so we can read error responses
  };

  try {
    var response = UrlFetchApp.fetch(url, options);
    var status = response.getResponseCode();

    // 204 No Content = success for workflow_dispatch
    if (status === 204) {
      return true;
    }

    Logger.log("GitHub API error " + status + ": " + response.getContentText());
    return false;

  } catch (e) {
    Logger.log("GitHub API request failed: " + e.message);
    return false;
  }
}

// ---------------------------------------------------------------------------
// Gmail helpers
// ---------------------------------------------------------------------------

function buildSearchQuery() {
  // Calculate the cutoff timestamp Gmail accepts (YYYY/MM/DD)
  // Gmail's newer_than operator only accepts integer days/hours/minutes
  // so we use it for a rough pre-filter, then rely on the label for exactness
  var query = [
    "from:" + CONFIG.REVIEW_SENDER,
    "subject:\"" + CONFIG.SUBJECT_PHRASE + "\"",
    "is:unread",
    "newer_than:" + CONFIG.LOOKBACK_MINUTES + "m",
    "-label:" + CONFIG.PROCESSED_LABEL,
  ].join(" ");

  return query;
}

function getOrCreateLabel(labelName) {
  // Return existing label or create a new one
  var existing = GmailApp.getUserLabelByName(labelName);
  if (existing) {
    return existing;
  }
  Logger.log("Creating Gmail label: " + labelName);
  return GmailApp.createLabel(labelName);
}

// ---------------------------------------------------------------------------
// Trigger setup — run this function ONCE from the Apps Script editor
// ---------------------------------------------------------------------------

/**
 * Installs a 5-minute recurring trigger for checkReviewEmails().
 * Run this manually one time — it sets up the automatic schedule.
 * Safe to re-run: removes old triggers first to avoid duplicates.
 */
function setupTrigger() {
  // Remove any existing triggers for this function to avoid duplicates
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === "checkReviewEmails") {
      ScriptApp.deleteTrigger(triggers[i]);
      Logger.log("Removed existing trigger.");
    }
  }

  // Create a new 5-minute trigger
  ScriptApp.newTrigger("checkReviewEmails")
    .timeBased()
    .everyMinutes(5)
    .create();

  Logger.log("5-minute trigger created for checkReviewEmails().");
  Logger.log("The script will now run automatically every 5 minutes.");
}

/**
 * Removes all triggers for checkReviewEmails().
 * Run this if you want to pause or stop the automation.
 */
function removeTrigger() {
  var triggers = ScriptApp.getProjectTriggers();
  var removed = 0;
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === "checkReviewEmails") {
      ScriptApp.deleteTrigger(triggers[i]);
      removed++;
    }
  }
  Logger.log("Removed " + removed + " trigger(s).");
}
