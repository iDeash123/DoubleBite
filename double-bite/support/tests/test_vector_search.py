import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from menu.models import Category, Dish
from menu.selectors import search_dishes_for_agent

from support.vector_search import (
    EMBEDDING_DIM,
    cosine_similarity,
    generate_mock_embedding,
    get_embedding,
    get_embeddings_batch,
    search_dishes_semantic,
)


@pytest.fixture
def sample_category(db):
    return Category.objects.create(
        name='Піца',
        slug='pizza',
        display_order=1,
        is_active=True,
    )


@pytest.fixture
def sample_dishes_with_embeddings(sample_category):
    vec_margherita = generate_mock_embedding('піца маргарита сир моцарела томати базилік')
    vec_pepperoni = generate_mock_embedding('піца пепероні гостра ковбаса салямі чилі')
    vec_carbonara = generate_mock_embedding('паста спагеті бекон вершки пармезан')

    dish1 = Dish.objects.create(
        category=sample_category,
        title='Маргарита Класична',
        slug='margherita-classic',
        description='Традиційна італійська піца з ніжним сиром моцарела та базиліком',
        price=Decimal('220.00'),
        weight_grams=400,
        calories=750,
        is_vegetarian=True,
        is_spicy=False,
        is_available=True,
        embedding=vec_margherita,
    )
    dish2 = Dish.objects.create(
        category=sample_category,
        title='Пепероні Вогняна',
        slug='pepperoni-fire',
        description='Гостра піца з ковбасками пепероні та перцем халапеньйо',
        price=Decimal('260.00'),
        weight_grams=450,
        calories=900,
        is_vegetarian=False,
        is_spicy=True,
        is_available=True,
        embedding=vec_pepperoni,
    )
    dish3 = Dish.objects.create(
        category=sample_category,
        title='Карбонара Рома',
        slug='carbonara-roma',
        description='Класична римська паста з гуанчале та жовтком',
        price=Decimal('240.00'),
        weight_grams=350,
        calories=680,
        is_vegetarian=False,
        is_spicy=False,
        is_available=True,
        embedding=vec_carbonara,
    )
    return dish1, dish2, dish3


@pytest.mark.django_db
def test_embedding_generator_dimensions():
    vec = generate_mock_embedding('тестовий запит')
    assert len(vec) == EMBEDDING_DIM
    assert isinstance(vec, list)
    assert all(isinstance(x, float) for x in vec)


@pytest.mark.django_db
def test_embedding_empty_text():
    vec = generate_mock_embedding('')
    assert len(vec) == EMBEDDING_DIM
    assert all(x == 0.0 for x in vec)


@pytest.mark.django_db
def test_cosine_similarity_calculation():
    v1 = [1.0, 0.0, 0.0]
    v2 = [1.0, 0.0, 0.0]
    v3 = [0.0, 1.0, 0.0]
    assert abs(cosine_similarity(v1, v2) - 1.0) < 1e-6
    assert abs(cosine_similarity(v1, v3) - 0.0) < 1e-6
    assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


@pytest.mark.django_db
def test_dish_model_embedding_field(sample_dishes_with_embeddings):
    dish1, _, _ = sample_dishes_with_embeddings
    refreshed = Dish.objects.get(pk=dish1.pk)
    assert refreshed.embedding is not None
    assert len(refreshed.embedding) == EMBEDDING_DIM


@pytest.mark.django_db
def test_search_dishes_semantic_success(sample_dishes_with_embeddings):
    dish1, _, _ = sample_dishes_with_embeddings
    results = search_dishes_semantic('маргарита моцарела', limit=2)
    assert len(results) > 0
    assert results[0].pk == dish1.pk


@pytest.mark.django_db
def test_search_dishes_semantic_pepperoni(sample_dishes_with_embeddings):
    _, dish2, _ = sample_dishes_with_embeddings
    results = search_dishes_semantic('гостра ковбаса пепероні', limit=2)
    assert len(results) > 0
    assert results[0].pk == dish2.pk


@pytest.mark.django_db
def test_search_dishes_semantic_empty_query():
    results = search_dishes_semantic('')
    assert results == []


@pytest.mark.django_db
def test_search_dishes_semantic_fallback_no_embeddings(sample_category):
    dish = Dish.objects.create(
        category=sample_category,
        title='Салат Цезар',
        slug='caesar-salad',
        description='Хрусткий салат ромен з соусом цезар та сухариками',
        price=Decimal('180.00'),
        weight_grams=250,
        calories=350,
        is_available=True,
        embedding=None,
    )
    results = search_dishes_semantic('Цезар', limit=5)
    assert len(results) >= 1
    assert any(d.pk == dish.pk for d in results)


