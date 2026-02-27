import asyncio
import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from config import settings
import cache as cache_module
import pr_fetcher
import context_builder
import prompt_builder
import llm_client
import repo_indexer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

indexing_status: dict[str, str] = {}


async def background_index(repo_full_name: str, force: bool = False):
    indexing_status[repo_full_name] = "indexing"
    try:
        result = await asyncio.to_thread(repo_indexer.build_index, repo_full_name, force)
        indexing_status[repo_full_name] = "done"
        logger.info(f"Indexing complete for {repo_full_name}: {result}")
    except Exception as e:
        indexing_status[repo_full_name] = f"error: {str(e)}"
        logger.error(f"Indexing failed for {repo_full_name}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    for repo in settings.repos_to_watch:
        status = cache_module.get_index_status(repo)
        if not status:
            logger.info(f"Auto-indexing {repo} on startup")
            asyncio.create_task(background_index(repo))
        else:
            logger.info(f"Repo {repo} already indexed (SHA: {status['sha'][:8]})")
    yield


app = FastAPI(title="PR Review Bot", lifespan=lifespan)


class ReviewRequest(BaseModel):
    pr_url: str


class IndexRequest(BaseModel):
    repo_full_name: str
    force: bool = False


@app.post("/review")
async def review_pr(req: ReviewRequest):
    if "github.com" not in req.pr_url or "/pull/" not in req.pr_url:
        raise HTTPException(400, "Invalid GitHub PR URL")

    if not await llm_client.check_health():
        raise HTTPException(503, "vLLM is not running at " + settings.VLLM_BASE_URL)

    start = time.time()
    logger.info(f"Starting review for {req.pr_url}")

    try:
        pr_data = await asyncio.to_thread(pr_fetcher.fetch_pr, req.pr_url)
    except Exception as e:
        raise HTTPException(400, f"Failed to fetch PR: {e}")

    try:
        ctx = await asyncio.to_thread(context_builder.build_context, pr_data)
    except Exception as e:
        raise HTTPException(500, f"Failed to build context: {e}")

    prompt = prompt_builder.build_prompt(ctx)

    try:
        review_text = await llm_client.review(prompt)
    except Exception as e:
        raise HTTPException(500, f"LLM call failed: {e}")

    elapsed = round(time.time() - start, 1)
    similar_count = sum(len(f.similar_files) for f in ctx.files)

    return {
        "review": review_text,
        "pr_url": req.pr_url,
        "files_reviewed": [f.changed_file for f in ctx.files],
        "similar_files_found": similar_count,
        "processing_time_seconds": elapsed
    }


@app.post("/index")
async def index_repo(req: IndexRequest):
    if req.repo_full_name not in settings.repos_to_watch:
        raise HTTPException(400, f"{req.repo_full_name} not in GITHUB_REPOS_TO_WATCH")
    asyncio.create_task(background_index(req.repo_full_name, req.force))
    return {"status": "indexing_started", "repo": req.repo_full_name}


@app.get("/index/status/{owner}/{repo}")
async def index_status(owner: str, repo: str):
    full_name = f"{owner}/{repo}"
    cached = cache_module.get_index_status(full_name)
    live_status = indexing_status.get(full_name, "idle")
    return {"repo": full_name, "cached": cached, "live_status": live_status}


@app.get("/health")
async def health():
    vllm_ok = await llm_client.check_health()
    ollama_ok = await llm_client.check_ollama_health()
    indexed = cache_module.get_all_indexed_repos()
    return {
        "vllm": "up" if vllm_ok else "down",
        "ollama": "up" if ollama_ok else "down",
        "indexed_repos": indexed,
        "watched_repos": settings.repos_to_watch
    }
