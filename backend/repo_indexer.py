import ast
import re
import logging
import httpx
import chromadb
from pathlib import Path
from git import Repo, InvalidGitRepositoryError
from typing import Optional
from config import settings
import cache as cache_module

logger = logging.getLogger(__name__)

INCLUDE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".rs", ".cpp", ".c", ".cs", ".rb", ".swift", ".kt"}

SKIP_PATTERNS = [
    "node_modules", ".git", "dist", "build", "__pycache__",
    ".next", "vendor", "coverage", "migrations", ".turbo",
    ".gradle", "target", "out", ".idea", ".vscode"
]

SKIP_SUFFIXES = [".min.js", ".lock", ".sum", ".pb.go", ".generated.ts", ".d.ts"]


def should_skip(path: Path) -> bool:
    parts = set(path.parts)
    for p in SKIP_PATTERNS:
        if p in parts:
            return True
    for s in SKIP_SUFFIXES:
        if path.name.endswith(s):
            return True
    return False


def clone_or_pull(repo_full_name: str) -> Path:
    owner, repo_name = repo_full_name.split("/")
    repo_path = Path(settings.REPOS_DIR) / owner / repo_name
    repo_path.mkdir(parents=True, exist_ok=True)

    if (repo_path / ".git").exists():
        logger.info(f"Pulling latest for {repo_full_name}")
        r = Repo(repo_path)
        r.remotes.origin.pull()
    else:
        logger.info(f"Cloning {repo_full_name}")
        clone_url = f"https://{settings.GITHUB_TOKEN}@github.com/{repo_full_name}.git"
        Repo.clone_from(clone_url, repo_path)

    return repo_path


def get_indexable_files(repo_path: Path) -> list[Path]:
    result = []
    for p in sorted(repo_path.rglob("*")):
        if not p.is_file():
            continue
        if should_skip(p.relative_to(repo_path)):
            continue
        if p.suffix not in INCLUDE_EXTENSIONS:
            continue
        result.append(p)
    return result


def chunk_file(file_path: Path, repo_path: Path) -> list[dict]:
    try:
        content = file_path.read_text(errors="ignore")
    except Exception:
        return []

    lines = content.splitlines()
    relative = str(file_path.relative_to(repo_path))

    if len(lines) < 150:
        return [{
            "chunk_id": f"{relative}::chunk_0",
            "file_path": relative,
            "content": content,
            "start_line": 0,
            "end_line": len(lines),
            "chunk_type": "full_file",
            "symbol_name": ""
        }]

    chunks = []
    ext = file_path.suffix

    if ext == ".py":
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.col_offset == 0:
                        start = node.lineno - 1
                        end = node.end_lineno
                        chunk_content = "\n".join(lines[start:end])
                        chunks.append({
                            "chunk_id": f"{relative}::chunk_{len(chunks)}",
                            "file_path": relative,
                            "content": chunk_content,
                            "start_line": start,
                            "end_line": end,
                            "chunk_type": "class" if isinstance(node, ast.ClassDef) else "function",
                            "symbol_name": node.name
                        })
        except SyntaxError:
            pass

    elif ext in {".ts", ".tsx", ".js", ".jsx"}:
        pattern = re.compile(r'^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:class|function|const|interface|type|enum)\s+(\w+)', re.MULTILINE)
        matches = list(pattern.finditer(content))
        for i, m in enumerate(matches):
            start_line = content[:m.start()].count("\n")
            end_line = content[:matches[i+1].start()].count("\n") if i+1 < len(matches) else len(lines)
            chunks.append({
                "chunk_id": f"{relative}::chunk_{len(chunks)}",
                "file_path": relative,
                "content": "\n".join(lines[start_line:end_line]),
                "start_line": start_line,
                "end_line": end_line,
                "chunk_type": "block",
                "symbol_name": m.group(1)
            })

    elif ext == ".go":
        pattern = re.compile(r'^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(', re.MULTILINE)
        matches = list(pattern.finditer(content))
        for i, m in enumerate(matches):
            start_line = content[:m.start()].count("\n")
            end_line = content[:matches[i+1].start()].count("\n") if i+1 < len(matches) else len(lines)
            chunks.append({
                "chunk_id": f"{relative}::chunk_{len(chunks)}",
                "file_path": relative,
                "content": "\n".join(lines[start_line:end_line]),
                "start_line": start_line,
                "end_line": end_line,
                "chunk_type": "function",
                "symbol_name": m.group(1)
            })

    if not chunks:
        size = 100
        overlap = 20
        i = 0
        while i < len(lines):
            end = min(i + size, len(lines))
            chunks.append({
                "chunk_id": f"{relative}::chunk_{len(chunks)}",
                "file_path": relative,
                "content": "\n".join(lines[i:end]),
                "start_line": i,
                "end_line": end,
                "chunk_type": "block",
                "symbol_name": ""
            })
            i += size - overlap

    return chunks


