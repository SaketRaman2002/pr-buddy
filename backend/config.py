from pydantic_settings import BaseSettings
from typing import List

class Settings(BaseSettings):
    GITHUB_TOKEN: str
    GITHUB_REPOS_TO_WATCH: str = ""
    TEAM_MEMBERS: str = ""  # comma-separated GitHub usernames whose reviews to learn from

    # LLM provider: "local" (vLLM), "openai", or "anthropic"
    LLM_PROVIDER: str = "local"
    LLM_API_KEY: str = ""          # API key for openai or anthropic (unused for local)
    LLM_API_MODEL: str = "claude-sonnet-4-6"  # model for openai/anthropic

    VLLM_BASE_URL: str = "http://localhost:8000"
    VLLM_MODEL_NAME: str = "Qwen/Qwen2.5-Coder-32B-Instruct"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    EMBEDDING_MODEL: str = "nomic-embed-text"
    CHROMA_PERSIST_DIR: str = "../storage/chroma"
    REPOS_DIR: str = "../storage/repos"
    CACHE_DIR: str = "../storage/cache"
    MAX_CONTEXT_FILES: int = 5
    MAX_FILE_CHARS: int = 4000
    MAX_DIFF_CHARS: int = 6000
    PORT: int = 8001
    WHATSAPP_ALLOWED_JID: str = ""

    @property
    def repos_to_watch(self) -> List[str]:
        return [r.strip() for r in self.GITHUB_REPOS_TO_WATCH.split(",") if r.strip()]

    @property
    def team_members(self) -> List[str]:
        return [m.strip() for m in self.TEAM_MEMBERS.split(",") if m.strip()]

    class Config:
        env_file = ".env"

settings = Settings()
