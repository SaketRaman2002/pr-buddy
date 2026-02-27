from pydantic_settings import BaseSettings
from typing import List

class Settings(BaseSettings):
    GITHUB_TOKEN: str
    GITHUB_REPOS_TO_WATCH: str = ""
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

    class Config:
        env_file = ".env"

settings = Settings()
