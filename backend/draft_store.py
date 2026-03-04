import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from config import settings

logger = logging.getLogger(__name__)

DRAFTS_DIR = Path(settings.CACHE_DIR) / "drafts"


def _draft_path(repo: str, pr_number: int) -> Path:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_repo = repo.replace("/", "__")
    return DRAFTS_DIR / f"{safe_repo}__{pr_number}.json"


def save_draft(
    repo: str,
    pr_number: int,
    pr_title: str,
    pr_description: str,
    ai_draft_review_id: int,
    files_reviewed: list[str],
    layers: list[str],
) -> None:
    record = {
        "pr_number": pr_number,
        "pr_title": pr_title,
        "pr_description": pr_description,
        "repo": repo,
        "ai_draft_review_id": ai_draft_review_id,
        "files_reviewed": files_reviewed,
        "layers": layers,
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "processed": False,
    }
    path = _draft_path(repo, pr_number)
    path.write_text(json.dumps(record, indent=2))
    logger.debug(f"Saved draft record for PR #{pr_number} in {repo}")


def load_draft(repo: str, pr_number: int) -> dict | None:
    path = _draft_path(repo, pr_number)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        logger.warning(f"Failed to load draft for PR #{pr_number}: {e}")
        return None


def get_pending_drafts(repo: str) -> list[dict]:
    """Return all unprocessed draft records for a repo."""
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_repo = repo.replace("/", "__")
    results = []
    for path in DRAFTS_DIR.glob(f"{safe_repo}__*.json"):
        try:
            record = json.loads(path.read_text())
            if not record.get("processed", False):
                results.append(record)
        except Exception as e:
            logger.warning(f"Could not read draft file {path}: {e}")
    return results


def mark_processed(repo: str, pr_number: int) -> None:
    draft = load_draft(repo, pr_number)
    if draft is None:
        return
    draft["processed"] = True
    _draft_path(repo, pr_number).write_text(json.dumps(draft, indent=2))
    logger.debug(f"Marked draft PR #{pr_number} as processed for {repo}")
