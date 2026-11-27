import logging

from config.transactions import schedule_on_commit
from django.contrib import admin
from support.cache import invalidate_static_ai_prefix
from support.vector_search import (
    build_dish_embedding_text,
    get_embedding,
    get_embeddings_batch,
)

from .models import Category, Dish, DishOption, Promotion

logger = logging.getLogger('menu')

_EMBEDDING_SOURCE_FIELDS = frozenset({
    'title',
    'category',
    'description',
    'ingredients',
    'allergens',
    'allergen_codes',
})
_ACTION_BATCH_SIZE = 32


def refresh_dish_embedding(dish_id: int) -> bool:
    dish = Dish.objects.select_related('category').filter(pk=dish_id).first()
    if dish is None:
        return False
    text = build_dish_embedding_text(dish)
    if not text:
        return False
    vec = get_embedding(text)
    if vec is None:
        logger.warning(
            'Skipping embedding refresh for dish %s: embedding unavailable.',
            dish_id,
        )
        return False
    Dish.objects.filter(pk=dish_id).update(embedding=vec)
    return True


class DishOptionInline(admin.TabularInline):
    model = DishOption
    extra = 1


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'display_order', 'is_active')
    list_editable = ('display_order', 'is_active')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Dish)
class DishAdmin(admin.ModelAdmin):
    list_display = (
        'title',
        'category',
        'price',
        'weight_grams',
        'calories',
        'upsell_group',
        'is_vegetarian',
        'is_vegan',
        'is_spicy',
        'safe_for_pregnancy',
        'is_available',
    )
    list_filter = (
        'category',
        'is_available',
        'upsell_group',
        'is_vegetarian',
        'is_vegan',
        'is_spicy',
        'contains_raw_fish',
        'safe_for_pregnancy',
    )
    search_fields = ('title', 'description', 'ingredients', 'allergens')
    list_select_related = ('category',)
    prepopulated_fields = {'slug': ('title',)}
    exclude = ('embedding',)
    actions = ('rebuild_embeddings_action',)
    inlines = [DishOptionInline]

    def save_model(self, request, obj, form, change):
        if not change:
            should_refresh = True
        else:
            changed_data = getattr(form, 'changed_data', None)
            should_refresh = (
                changed_data is None
                or bool(_EMBEDDING_SOURCE_FIELDS.intersection(changed_data))
            )
        super().save_model(request, obj, form, change)
        if should_refresh and obj.pk:
            schedule_on_commit(refresh_dish_embedding, obj.pk)

    @admin.action(description='Перебудувати векторні ембедінги обраних страв')
    def rebuild_embeddings_action(self, request, queryset):
        dishes = list(queryset.select_related('category').order_by('id'))
        if not dishes:
            return

        updated_count = 0
        failed_count = 0
        for start in range(0, len(dishes), _ACTION_BATCH_SIZE):
            batch = dishes[start : start + _ACTION_BATCH_SIZE]
            valid_dishes: list[Dish] = []
            texts: list[str] = []
            for dish in batch:
                text = build_dish_embedding_text(dish)
                if not text:
                    failed_count += 1
                    continue
                valid_dishes.append(dish)
                texts.append(text)

            if not texts:
                continue

            vectors = get_embeddings_batch(texts)
            to_update: list[Dish] = []
            for idx, dish in enumerate(valid_dishes):
                vec = vectors[idx] if idx < len(vectors) else None
                if vec is None:
                    failed_count += 1
                    continue
                dish.embedding = vec
                to_update.append(dish)

            if to_update:
                Dish.objects.bulk_update(
                    to_update, ['embedding'], batch_size=_ACTION_BATCH_SIZE
                )
                updated_count += len(to_update)

        if updated_count:
            schedule_on_commit(invalidate_static_ai_prefix)

        if request is not None:
            if updated_count:
                self.message_user(
                    request,
                    f'Оновлено векторні ембедінги для {updated_count} страв.',
                )
            if failed_count:
                self.message_user(
                    request,
                    f'Не вдалося оновити ембедінги для {failed_count} страв.',
                    level='warning',
                )


@admin.register(DishOption)
class DishOptionAdmin(admin.ModelAdmin):
    list_display = ('name', 'dish', 'price_delta')
    list_filter = ('dish__category',)
    search_fields = ('name', 'dish__title')
    list_select_related = ('dish',)


@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    list_display = (
        'title',
        'promo_type',
        'discount_value',
        'min_order_amount',
        'is_active',
        'starts_at',
        'ends_at',
    )
    list_filter = ('promo_type', 'is_active')
    search_fields = ('title', 'slug', 'description')
    prepopulated_fields = {'slug': ('title',)}
    filter_horizontal = ('applicable_categories', 'applicable_dishes')
