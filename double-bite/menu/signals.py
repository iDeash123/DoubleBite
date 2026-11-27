from typing import Any

from config.transactions import schedule_on_commit
from django.db.models.signals import m2m_changed, post_delete, post_save
from support.cache import invalidate_static_ai_prefix
from support.models import FAQKnowledge

from menu.models import Category, Dish, DishOption, Promotion

_M2M_INVALIDATION_ACTIONS = frozenset({'post_add', 'post_remove', 'post_clear'})


def _schedule_static_prefix_invalidation(**_kwargs: Any) -> None:
    schedule_on_commit(invalidate_static_ai_prefix)


def _schedule_static_prefix_invalidation_m2m(
    *,
    action: str = '',
    **_kwargs: Any,
) -> None:
    if action in _M2M_INVALIDATION_ACTIONS:
        schedule_on_commit(invalidate_static_ai_prefix)


def connect_menu_signals() -> None:
    for model_cls, uid_prefix in (
        (Dish, 'menu_dish'),
        (Category, 'menu_category'),
        (DishOption, 'menu_dish_option'),
        (Promotion, 'menu_promotion'),
        (FAQKnowledge, 'support_faq_knowledge'),
    ):
        post_save.connect(
            _schedule_static_prefix_invalidation,
            sender=model_cls,
            dispatch_uid=f'{uid_prefix}_static_prefix_post_save',
        )
        post_delete.connect(
            _schedule_static_prefix_invalidation,
            sender=model_cls,
            dispatch_uid=f'{uid_prefix}_static_prefix_post_delete',
        )

    m2m_changed.connect(
        _schedule_static_prefix_invalidation_m2m,
        sender=Promotion.applicable_categories.through,
        dispatch_uid='menu_promotion_categories_static_prefix_m2m',
    )
    m2m_changed.connect(
        _schedule_static_prefix_invalidation_m2m,
        sender=Promotion.applicable_dishes.through,
        dispatch_uid='menu_promotion_dishes_static_prefix_m2m',
    )
