import asyncio
import ollama
from ollama import ResponseError
from app.config.settings import get_settings

class NomicEmbedder:
    model = "nomic-embed-text"
    def __init__(self):
        self.client = ollama.Client(host=get_settings().ollama_host)

    async def aembed_query(self, text: str) -> list[float]:
        # Token density varies (URLs and encoded text are unusually dense), so retry
        # progressively smaller inputs when Ollama reports a context overflow.
        size = min(len(text), 6000)
        while size >= 500:
            try:
                response = await asyncio.to_thread(
                    self.client.embed, model=self.model, input=text[:size]
                )
                return response["embeddings"][0]
            except ResponseError as exc:
                if "context length" not in str(exc).lower():
                    raise
                size //= 2
        response = await asyncio.to_thread(
            self.client.embed, model=self.model, input=text[:500]
        )
        return response["embeddings"][0]
    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.gather(*(self.aembed_query(t) for t in texts))
