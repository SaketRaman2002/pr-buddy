import httpx
import logging
from config import settings

logger = logging.getLogger(__name__)


async def review(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{settings.VLLM_BASE_URL}/v1/chat/completions",
            json={
                "model": settings.VLLM_MODEL_NAME,
                "messages": [
                    {"role": "system", "content": "You are an expert code reviewer. Be precise, specific, and reference exact files and line numbers where possible."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.15,
                "max_tokens": 4096
            }
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def check_health() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            # Support both vLLM (/health) and Ollama (/api/tags)
            resp = await client.get(f"{settings.VLLM_BASE_URL}/api/tags")
            if resp.status_code == 200:
                return True
            resp2 = await client.get(f"{settings.VLLM_BASE_URL}/health")
            return resp2.status_code == 200
    except Exception:
        return False


async def check_ollama_health() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
            return resp.status_code == 200
    except Exception:
        return False
