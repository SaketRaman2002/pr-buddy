import logging
import chromadb
from config import settings
from repo_indexer import get_embedding

logger = logging.getLogger(__name__)


def _collection_name(repo_full_name: str) -> str:
    # "owner/repo" → "owner__repo__reviews"
    # Double underscore avoids collision with code collections ("owner_repo")
    return repo_full_name.replace("/", "__").replace("-", "_") + "__reviews"


def get_review_collection(repo_full_name: str) -> chromadb.Collection:
    client = chromadb.PersistentClient(path=settings.CHROMA_PERSIST_DIR)
    return client.get_or_create_collection(
        name=_collection_name(repo_full_name),
        metadata={"hnsw:space": "cosine"},
    )


def _build_pr_context_str(
    pr_title: str,
    files_changed: list[str],
    layers: list[str],
    pr_description: str = "",
) -> str:
    files_str = ", ".join(files_changed[:15])  # cap to stay within nomic-embed-text token limit
    layers_str = ", ".join(sorted(set(layers)))
    desc_snippet = pr_description[:200].replace("\n", " ")
    return (
        f"PR: {pr_title}\n"
        f"FILES: {files_str}\n"
        f"LAYERS: {layers_str}\n"
        f"DESCRIPTION: {desc_snippet}"
    )


def _trim_review_body(body: str, max_words: int = 200) -> str:
    words = body.split()
    if len(words) <= max_words:
        return body
    return " ".join(words[:max_words]) + "..."


def add_review_example(
    repo_full_name: str,
    pr_number: int,
    pr_title: str,
    files_changed: list[str],
    layers: list[str],
    reviewer: str,
    source: str,         # "team" or "self"
    verdict: str,
    review_body: str,
    pr_description: str = "",
) -> None:
    doc_id = f"{pr_number}_{reviewer}"
    context_str = _build_pr_context_str(pr_title, files_changed, layers, pr_description)

    try:
        embedding = get_embedding(context_str)
    except Exception as e:
        logger.warning(f"Embedding failed for PR #{pr_number}: {e}")
        return

    review_summary = _trim_review_body(review_body, max_words=200)

    # ChromaDB metadata values must be str/int/float/bool — no lists
    collection = get_review_collection(repo_full_name)
    collection.upsert(
        ids=[doc_id],
        embeddings=[embedding],
        documents=[context_str],
        metadatas=[{
            "pr_number": pr_number,
            "pr_title": pr_title,
            "repo": repo_full_name,
            "reviewer": reviewer,
            "source": source,
            "verdict": verdict,
            "review_summary": review_summary,
        }],
    )
    logger.debug(f"Upserted review example {doc_id} for {repo_full_name}")


def find_similar_reviews(
    repo_full_name: str,
    pr_title: str,
    files_changed: list[str],
    layers: list[str],
    pr_description: str = "",
    top_k: int = 2,
) -> list[dict]:
    collection = get_review_collection(repo_full_name)

    # Guard: ChromaDB raises if you query an empty collection
    if collection.count() == 0:
        return []

    context_str = _build_pr_context_str(pr_title, files_changed, layers, pr_description)
    try:
        embedding = get_embedding(context_str)
    except Exception as e:
        logger.warning(f"Embedding failed during review retrieval: {e}")
        return []

    results = collection.query(
        query_embeddings=[embedding],
        n_results=min(top_k, collection.count()),
        include=["metadatas", "distances"],
    )

    output = []
    for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
        output.append({
            "pr_title": meta["pr_title"],
            "reviewer": meta["reviewer"],
            "source": meta["source"],
            "verdict": meta["verdict"],
            "review_summary": meta["review_summary"],
            "similarity_score": round(1 - dist, 3),
        })
    return output


def index_team_reviews(repo_full_name: str) -> int:
    """
    Load team reviews from the JSON cache written by team_context.py and
    upsert them all into the review examples ChromaDB collection.
    Safe to call repeatedly — upsert is idempotent.
    Returns count of reviews processed.
    """
    # Deferred imports to avoid circular dependency:
    # review_examples ← context_builder (infer_layer) ← review_examples
    from team_context import _load_reviews
    from context_builder import infer_layer

    reviews = _load_reviews(repo_full_name)
    if not reviews:
        logger.info(f"No team reviews to index for {repo_full_name}")
        return 0

    count = 0
    for r in reviews:
        files = r.get("files", [])
        layers = list({infer_layer(f) for f in files})
        add_review_example(
            repo_full_name=repo_full_name,
            pr_number=r["pr_number"],
            pr_title=r["pr_title"],
            files_changed=files,
            layers=layers,
            reviewer=r["reviewer"],
            source="team",
            verdict=r["state"],
            review_body=r["body"],
            pr_description="",  # not stored in team reviews JSON
        )
        count += 1

    logger.info(f"Indexed {count} team reviews into ChromaDB for {repo_full_name}")
    return count


def add_submitted_review(
    repo_full_name: str,
    pr_number: int,
    pr_title: str,
    files_changed: list[str],
    layers: list[str],
    submitted_body: str,
    verdict: str,
    pr_description: str = "",
) -> None:
    """Add the user's own submitted review as a 'self' source example."""
    add_review_example(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        pr_title=pr_title,
        files_changed=files_changed,
        layers=layers,
        reviewer="self",
        source="self",
        verdict=verdict,
        review_body=submitted_body,
        pr_description=pr_description,
    )
    logger.info(f"Added self-submitted review for PR #{pr_number} in {repo_full_name}")
