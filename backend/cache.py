import json
import os
from pathlib import Path
from datetime import datetime
from config import settings

def _cache_path(repo_full_name: str) -> Path:
    safe_name = repo_full_name.replace("/", "__")
    return Path(settings.CACHE_DIR) / f"{safe_name}.json"

def get_index_status(repo_full_name: str) -> dict | None:
    p = _cache_path(repo_full_name)
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)

def set_index_status(repo_full_name: str, sha: str, file_count: int, chunk_count: int):
    Path(settings.CACHE_DIR).mkdir(parents=True, exist_ok=True)
    p = _cache_path(repo_full_name)
    with open(p, "w") as f:
        json.dump({
            "repo": repo_full_name,
            "sha": sha,
            "file_count": file_count,
            "chunk_count": chunk_count,
            "indexed_at": datetime.utcnow().isoformat()
        }, f, indent=2)

def is_index_fresh(repo_full_name: str, current_sha: str) -> bool:
    status = get_index_status(repo_full_name)
    if not status:
        return False
    return status.get("sha") == current_sha

def get_all_indexed_repos() -> list[dict]:
    cache_dir = Path(settings.CACHE_DIR)
    if not cache_dir.exists():
        return []
    results = []
    for f in cache_dir.glob("*.json"):
        with open(f) as fh:
            results.append(json.load(fh))
    return results
