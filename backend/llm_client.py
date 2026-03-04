import httpx
import logging
from config import settings

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = "You are an expert code reviewer. Be precise, specific, and reference exact files and line numbers where possible."


async def _review_local(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{settings.VLLM_BASE_URL}/v1/chat/completions",
            json={
                "model": settings.VLLM_MODEL_NAME,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.15,
                "max_tokens": 4096,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def _review_openai(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
            json={
                "model": settings.LLM_API_MODEL,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.15,
                "max_tokens": 4096,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def _review_anthropic(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.LLM_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": settings.LLM_API_MODEL,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.15,
                "max_tokens": 4096,
            },
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


async def review(prompt: str) -> str:
    provider = settings.LLM_PROVIDER.lower()
    if provider == "anthropic":
        return await _review_anthropic(prompt)
    elif provider == "openai":
        return await _review_openai(prompt)
    else:
        return await _review_local(prompt)


async def check_health() -> bool:
    provider = settings.LLM_PROVIDER.lower()
    if provider in ("openai", "anthropic"):
        # For API providers, healthy = API key is configured
        return bool(settings.LLM_API_KEY)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
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
