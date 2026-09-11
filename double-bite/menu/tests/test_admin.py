from django.contrib.admin.sites import site
from django.test import TestCase

from menu.admin import CategoryAdmin, DishAdmin, DishOptionAdmin
from menu.models import Category, Dish, DishOption


class MenuAdminTest(TestCase):
    def test_models_registered_in_admin(self):
        self.assertIn(Category, site._registry)
        self.assertIn(Dish, site._registry)
        self.assertIn(DishOption, site._registry)

    def test_category_admin_config(self):
        admin_obj = site._registry[Category]
        self.assertIsInstance(admin_obj, CategoryAdmin)
        self.assertIn('name', admin_obj.list_display)
        self.assertIn('slug', admin_obj.list_display)

    def test_dish_admin_config(self):
        admin_obj = site._registry[Dish]
        self.assertIsInstance(admin_obj, DishAdmin)
        self.assertIn('title', admin_obj.list_display)
        self.assertIn('category', admin_obj.list_display)
        self.assertIn('price', admin_obj.list_display)

    def test_dish_option_admin_config(self):
        admin_obj = site._registry[DishOption]
        self.assertIsInstance(admin_obj, DishOptionAdmin)
        self.assertIn('name', admin_obj.list_display)
        self.assertIn('dish', admin_obj.list_display)
