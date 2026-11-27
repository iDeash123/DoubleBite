import hashlib
import logging
import math
import os
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

import httpx
from django.conf import settings
from django.db import DatabaseError, connection
from django.db.models import Q, QuerySet
from menu.models import Dish

GEMINI_EMBEDDING_MODEL = 'gemini-embedding-2'
GEMINI_API_BASE_URL = 'https://generativelanguage.googleapis.com/v1beta'

logger = logging.getLogger('support')

EMBEDDING_DIM = 1024
DEFAULT_EMBEDDING_TIMEOUT_MS = 15000
DEFAULT_MAX_COSINE_DISTANCE = 0.65
_MAX_EMBED_BATCH_SIZE = 64

_EMBEDDING_EXCEPTIONS = (
    httpx.HTTPError,
    TimeoutError,
    OSError,
    ValueError,
    TypeError,
    AttributeError,
    RuntimeError,
)


def _is_testing_env() -> bool:
    if hasattr(settings, 'is_testing'):
        return bool(settings.is_testing)
    if hasattr(settings, 'IS_TESTING'):
        return bool(settings.IS_TESTING)
    try:
        from config import settings as config_settings
    except ImportError:
        return False
    return bool(getattr(config_settings, 'is_testing', False))


def _should_use_mock_embedding() -> bool:
    raw = os.getenv('TEST_USE_MOCK_EMBEDDING')
    if raw is not None:
        cleaned = raw.strip().lower()
        if cleaned:
            return cleaned in ('1', 'true', 'yes', 'on')
    return _is_testing_env()


def _resolve_embedding_timeout_ms(timeout_ms: int | None) -> int:
    if timeout_ms is not None:
        parsed = int(timeout_ms)
        return parsed if parsed > 0 else DEFAULT_EMBEDDING_TIMEOUT_MS
    raw = os.getenv('GEMINI_EMBEDDING_TIMEOUT_MS', '').strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except ValueError:
            logger.warning('Invalid GEMINI_EMBEDDING_TIMEOUT_MS value: %r', raw)
    return DEFAULT_EMBEDDING_TIMEOUT_MS


def _resolve_max_cosine_distance(max_distance: float | None) -> float:
    if max_distance is not None:
        val = float(max_distance)
        if math.isfinite(val) and 0.0 <= val <= 2.0:
            return val
    raw = getattr(settings, 'RAG_MAX_COSINE_DISTANCE', None)
    if raw is None:
        raw = os.getenv('RAG_MAX_COSINE_DISTANCE', '').strip() or None
    if raw is not None:
        try:
            val = float(raw)
            if math.isfinite(val) and 0.0 <= val <= 2.0:
                return val
        except (ValueError, TypeError):
            logger.warning('Invalid RAG_MAX_COSINE_DISTANCE value: %r', raw)
    return DEFAULT_MAX_COSINE_DISTANCE


def _get_gemini_api_key() -> str:
    return (
        os.getenv('GEMINI_API_KEY')
        or getattr(settings, 'GEMINI_API_KEY', '')
        or ''
    ).strip()


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm > 0 else vec


def _gemini_embed_request(
    texts: list[str], dim: int, api_key: str, timeout_ms: int
) -> list[list[float] | None]:
    model_path = f'models/{GEMINI_EMBEDDING_MODEL}'
    body = {
        'requests': [
            {
                'model': model_path,
                'content': {'parts': [{'text': t}]},
                'outputDimensionality': dim,
            }
            for t in texts
        ]
    }
    response = httpx.post(
        f'{GEMINI_API_BASE_URL}/{model_path}:batchEmbedContents',
        json=body,
        headers={'x-goog-api-key': api_key},
        timeout=timeout_ms / 1000,
    )
    response.raise_for_status()
    items = response.json().get('embeddings') or []
    out: list[list[float] | None] = [None] * len(texts)
    for pos, item in enumerate(items[: len(texts)]):
        values = item.get('values')
        if values:
            out[pos] = _normalize([float(x) for x in values])
    return out


def build_dish_embedding_text(dish: Dish) -> str:
    parts: list[str] = []
    if getattr(dish, 'title', None):
        parts.append(str(dish.title).strip())
    category = getattr(dish, 'category', None)
    if category is not None and getattr(category, 'name', None):
        parts.append(str(category.name).strip())
    if getattr(dish, 'description', None):
        parts.append(str(dish.description).strip())
    if getattr(dish, 'allergens', None):
        parts.append(str(dish.allergens).strip())
    return ' '.join(part for part in parts if part)


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


def get_embedding(
    text: str,
    dim: int = EMBEDDING_DIM,
    timeout_ms: int | None = None,
) -> list[float] | None:
    if _should_use_mock_embedding():
        return generate_mock_embedding(text, dim=dim)

    if not text or not text.strip():
        return None

    api_key = _get_gemini_api_key()
    if not api_key:
        logger.warning('GEMINI_API_KEY is not configured; cannot generate embedding.')
        return None

    resolved_timeout = _resolve_embedding_timeout_ms(timeout_ms)
    try:
        return _gemini_embed_request([text], dim, api_key, resolved_timeout)[0]
    except _EMBEDDING_EXCEPTIONS as exc:
        logger.warning('Gemini embedding request failed: %s', exc)
        return None


