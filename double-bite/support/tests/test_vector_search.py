from decimal import Decimal

import pytest
from menu.models import Category, Dish
from menu.selectors import search_dishes_for_agent
from support.vector_search import (
    EMBEDDING_DIM,
    cosine_similarity,
    generate_mock_embedding,
    get_embedding,
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
    dish1, dish2, dish3 = sample_dishes_with_embeddings
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
