import math


def retrieval_metrics(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> dict:
    ranked = retrieved_ids[:k]
    hits = [1 if item in relevant_ids else 0 for item in ranked]
    hit_count = sum(hits)
    precision = hit_count / k if k else 0.0
    recall = hit_count / len(relevant_ids) if relevant_ids else 1.0
    reciprocal_rank = next((1.0 / rank for rank, hit in enumerate(hits, 1) if hit), 0.0)
    dcg = sum(hit / math.log2(rank + 1) for rank, hit in enumerate(hits, 1))
    ideal_hits = min(len(relevant_ids), k)
    ideal_dcg = sum(1 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return {
        f"precision@{k}": precision,
        f"recall@{k}": recall,
        "mrr": reciprocal_rank,
        f"ndcg@{k}": dcg / ideal_dcg if ideal_dcg else 1.0,
    }


def aggregate_metrics(rows: list[dict]) -> dict:
    if not rows:
        return {}
    keys = sorted({key for row in rows for key in row})
    return {key: sum(float(row.get(key, 0)) for row in rows) / len(rows) for key in keys}