def get_embeddings_batch(
    texts: Sequence[str],
    dim: int = EMBEDDING_DIM,
    timeout_ms: int | None = None,
) -> list[list[float] | None]:
    texts_list = list(texts)
    if not texts_list:
        return []

    if _should_use_mock_embedding():
        return [generate_mock_embedding(t, dim=dim) for t in texts_list]

    api_key = _get_gemini_api_key()
    if not api_key:
        logger.warning(
            'GEMINI_API_KEY is not configured; cannot generate batch embeddings.'
        )
        return [None] * len(texts_list)

    resolved_timeout = _resolve_embedding_timeout_ms(timeout_ms)
    results: list[list[float] | None] = [None] * len(texts_list)

    for start in range(0, len(texts_list), _MAX_EMBED_BATCH_SIZE):
        indices = [
            i
            for i in range(start, min(start + _MAX_EMBED_BATCH_SIZE, len(texts_list)))
            if texts_list[i] and texts_list[i].strip()
        ]
        if not indices:
            continue
        try:
            vecs = _gemini_embed_request(
                [texts_list[i] for i in indices], dim, api_key, resolved_timeout
            )
        except _EMBEDDING_EXCEPTIONS as exc:
            logger.warning('Gemini batch embedding request failed: %s', exc)
            continue
        for idx, vec in zip(indices, vecs, strict=True):
            results[idx] = vec

    return results


def cosine_similarity(v1: Sequence[float], v2: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2, strict=False))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return float(dot / (norm1 * norm2))


def _apply_dish_filters(
    qs: QuerySet[Dish],
    *,
    only_available: bool = True,
    category_slug: str | None = None,
    max_price: Decimal | float | int | None = None,
    is_vegetarian: bool | None = None,
    max_calories: int | None = None,
) -> QuerySet[Dish]:
    if only_available:
        qs = qs.filter(is_available=True, category__is_active=True)
    if category_slug:
        qs = qs.filter(category__slug=category_slug)
    if max_price is not None:
        if isinstance(max_price, bool):
            logger.warning('Ignoring invalid boolean max_price filter.')
            return qs.none()
        try:
            price_dec = (
                max_price
                if isinstance(max_price, Decimal)
                else Decimal(str(max_price))
            )
            if not price_dec.is_finite():
                logger.warning('Ignoring non-finite max_price filter: %r', max_price)
                return qs.none()
            qs = qs.filter(price__lte=price_dec)
        except (InvalidOperation, ValueError, TypeError):
            logger.warning('Ignoring invalid max_price filter: %r', max_price)
            return qs.none()
    if is_vegetarian is not None:
        qs = qs.filter(is_vegetarian=bool(is_vegetarian))
    if max_calories is not None:
        if isinstance(max_calories, bool):
            logger.warning('Ignoring invalid boolean max_calories filter.')
            return qs.none()
        try:
            qs = qs.filter(calories__lte=int(max_calories))
        except (ValueError, TypeError):
            logger.warning('Ignoring invalid max_calories filter: %r', max_calories)
            return qs.none()
    return qs


def _build_text_search_q(term: str) -> Q:
    if connection.vendor == 'sqlite' and not term.isascii():
        variants = {
            term,
            term.lower(),
            term.capitalize(),
            term.upper(),
        }
        q_filter = Q()
        for variant in variants:
            q_filter |= (
                Q(title__icontains=variant)
                | Q(description__icontains=variant)
                | Q(category__name__icontains=variant)
            )
        return q_filter
    return (
        Q(title__icontains=term)
        | Q(description__icontains=term)
        | Q(category__name__icontains=term)
    )


def search_dishes_semantic(
    query: str,
    limit: int = 5,
    category_slug: str | None = None,
    only_available: bool = True,
    max_price: Decimal | float | int | None = None,
    is_vegetarian: bool | None = None,
    max_calories: int | None = None,
    max_distance: float | None = None,
) -> list[Dish]:
    cleaned = query.strip()
    if not cleaned or limit <= 0:
        return []

    resolved_max_distance = _resolve_max_cosine_distance(max_distance)
    base_qs = _apply_dish_filters(
        Dish.objects.all(),
        only_available=only_available,
        category_slug=category_slug,
        max_price=max_price,
        is_vegetarian=is_vegetarian,
        max_calories=max_calories,
    )

    query_vec = get_embedding(cleaned)
    if query_vec is not None:
        embedded_qs = base_qs.filter(embedding__isnull=False)

        if connection.vendor == 'postgresql':
            try:
                from pgvector.django import CosineDistance

                vector_matches = (
                    embedded_qs.select_related('category')
                    .prefetch_related('options')
                    .annotate(distance=CosineDistance('embedding', query_vec))
                    .filter(distance__lte=resolved_max_distance)
                    .order_by('distance')[:limit]
                )
                results = list(vector_matches)
                if results or embedded_qs.exists():
                    return results
            except (
                ImportError,
                DatabaseError,
                ValueError,
                TypeError,
                RuntimeError,
            ) as exc:
                logger.warning(
                    'PostgreSQL pgvector search failed, falling back: %s', exc
                )

        dishes_with_vec = list(
            embedded_qs.select_related('category').prefetch_related('options')
        )
        if dishes_with_vec:
            scored: list[tuple[Dish, float]] = []
            for dish in dishes_with_vec:
                if dish.embedding is None:
                    continue
                sim = cosine_similarity(query_vec, list(dish.embedding))
                dist = 1.0 - sim
                if dist <= resolved_max_distance:
                    scored.append((dish, dist))
            scored.sort(key=lambda item: item[1])
            return [item[0] for item in scored[:limit]]

    keywords = [w for w in cleaned.split() if len(w) > 2]
    q_filter = _build_text_search_q(cleaned)
    for kw in keywords:
        q_filter |= _build_text_search_q(kw)

    fallback_qs = base_qs.filter(q_filter).distinct()
    return list(
        fallback_qs.select_related('category').prefetch_related('options')[:limit]
    )
