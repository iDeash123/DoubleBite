import hashlib
import math
import os
from typing import Any

from django.db import connection
from django.db.models import Q
from menu.models import Dish

EMBEDDING_DIM = 1024


def generate_mock_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    cleaned = text.strip().lower()
    if not cleaned:
        return [0.0] * dim

    vec = [0.0] * dim
    for word in cleaned.split():
        h = int(hashlib.sha256(word.encode('utf-8')).hexdigest(), 16)
        for j in range(dim):
            bit = (h >> (j % 64)) & 1
            val = 1.0 if bit else -1.0
            vec[j] += val / (1.0 + (j % 10))

    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


def get_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    api_key = os.getenv('MISTRAL_API_KEY')
    if api_key and not os.getenv('TEST_USE_MOCK_EMBEDDING', '1') == '1':
        try:
            from mistralai import Mistral
            client = Mistral(api_key=api_key)
            response = client.embeddings.create(
                model="mistral-embed",
                inputs=[text],
            )
            return list(response.data[0].embedding)
        except Exception:
            return generate_mock_embedding(text, dim=dim)
    return generate_mock_embedding(text, dim=dim)


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


def search_dishes_semantic(
    query: str,
    limit: int = 5,
    category_slug: str | None = None,
    only_available: bool = True,
) -> list[Dish]:
    cleaned = query.strip()
    if not cleaned:
        return []

    query_vec = get_embedding(cleaned)

    if connection.vendor == 'postgresql':
        try:
            from pgvector.django import CosineDistance
            qs = Dish.objects.all()
            if only_available:
                qs = qs.filter(is_available=True, category__is_active=True)
            if category_slug:
                qs = qs.filter(category__slug=category_slug)

            vector_matches = (
                qs.filter(embedding__isnull=False)
                .annotate(distance=CosineDistance('embedding', query_vec))
                .order_by('distance')[:limit]
            )
            results = list(vector_matches)
            if results:
                return results
        except Exception:
            pass

    qs = Dish.objects.filter(embedding__isnull=False)
    if only_available:
        qs = qs.filter(is_available=True, category__is_active=True)
    if category_slug:
        qs = qs.filter(category__slug=category_slug)

    dishes_with_vec = list(qs.select_related('category'))
    if dishes_with_vec:
        scored = [
            (dish, cosine_similarity(query_vec, dish.embedding))
            for dish in dishes_with_vec
            if dish.embedding is not None
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return [item[0] for item in scored[:limit]]

    keywords = [w for w in cleaned.split() if len(w) > 2]
    q_filter = Q(title__icontains=cleaned) | Q(description__icontains=cleaned)
    for kw in keywords:
        q_filter |= Q(title__icontains=kw) | Q(description__icontains=kw) | Q(category__name__icontains=kw)

    fallback_qs = Dish.objects.filter(q_filter)
    if only_available:
        fallback_qs = fallback_qs.filter(is_available=True, category__is_active=True)
    if category_slug:
        fallback_qs = fallback_qs.filter(category__slug=category_slug)

    return list(fallback_qs.select_related('category')[:limit])