def get_structural_signature(content: str, file_path: str) -> str:
    ext = Path(file_path).suffix
    sig_parts = [f"FILE: {file_path}"]

    if ext == ".py":
        imports = re.findall(r'^(?:import|from)\s+(.+)', content, re.MULTILINE)
        if imports:
            sig_parts.append(f"IMPORTS: {', '.join(imports[:15])}")
        try:
            tree = ast.parse(content)
            exports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.col_offset == 0:
                    bases = [ast.unparse(b) for b in node.bases] if node.bases else []
                    base_str = f" ({', '.join(bases)})" if bases else ""
                    exports.append(f"CLASS: {node.name}{base_str}")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.col_offset == 0:
                    args = [a.arg for a in node.args.args]
                    exports.append(f"FUNCTION: {node.name}({', '.join(args)})")
            if exports:
                sig_parts.append("EXPORTS: " + ", ".join(exports[:15]))
        except SyntaxError:
            pass
        decorators = re.findall(r'^@(\w+)', content, re.MULTILINE)
        if decorators:
            sig_parts.append(f"DECORATORS: {', '.join(set(decorators))}")

    elif ext in {".ts", ".tsx", ".js", ".jsx"}:
        imports = re.findall(r'^import\s+.+\s+from\s+[\'"]([^\'"]+)[\'"]', content, re.MULTILINE)
        if imports:
            sig_parts.append(f"IMPORTS: {', '.join(imports[:15])}")
        classes = re.findall(r'(?:export\s+)?(?:abstract\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?(?:\s+implements\s+([\w,\s]+))?', content)
        for cls in classes[:5]:
            name, extends, implements = cls
            parts = [f"CLASS: {name}"]
            if extends:
                parts.append(f"extends {extends}")
            if implements:
                parts.append(f"implements {implements.strip()}")
            sig_parts.append(" ".join(parts))
        funcs = re.findall(r'(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)', content)
        for name, args in funcs[:10]:
            sig_parts.append(f"FUNCTION: {name}({args[:60]})")
        exports = re.findall(r'^export\s+(?:const|let|type|interface|enum)\s+(\w+)', content, re.MULTILINE)
        if exports:
            sig_parts.append(f"EXPORTS: {', '.join(exports[:10])}")
        decorators = re.findall(r'@(\w+)\s*\(', content)
        if decorators:
            sig_parts.append(f"DECORATORS: {', '.join(set(decorators))}")

    elif ext == ".go":
        pkg = re.search(r'^package\s+(\w+)', content, re.MULTILINE)
        if pkg:
            sig_parts.append(f"PACKAGE: {pkg.group(1)}")
        imports = re.findall(r'"([^"]+)"', content[:500])
        if imports:
            sig_parts.append(f"IMPORTS: {', '.join(imports[:10])}")
        structs = re.findall(r'type\s+(\w+)\s+struct', content)
        interfaces = re.findall(r'type\s+(\w+)\s+interface', content)
        funcs = re.findall(r'^func\s+(?:\(\w+\s+\*?(\w+)\)\s+)?(\w+)\s*\(([^)]*)\)', content, re.MULTILINE)
        if structs:
            sig_parts.append(f"STRUCTS: {', '.join(structs[:8])}")
        if interfaces:
            sig_parts.append(f"INTERFACES: {', '.join(interfaces[:8])}")
        for recv, name, args in funcs[:10]:
            prefix = f"({recv}) " if recv else ""
            sig_parts.append(f"FUNC: {prefix}{name}({args[:60]})")

    return "\n".join(sig_parts)


