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
import team_context
import draft_store
import feedback_collector

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


async def background_sync_team_reviews(repo_full_name: str):
    try:
        n = await asyncio.to_thread(team_context.fetch_and_save_team_reviews, repo_full_name)
        logger.info(f"Team review sync done for {repo_full_name}: {n} new reviews")
    except Exception as e:
        logger.error(f"Team review sync failed for {repo_full_name}: {e}")


async def background_collect_feedback(repo_full_name: str):
    try:
        n = await asyncio.to_thread(feedback_collector.collect_for_repo, repo_full_name)
        logger.info(f"Feedback collection: {n} new examples indexed for {repo_full_name}")
    except Exception as e:
        logger.error(f"Feedback collection failed for {repo_full_name}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    for repo in settings.repos_to_watch:
        status = cache_module.get_index_status(repo)
        if not status:
            logger.info(f"Auto-indexing {repo} on startup")
            asyncio.create_task(background_index(repo))
        else:
            logger.info(f"Repo {repo} already indexed (SHA: {status['sha'][:8]})")

        if settings.team_members:
            logger.info(f"Syncing team reviews for {repo} on startup")
            asyncio.create_task(background_sync_team_reviews(repo))

        # Check if any previously posted drafts have since been submitted
        asyncio.create_task(background_collect_feedback(repo))
    yield


app = FastAPI(title="PR Review Bot", lifespan=lifespan)


class ReviewRequest(BaseModel):
    pr_url: str


class IndexRequest(BaseModel):
    repo_full_name: str
    force: bool = False


class SyncTeamReviewsRequest(BaseModel):
    repo_full_name: str


class FeedbackCollectRequest(BaseModel):
    repo_full_name: str
    min_age_hours: int = 2


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

    # Post as a pending (draft) review on the PR — not submitted, only visible to you
    try:
        gh_review = await asyncio.to_thread(
            pr_fetcher.post_pending_review,
            pr_data.repo_full_name,
            pr_data.pr_number,
            review_text
        )
        review_id = gh_review.get("id")
    except Exception as e:
        logger.error(f"Failed to post pending review on GitHub: {e}")
        raise HTTPException(500, f"Review generated but failed to post to GitHub: {e}")

    # Save draft metadata so feedback_collector can detect submission later (learning loop)
    try:
        layers = list({context_builder.infer_layer(fc.changed_file) for fc in ctx.files})
        draft_store.save_draft(
            repo=pr_data.repo_full_name,
            pr_number=pr_data.pr_number,
            pr_title=pr_data.title,
            pr_description=pr_data.description,
            ai_draft_review_id=review_id,
            files_reviewed=[fc.changed_file for fc in ctx.files],
            layers=layers,
        )
    except Exception as e:
        logger.warning(f"Failed to save draft record (non-fatal): {e}")

    elapsed = round(time.time() - start, 1)
    similar_count = sum(len(f.similar_files) for f in ctx.files)

    return {
        "pr_url": req.pr_url,
        "draft_review_id": review_id,
        "files_reviewed": [f.changed_file for f in ctx.files],
        "similar_files_found": similar_count,
        "processing_time_seconds": elapsed,
        "examples_used": bool(ctx.team_review_context),
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


@app.post("/sync-team-reviews")
async def sync_team_reviews(req: SyncTeamReviewsRequest):
    if not settings.team_members:
        raise HTTPException(400, "No TEAM_MEMBERS configured in .env")
    asyncio.create_task(background_sync_team_reviews(req.repo_full_name))
    return {"status": "sync_started", "repo": req.repo_full_name, "team": settings.team_members}


@app.post("/feedback/collect")
async def collect_feedback(req: FeedbackCollectRequest):
    n = await asyncio.to_thread(
        feedback_collector.collect_for_repo, req.repo_full_name, req.min_age_hours
    )
    return {"repo": req.repo_full_name, "new_examples_indexed": n}


@app.get("/health")
async def health():
    vllm_ok = await llm_client.check_health()
    ollama_ok = await llm_client.check_ollama_health()
    indexed = cache_module.get_all_indexed_repos()
    return {
        "vllm": "up" if vllm_ok else "down",
        "ollama": "up" if ollama_ok else "down",
        "indexed_repos": indexed,
        "watched_repos": settings.repos_to_watch,
        "team_members": settings.team_members,
    }
