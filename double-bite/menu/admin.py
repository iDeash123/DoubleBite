from django.contrib import admin

from .models import Category, Dish, DishOption


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
        'is_vegetarian',
        'is_spicy',
        'is_available',
    )
    list_filter = ('category', 'is_available', 'is_vegetarian', 'is_spicy')
    search_fields = ('title', 'description', 'allergens')
    list_select_related = ('category',)
    prepopulated_fields = {'slug': ('title',)}
    inlines = [DishOptionInline]


@admin.register(DishOption)
class DishOptionAdmin(admin.ModelAdmin):
    list_display = ('name', 'dish', 'price_delta')
    list_filter = ('dish__category',)
    search_fields = ('name', 'dish__title')
    list_select_related = ('dish',)