def get_embedding(text: str) -> list[float]:
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{settings.OLLAMA_BASE_URL}/api/embeddings",
            json={"model": settings.EMBEDDING_MODEL, "prompt": text}
        )
        resp.raise_for_status()
        return resp.json()["embedding"]


def get_chroma_collection(repo_full_name: str) -> chromadb.Collection:
    client = chromadb.PersistentClient(path=settings.CHROMA_PERSIST_DIR)
    collection_name = repo_full_name.replace("/", "_").replace("-", "_")
    return client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"}
    )


def build_index(repo_full_name: str, force_rebuild: bool = False) -> dict:
    logger.info(f"Starting index for {repo_full_name}")
    repo_path = clone_or_pull(repo_full_name)

    git_repo = Repo(repo_path)
    current_sha = git_repo.head.commit.hexsha

    if not force_rebuild and cache_module.is_index_fresh(repo_full_name, current_sha):
        logger.info(f"Index is fresh for {repo_full_name}, skipping")
        return cache_module.get_index_status(repo_full_name)

    files = get_indexable_files(repo_path)
    logger.info(f"Found {len(files)} indexable files in {repo_full_name}")

    collection = get_chroma_collection(repo_full_name)

    all_ids, all_embeddings, all_documents, all_metadatas = [], [], [], []
    total_chunks = 0

    for i, file_path in enumerate(files):
        if i % 50 == 0:
            logger.info(f"Indexing {i}/{len(files)} files...")

        chunks = chunk_file(file_path, repo_path)
        for chunk in chunks:
            sig = get_structural_signature(chunk["content"], chunk["file_path"])
            try:
                embedding = get_embedding(sig)
            except Exception as e:
                logger.warning(f"Embedding failed for {chunk['file_path']}: {e}")
                continue

            all_ids.append(chunk["chunk_id"])
            all_embeddings.append(embedding)
            all_documents.append(sig)
            all_metadatas.append({
                "file_path": chunk["file_path"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
                "chunk_type": chunk["chunk_type"],
                "symbol_name": chunk["symbol_name"],
                "sha": current_sha
            })
            total_chunks += 1

        if len(all_ids) >= 100:
            collection.upsert(ids=all_ids, embeddings=all_embeddings, documents=all_documents, metadatas=all_metadatas)
            all_ids, all_embeddings, all_documents, all_metadatas = [], [], [], []

    if all_ids:
        collection.upsert(ids=all_ids, embeddings=all_embeddings, documents=all_documents, metadatas=all_metadatas)

    cache_module.set_index_status(repo_full_name, current_sha, len(files), total_chunks)
    logger.info(f"Indexed {len(files)} files, {total_chunks} chunks for {repo_full_name}")
    return cache_module.get_index_status(repo_full_name)


def find_similar_files(repo_full_name: str, query_content: str, query_file_path: str, top_k: int = 8) -> list[dict]:
    sig = get_structural_signature(query_content, query_file_path)
    query_embedding = get_embedding(sig)

    collection = get_chroma_collection(repo_full_name)
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(top_k * 3, 30),
        include=["metadatas", "distances", "documents"]
    )

    seen_files = {query_file_path}
    similar = []

    for meta, dist, doc in zip(results["metadatas"][0], results["distances"][0], results["documents"][0]):
        fp = meta["file_path"]
        if fp in seen_files:
            continue
        seen_files.add(fp)

        repo_path = Path(settings.REPOS_DIR) / repo_full_name.split("/")[0] / repo_full_name.split("/")[1]
        full_path = repo_path / fp
        content = ""
        if full_path.exists():
            try:
                content = full_path.read_text(errors="ignore")[:settings.MAX_FILE_CHARS]
            except Exception:
                pass

        similar.append({
            "file_path": fp,
            "similarity_score": round(1 - dist, 3),
            "structural_signature": doc,
            "content": content
        })

        if len(similar) >= top_k:
            break

    return similar
