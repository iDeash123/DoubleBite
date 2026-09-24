from django.contrib.sitemaps import Sitemap
from django.db.models import QuerySet
from django.urls import reverse

from .models import Category, Dish


class StaticViewSitemap(Sitemap):
    priority = 1.0
    changefreq = 'daily'

    def items(self) -> list[str]:
        return ['home', 'menu:catalog']

    def location(self, item: str) -> str:
        return reverse(item)


class CategorySitemap(Sitemap):
    priority = 0.7
    changefreq = 'daily'

    def items(self) -> QuerySet[Category]:
        return Category.objects.filter(is_active=True).order_by('display_order', 'id')

    def location(self, item: Category) -> str:
        return item.get_absolute_url()


class DishSitemap(Sitemap):
    priority = 0.9
    changefreq = 'weekly'

    def items(self) -> QuerySet[Dish]:
        return (
            Dish.objects.filter(is_available=True, category__is_active=True)
            .select_related('category')
            .order_by('id')
        )

    def lastmod(self, item: Dish):
        return item.updated_at

    def location(self, item: Dish) -> str:
        return item.get_absolute_url()
