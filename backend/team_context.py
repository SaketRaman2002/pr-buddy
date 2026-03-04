import json
import logging
from pathlib import Path
from github import Github
from config import settings

logger = logging.getLogger(__name__)
gh = Github(settings.GITHUB_TOKEN)

TEAM_REVIEWS_DIR = Path(settings.CACHE_DIR) / "team_reviews"


def _reviews_file(repo_full_name: str) -> Path:
    TEAM_REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = repo_full_name.replace("/", "__")
    return TEAM_REVIEWS_DIR / f"{safe_name}.json"


def _load_reviews(repo_full_name: str) -> list:
    f = _reviews_file(repo_full_name)
    if not f.exists():
        return []
    try:
        with open(f) as fp:
            return json.load(fp)
    except Exception:
        return []


def fetch_and_save_team_reviews(repo_full_name: str, max_prs: int = 30) -> int:
    """
    Fetch recent PR reviews by configured team members from a repo and cache them.
    Returns the number of new reviews saved.
    """
    team = settings.team_members
    if not team:
        logger.info("No TEAM_MEMBERS configured, skipping team review sync")
        return 0

    try:
        repo = gh.get_repo(repo_full_name)
        pulls = repo.get_pulls(state="closed", sort="updated", direction="desc")

        reviews_data = []
        prs_checked = 0
        for pr in pulls:
            if prs_checked >= max_prs:
                break
            if not pr.merged:
                prs_checked += 1
                continue

            try:
                for review in pr.get_reviews():
                    if review.user.login not in team:
                        continue
                    if not review.body:
                        continue
                    files = [f.filename for f in pr.get_files()]
                    reviews_data.append({
                        "repo": repo_full_name,
                        "pr_number": pr.number,
                        "pr_title": pr.title,
                        "pr_author": pr.user.login,
                        "reviewer": review.user.login,
                        "state": review.state,
                        "body": review.body[:2000],
                        "files": files[:10],
                        "submitted_at": review.submitted_at.isoformat() if review.submitted_at else "",
                    })
            except Exception as e:
                logger.warning(f"Could not fetch reviews for PR #{pr.number}: {e}")

            prs_checked += 1

        # Merge with existing, deduplicate by (pr_number, reviewer)
        existing = _load_reviews(repo_full_name)
        existing_keys = {(r["pr_number"], r["reviewer"]) for r in existing}
        new_reviews = [r for r in reviews_data if (r["pr_number"], r["reviewer"]) not in existing_keys]
        merged = existing + new_reviews
        merged = merged[-200:]  # cap at 200 reviews

        with open(_reviews_file(repo_full_name), "w") as fp:
            json.dump(merged, fp, indent=2)

        logger.info(f"Team review sync for {repo_full_name}: {len(new_reviews)} new (total {len(merged)})")

        # Index all reviews into ChromaDB for semantic retrieval.
        # Deferred import avoids circular dependency (review_examples imports team_context._load_reviews).
        try:
            import review_examples
            review_examples.index_team_reviews(repo_full_name)
        except Exception as e:
            logger.warning(f"Failed to index team reviews into ChromaDB: {e}")

        return len(new_reviews)

    except Exception as e:
        logger.error(f"Failed to fetch team reviews for {repo_full_name}: {e}")
        return 0


def get_team_review_context(repo_full_name: str, max_reviews: int = 5) -> str:
    """
    Return a formatted string of recent team reviews to inject as context.
    Returns empty string if no reviews are cached.
    """
    reviews = _load_reviews(repo_full_name)
    if not reviews:
        return ""

    recent = reviews[-max_reviews:]
    lines = ["## HOW YOUR TEAM REVIEWS CODE (use these as style reference):\n"]
    for r in recent:
        lines.append(f"### PR #{r['pr_number']}: {r['pr_title']} (author: {r['pr_author']})")
        lines.append(f"Reviewer: **{r['reviewer']}** | Verdict: {r['state']}")
        if r["files"]:
            lines.append(f"Files changed: {', '.join(r['files'][:5])}")
        lines.append(f"\n{r['body']}\n")
        lines.append("---")

    return "\n".join(lines)
