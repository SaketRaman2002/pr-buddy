import logging
import requests
from datetime import datetime, timezone, timedelta
from config import settings
import draft_store
import review_examples

logger = logging.getLogger(__name__)


def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def check_submitted(repo: str, pr_number: int, review_id: int) -> dict | None:
    """
    Fetch all reviews for the PR and look for our review_id.
    If its state is no longer PENDING it was submitted by the user.
    Returns {"submitted_body": str, "verdict": str} or None if still pending.
    """
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"
    try:
        resp = requests.get(url, headers=_gh_headers(), timeout=15)
        resp.raise_for_status()
        reviews = resp.json()
    except Exception as e:
        logger.warning(f"Could not fetch reviews for PR #{pr_number} in {repo}: {e}")
        return None

    for review in reviews:
        if review.get("id") != review_id:
            continue
        state = review.get("state", "")
        if state == "PENDING":
            return None  # still a draft, not submitted yet
        # APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED → was submitted
        return {
            "submitted_body": review.get("body") or "",
            "verdict": state,
        }

    # Our review_id not in the list — PR may have been closed or review dismissed
    logger.info(f"Review {review_id} not found for PR #{pr_number} in {repo}")
    return None


def collect_for_repo(repo_full_name: str, min_age_hours: int = 2) -> int:
    """
    Check all pending drafts for the repo. For each old enough, poll GitHub.
    If submitted, embed it as a 'self' example and mark processed.
    Returns count of newly processed submissions.
    """
    pending = draft_store.get_pending_drafts(repo_full_name)
    if not pending:
        return 0

    now = datetime.now(timezone.utc)
    processed_count = 0

    for draft in pending:
        # Age check — give the user time to read and edit the draft before we capture it
        try:
            posted_at = datetime.fromisoformat(draft["posted_at"])
            if (now - posted_at) < timedelta(hours=min_age_hours):
                logger.debug(f"Draft PR #{draft['pr_number']} too recent, skipping")
                continue
        except Exception:
            pass  # malformed posted_at — process it anyway

        result = check_submitted(
            repo=repo_full_name,
            pr_number=draft["pr_number"],
            review_id=draft["ai_draft_review_id"],
        )
        if result is None:
            continue

        logger.info(
            f"Detected submission of PR #{draft['pr_number']} in {repo_full_name}: {result['verdict']}"
        )
        try:
            review_examples.add_submitted_review(
                repo_full_name=repo_full_name,
                pr_number=draft["pr_number"],
                pr_title=draft["pr_title"],
                files_changed=draft.get("files_reviewed", []),
                layers=draft.get("layers", []),
                submitted_body=result["submitted_body"],
                verdict=result["verdict"],
                pr_description=draft.get("pr_description", ""),
            )
        except Exception as e:
            logger.error(f"Failed to index submitted review for PR #{draft['pr_number']}: {e}")
            continue

        draft_store.mark_processed(repo_full_name, draft["pr_number"])
        processed_count += 1

    return processed_count
