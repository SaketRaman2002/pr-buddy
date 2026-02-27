import logging
from dataclasses import dataclass, field
from pathlib import Path
from pr_fetcher import PRData, ChangedFile, fetch_full_file
from repo_indexer import find_similar_files, get_indexable_files
from github import Github
from config import settings

logger = logging.getLogger(__name__)
gh = Github(settings.GITHUB_TOKEN)

SKIP_EXTENSIONS = {".lock", ".sum", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".ttf"}


def infer_layer(file_path: str) -> str:
    p = file_path.lower()
    if any(x in p for x in ["controller", "handler", "route", "router", "view"]):
        return "controller"
    if any(x in p for x in ["service", "usecase", "use_case"]):
        return "service"
    if any(x in p for x in ["model", "entity", "schema", "domain"]):
        return "model"
    if any(x in p for x in ["repo", "repository", "dao", "store", "db"]):
        return "repository"
    if any(x in p for x in ["util", "helper", "common", "shared", "lib"]):
        return "util"
    if any(x in p for x in ["test", "spec", "__test__"]):
        return "test"
    if any(x in p for x in ["config", "setting", "env"]):
        return "config"
    if any(x in p for x in ["middleware", "interceptor", "guard", "hook"]):
        return "middleware"
    return "unknown"


def get_directory_siblings(repo_full_name: str, file_path: str, ref: str, max_siblings: int = 3) -> list[dict]:
    try:
        repo = gh.get_repo(repo_full_name)
        directory = str(Path(file_path).parent)
        ext = Path(file_path).suffix
        contents = repo.get_contents(directory, ref=ref)
        siblings = []
        for item in contents:
            if item.type != "file":
                continue
            if item.path == file_path:
                continue
            if Path(item.name).suffix != ext:
                continue
            if Path(item.name).suffix in SKIP_EXTENSIONS:
                continue
            try:
                content = item.decoded_content.decode("utf-8", errors="ignore")
                siblings.append({"path": item.path, "content": content[:1500]})
            except Exception:
                pass
            if len(siblings) >= max_siblings:
                break
        return siblings
    except Exception as e:
        logger.warning(f"Could not fetch siblings for {file_path}: {e}")
        return []


def get_repo_structure(repo_full_name: str, ref: str) -> str:
    try:
        repo = gh.get_repo(repo_full_name)
        lines = []

        def walk(path="", depth=0):
            if depth > 2:
                return
            try:
                items = repo.get_contents(path, ref=ref)
            except Exception:
                return
            for item in sorted(items, key=lambda x: (x.type == "file", x.name)):
                indent = "  " * depth
                if item.type == "dir":
                    if item.name in {"node_modules", ".git", "dist", "build", "__pycache__", "vendor"}:
                        continue
                    lines.append(f"{indent}📁 {item.name}/")
                    walk(item.path, depth + 1)
                else:
                    if Path(item.name).suffix not in SKIP_EXTENSIONS:
                        lines.append(f"{indent}📄 {item.name}")

        walk()
        return "\n".join(lines[:200])
    except Exception as e:
        logger.warning(f"Could not get repo structure: {e}")
        return "Could not fetch repo structure"


@dataclass
class FileReviewContext:
    changed_file: str
    status: str
    diff: str
    original_content: str
    similar_files: list[dict] = field(default_factory=list)
    directory_siblings: list[dict] = field(default_factory=list)
    inferred_layer: str = "unknown"


@dataclass
class ReviewContext:
    pr_title: str
    pr_description: str
    pr_author: str
    pr_url: str
    base_branch: str
    repo_full_name: str
    repo_structure: str
    files: list[FileReviewContext] = field(default_factory=list)


def build_context(pr_data: PRData) -> ReviewContext:
    repo_structure = get_repo_structure(pr_data.repo_full_name, pr_data.base_branch)

    ctx = ReviewContext(
        pr_title=pr_data.title,
        pr_description=pr_data.description,
        pr_author=pr_data.author,
        pr_url=pr_data.pr_url,
        base_branch=pr_data.base_branch,
        repo_full_name=pr_data.repo_full_name,
        repo_structure=repo_structure
    )

    for changed_file in pr_data.changed_files:
        if Path(changed_file.filename).suffix in SKIP_EXTENSIONS:
            continue
        if len(changed_file.patch) == 0 and changed_file.status == "deleted":
            continue

        original = ""
        if changed_file.status != "added":
            original = fetch_full_file(pr_data.repo_full_name, changed_file.filename, pr_data.base_branch) or ""

        query_content = original if original else changed_file.patch

        similar_files = []
        try:
            similar_files = find_similar_files(
                pr_data.repo_full_name,
                query_content,
                changed_file.filename,
                top_k=settings.MAX_CONTEXT_FILES
            )
        except Exception as e:
            logger.warning(f"Vector search failed for {changed_file.filename}: {e}")

        siblings = get_directory_siblings(
            pr_data.repo_full_name,
            changed_file.filename,
            pr_data.base_branch
        )

        ctx.files.append(FileReviewContext(
            changed_file=changed_file.filename,
            status=changed_file.status,
            diff=changed_file.patch[:settings.MAX_DIFF_CHARS],
            original_content=original,
            similar_files=similar_files,
            directory_siblings=siblings,
            inferred_layer=infer_layer(changed_file.filename)
        ))

    return ctx
