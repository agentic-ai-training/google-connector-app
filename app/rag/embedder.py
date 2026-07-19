import asyncio
import time
import ollama
from ollama import ResponseError
from app.config.settings import get_settings
from app.mlops.metrics import (
    embedding_duration, embedding_input_chars, embedding_overflows,
    ollama_model_loaded,
)

class NomicEmbedder:
    model = "nomic-embed-text"
    def __init__(self):
        self.client = ollama.Client(host=get_settings().ollama_host)

    async def aembed_query(self, text: str) -> list[float]:
        # Token density varies (URLs and encoded text are unusually dense), so retry
        # progressively smaller inputs when Ollama reports a context overflow.
        size = min(len(text), 6000)
        started = time.perf_counter()
        embedding_input_chars.labels("query").observe(size)
        while size >= 500:
            try:
                response = await asyncio.to_thread(
                    self.client.embed, model=self.model, input=text[:size]
                )
                embedding_duration.labels("query", "success").observe(
                    time.perf_counter() - started
                )
                ollama_model_loaded.set(1)
                return response["embeddings"][0]
            except ResponseError as exc:
                if "context length" not in str(exc).lower():
                    embedding_duration.labels("query", "error").observe(
                        time.perf_counter() - started
                    )
                    ollama_model_loaded.set(0)
                    raise
                embedding_overflows.inc()
                size //= 2
            except Exception:
                embedding_duration.labels("query", "error").observe(
                    time.perf_counter() - started
                )
                ollama_model_loaded.set(0)
                raise
        try:
            response = await asyncio.to_thread(
                self.client.embed, model=self.model, input=text[:500]
            )
        except Exception:
            embedding_duration.labels("query", "error").observe(
                time.perf_counter() - started
            )
            ollama_model_loaded.set(0)
            raise
        embedding_duration.labels("query", "success").observe(time.perf_counter()-started)
        ollama_model_loaded.set(1)
        return response["embeddings"][0]
    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for offset in range(0, len(texts), 8):
            batch = [text[:6000] for text in texts[offset:offset + 8]]
            started = time.perf_counter()
            embedding_input_chars.labels("batch").observe(sum(map(len, batch)))
            try:
                response = await asyncio.to_thread(
                    self.client.embed, model=self.model, input=batch
                )
                vectors.extend(response["embeddings"])
                embedding_duration.labels("batch", "success").observe(
                    time.perf_counter() - started
                )
                ollama_model_loaded.set(1)
            except ResponseError as exc:
                if "context length" not in str(exc).lower():
                    embedding_duration.labels("batch", "error").observe(
                        time.perf_counter() - started
                    )
                    ollama_model_loaded.set(0)
                    raise
                embedding_overflows.inc()
                # Preserve per-item overflow handling without unbounded concurrency.
                for text in batch:
                    vectors.append(await self.aembed_query(text))
                embedding_duration.labels("batch", "success").observe(
                    time.perf_counter() - started
                )
            except Exception:
                embedding_duration.labels("batch", "error").observe(
                    time.perf_counter() - started
                )
                ollama_model_loaded.set(0)
                raise
        return vectors