@pytest.mark.django_db
def test_search_dishes_for_agent_with_semantic_fallback(sample_dishes_with_embeddings):
    dish1, _, _ = sample_dishes_with_embeddings
    agent_results = search_dishes_for_agent(query='базилік моцарела')
    assert len(agent_results) > 0
    titles = [item['title'] for item in agent_results]
    assert dish1.title in titles


def test_real_mode_defaults_off_mock_and_returns_none_on_failure():
    env_clean = {
        k: v
        for k, v in os.environ.items()
        if k not in ('TEST_USE_MOCK_EMBEDDING', 'GEMINI_EMBEDDING_TIMEOUT_MS')
    }
    env_clean['GEMINI_API_KEY'] = 'real-api-key'

    mock_resp = MagicMock()
    mock_resp.json.return_value = {'embeddings': [{'values': [0.25] * EMBEDDING_DIM}]}

    with (
        override_settings(is_testing=False),
        patch.dict(os.environ, env_clean, clear=True),
        patch('support.vector_search.httpx.post', return_value=mock_resp) as mock_post,
    ):
        vec = get_embedding('піца маргарита')

    assert vec is not None and len(vec) == EMBEDDING_DIM
    assert abs(sum(x * x for x in vec) - 1.0) < 1e-6
    assert mock_post.call_count == 1
    assert mock_post.call_args.kwargs['headers'] == {'x-goog-api-key': 'real-api-key'}
    assert mock_post.call_args.kwargs['timeout'] == 15

    with (
        override_settings(is_testing=False),
        patch.dict(os.environ, env_clean, clear=True),
        patch('support.vector_search.httpx.post', side_effect=TimeoutError('timed out')),
    ):
        failed_vec = get_embedding('піца маргарита', timeout_ms=5000)

    assert failed_vec is None


def test_real_mode_without_api_key_returns_none():
    env_no_key = {
        k: v
        for k, v in os.environ.items()
        if k not in ('TEST_USE_MOCK_EMBEDDING', 'GEMINI_API_KEY')
    }
    with (
        override_settings(is_testing=False, GEMINI_API_KEY=''),
        patch.dict(os.environ, env_no_key, clear=True),
    ):
        assert get_embedding('піца') is None
        assert get_embeddings_batch(['піца', 'паста']) == [None, None]


@pytest.mark.django_db
def test_sql_filters_applied_before_limit_prevent_crowding_out(sample_category):
    high_sim_vec = generate_mock_embedding('піца неаполітанська сир томати')
    for idx in range(5):
        Dish.objects.create(
            category=sample_category,
            title=f'Дорога Неаполітанська {idx}',
            slug=f'expensive-neapolitan-{idx}',
            description='піца неаполітанська сир томати',
            price=Decimal('450.00'),
            weight_grams=450,
            calories=900,
            is_vegetarian=False,
            is_available=True,
            embedding=high_sim_vec,
        )

    cheap_veg = Dish.objects.create(
        category=sample_category,
        title='Веганська Маргарита Бюджет',
        slug='cheap-veg-margherita',
        description='піца сир томати',
        price=Decimal('190.00'),
        weight_grams=380,
        calories=550,
        is_vegetarian=True,
        is_available=True,
        embedding=generate_mock_embedding('піца сир томати базилік'),
    )

    semantic_hits = search_dishes_semantic(
        'піца неаполітанська сир томати',
        limit=2,
        max_price=Decimal('250.00'),
        is_vegetarian=True,
    )
    assert [d.pk for d in semantic_hits] == [cheap_veg.pk]

    agent_hits = search_dishes_for_agent(
        query='піца неаполітанська сир томати',
        max_price=Decimal('250.00'),
        is_vegetarian=True,
        limit=2,
    )
    assert [item['id'] for item in agent_hits] == [cheap_veg.id]


@pytest.mark.django_db
def test_relevance_cutoff_drops_unrelated_queries(sample_dishes_with_embeddings):
    results = search_dishes_semantic('абсолютно неіснуючий трактор космос', limit=5)
    assert results == []

    agent_results = search_dishes_for_agent(
        query='абсолютно неіснуючий трактор космос',
        limit=5,
    )
    assert agent_results == []


@pytest.mark.django_db
def test_get_embeddings_batch_handles_blank_and_valid_texts():
    vectors = get_embeddings_batch(['піца', '   ', 'суші'])
    assert len(vectors) == 3
    assert vectors[0] is not None and len(vectors[0]) == EMBEDDING_DIM
    assert vectors[1] == [0.0] * EMBEDDING_DIM
    assert vectors[2] is not None and len(vectors[2]) == EMBEDDING_DIM
