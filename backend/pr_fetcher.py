import re
import logging
import requests
from dataclasses import dataclass, field
from github import Github
from config import settings

logger = logging.getLogger(__name__)
gh = Github(settings.GITHUB_TOKEN)


@dataclass
class ChangedFile:
    filename: str
    status: str
    patch: str
    additions: int
    deletions: int
    previous_filename: str = ""


@dataclass
class PRData:
    pr_number: int
    title: str
    description: str
    author: str
    base_branch: str
    head_branch: str
    repo_full_name: str
    pr_url: str
    changed_files: list[ChangedFile] = field(default_factory=list)


def parse_pr_url(url: str) -> tuple[str, str, int]:
    match = re.search(r'github\.com/([^/]+)/([^/]+)/pull/(\d+)', url)
    if not match:
        raise ValueError(f"Invalid GitHub PR URL: {url}")
    return match.group(1), match.group(2), int(match.group(3))


def fetch_pr(pr_url: str) -> PRData:
    owner, repo_name, pr_number = parse_pr_url(pr_url)
    repo = gh.get_repo(f"{owner}/{repo_name}")
    pr = repo.get_pull(pr_number)

    changed_files = []
    for f in pr.get_files():
        changed_files.append(ChangedFile(
            filename=f.filename,
            status=f.status,
            patch=f.patch or "",
            additions=f.additions,
            deletions=f.deletions,
            previous_filename=getattr(f, "previous_filename", "") or ""
        ))

    return PRData(
        pr_number=pr_number,
        title=pr.title,
        description=pr.body or "",
        author=pr.user.login,
        base_branch=pr.base.ref,
        head_branch=pr.head.ref,
        repo_full_name=f"{owner}/{repo_name}",
        pr_url=pr_url,
        changed_files=changed_files
    )


def _delete_existing_pending_reviews(repo_full_name: str, pr_number: int):
    """Delete any existing pending reviews by the authenticated user so we can post a new one."""
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/reviews"
    headers = {
        "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        for review in resp.json():
            if review.get("state") == "PENDING":
                del_url = f"{url}/{review['id']}"
                requests.delete(del_url, headers=headers, timeout=15)
                logger.info(f"Deleted existing pending review {review['id']} on PR #{pr_number}")
    except Exception as e:
        logger.warning(f"Failed to clean up pending reviews: {e}")


def post_pending_review(repo_full_name: str, pr_number: int, review_body: str,
                        comments: list[dict] | None = None) -> dict:
    """
    Post a pending (draft) review on a GitHub PR with optional inline comments.
    Each comment dict should have: path, position, body.
    'position' is the line offset within the diff hunk (1-indexed from the first line of the diff).
    Omitting 'event' leaves it as PENDING — not visible to others until submitted.
    Returns the review dict from GitHub API (includes 'id' and 'html_url').
    """
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/reviews"
    headers = {
        "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # Clean up any existing pending reviews first
    _delete_existing_pending_reviews(repo_full_name, pr_number)

    payload = {"body": review_body}
    if comments:
        # GitHub API expects: path, position (or line), body
        # position is 1-indexed line within the diff hunk
        valid_comments = [
            {"path": c["path"], "position": c["position"], "body": c["body"]}
            for c in comments
            if isinstance(c.get("position"), int) and c["position"] > 0
            and c.get("path") and c.get("body")
        ]
        if valid_comments:
            payload["comments"] = valid_comments
            logger.info(f"Posting review with {len(valid_comments)} inline comments")

    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    if resp.status_code >= 400:
        logger.error(f"GitHub review API error: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    return resp.json()


def fetch_full_file(repo_full_name: str, file_path: str, ref: str) -> str | None:
    try:
        repo = gh.get_repo(repo_full_name)
        content = repo.get_contents(file_path, ref=ref)
        text = content.decoded_content.decode("utf-8", errors="ignore")
        return text[:settings.MAX_FILE_CHARS]
    except Exception as e:
        logger.debug(f"Could not fetch {file_path}@{ref}: {e}")
        return None
