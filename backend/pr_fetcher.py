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


def post_pending_review(repo_full_name: str, pr_number: int, review_body: str) -> dict:
    """
    Post a pending (draft) review on a GitHub PR.
    Omitting 'event' leaves it as PENDING — not visible to others until submitted.
    Returns the review dict from GitHub API (includes 'id' and 'html_url').
    """
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/reviews"
    headers = {
        "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.post(url, json={"body": review_body}, headers=headers, timeout=30)
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
