import asyncio
import ollama
from ollama import ResponseError

class NomicEmbedder:
    model = "nomic-embed-text"
    async def aembed_query(self, text: str) -> list[float]:
        # Token density varies (URLs and encoded text are unusually dense), so retry
        # progressively smaller inputs when Ollama reports a context overflow.
        size = min(len(text), 6000)
        while size >= 500:
            try:
                response = await asyncio.to_thread(
                    ollama.embed, model=self.model, input=text[:size]
                )
                return response["embeddings"][0]
            except ResponseError as exc:
                if "context length" not in str(exc).lower():
                    raise
                size //= 2
        response = await asyncio.to_thread(ollama.embed, model=self.model, input=text[:500])
        return response["embeddings"][0]
    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.gather(*(self.aembed_query(t) for t in texts))
