from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import RequestFactory, TestCase

from menu.admin import CategoryAdmin, DishAdmin, DishOptionAdmin
from menu.models import Category, Dish, DishOption

User = get_user_model()


class MenuAdminTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.category = Category.objects.create(name='Піца', slug='pizza-adm', is_active=True)
        self.admin_user = User.objects.create_superuser(
            email='menu_admin@example.com',
            password='adminpassword123',
        )

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
        self.assertIn('embedding', admin_obj.exclude)
        req = self.factory.get('/admin/')
        req.user = self.admin_user
        form_cls = admin_obj.get_form(req)
        self.assertNotIn('embedding', form_cls.base_fields)

    def test_dish_option_admin_config(self):
        admin_obj = site._registry[DishOption]
        self.assertIsInstance(admin_obj, DishOptionAdmin)
        self.assertIn('name', admin_obj.list_display)
        self.assertIn('dish', admin_obj.list_display)

    def test_dish_admin_save_model_refreshes_embedding_only_on_commit(self):
        admin_obj = site._registry[Dish]
        req = self.factory.post('/admin/menu/dish/add/')

        new_dish = Dish(
            category=self.category,
            title='Трюфельна Піца',
            slug='truffle-adm',
            description='Сир та трюфель',
            price=Decimal('320.00'),
            weight_grams=420,
            is_available=True,
        )
        form_new = MagicMock(changed_data=['title'])

        with (
            patch('menu.admin.refresh_dish_embedding') as mock_refresh,
            self.captureOnCommitCallbacks(execute=True),
        ):
            admin_obj.save_model(req, new_dish, form_new, change=False)
        mock_refresh.assert_called_once_with(new_dish.pk)

        form_price_only = MagicMock(changed_data=['price'])
        new_dish.price = Decimal('340.00')
        with (
            patch('menu.admin.refresh_dish_embedding') as mock_refresh_price,
            self.captureOnCommitCallbacks(execute=True),
        ):
            admin_obj.save_model(req, new_dish, form_price_only, change=True)
        mock_refresh_price.assert_not_called()

        rolled_back_dish = Dish(
            category=self.category,
            title='Відкатна Страва',
            slug='rollback-adm',
            price=Decimal('200.00'),
            weight_grams=300,
        )
        with (
            patch('menu.admin.refresh_dish_embedding') as mock_refresh_rb,
            self.captureOnCommitCallbacks(execute=True),
        ):
            try:
                with transaction.atomic():
                    admin_obj.save_model(req, rolled_back_dish, form_new, change=False)
                    raise RuntimeError('Simulated rollback')
            except RuntimeError:
                pass
        mock_refresh_rb.assert_not_called()

    def test_dish_admin_rebuild_embeddings_action(self):
        admin_obj = site._registry[Dish]
        dish = Dish.objects.create(
            category=self.category,
            title='Карбонара',
            slug='carbonara-adm',
            description='Паста з пармезаном',
            price=Decimal('240.00'),
            weight_grams=350,
            is_available=True,
            embedding=None,
        )
        admin_obj.rebuild_embeddings_action(None, Dish.objects.filter(pk=dish.pk))
        dish.refresh_from_db()
        self.assertIsNotNone(dish.embedding)
        self.assertEqual(len(dish.embedding), 1024)
