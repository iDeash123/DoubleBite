from decimal import Decimal
from importlib import import_module
from unittest.mock import patch

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.test import TestCase
from django.urls import resolve

from menu.models import Category, Dish, DishOption, transliterate_to_ascii


class CategoryModelTest(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name='Піца',
            slug='pizza',
            icon='pizza-slice',
            display_order=1,
            is_active=True,
        )

    def test_category_creation(self):
        self.assertEqual(self.category.name, 'Піца')
        self.assertEqual(self.category.slug, 'pizza')
        self.assertEqual(self.category.icon, 'pizza-slice')
        self.assertEqual(self.category.display_order, 1)
        self.assertTrue(self.category.is_active)

    def test_category_str(self):
        self.assertEqual(str(self.category), 'Піца')

    def test_category_unique_slug(self):
        with self.assertRaises(IntegrityError):
            Category.objects.create(
                name='Інша піца',
                slug='pizza',
            )

    def test_category_auto_slug(self):
        cat = Category.objects.create(name='Бургери')
        self.assertTrue(bool(cat.slug))

    def test_category_ordering(self):
        cat2 = Category.objects.create(name='Суші', slug='sushi', display_order=0)
        categories = list(Category.objects.all())
        self.assertEqual(categories[0], cat2)
        self.assertEqual(categories[1], self.category)


class DishModelTest(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name='Піца',
            slug='pizza',
        )
        self.dish = Dish.objects.create(
            category=self.category,
            title='Маргарита',
            slug='margarita',
            description='Томатний соус, моцарела, базилік',
            price=Decimal('220.00'),
            weight_grams=450,
            calories=850,
            allergens='глютен, лактоза',
            is_vegetarian=True,
            is_spicy=False,
            is_available=True,
        )

    def test_dish_creation(self):
        self.assertEqual(self.dish.title, 'Маргарита')
        self.assertEqual(self.dish.slug, 'margarita')
        self.assertEqual(self.dish.price, Decimal('220.00'))
        self.assertEqual(self.dish.weight_grams, 450)
        self.assertEqual(self.dish.calories, 850)
        self.assertTrue(self.dish.is_vegetarian)
        self.assertFalse(self.dish.is_spicy)
        self.assertTrue(self.dish.is_available)

    def test_dish_str(self):
        self.assertEqual(str(self.dish), 'Маргарита (220.00 грн)')

    def test_dish_unique_slug(self):
        with self.assertRaises(IntegrityError):
            Dish.objects.create(
                category=self.category,
                title='Маргарита 2',
                slug='margarita',
                price=Decimal('230.00'),
                weight_grams=400,
            )

    def test_dish_auto_slug(self):
        dish = Dish.objects.create(
            category=self.category,
            title='Кальцоне',
            price=Decimal('240.00'),
            weight_grams=420,
        )
        self.assertTrue(bool(dish.slug))

    def test_dish_price_validation_zero(self):
        dish = Dish(
            category=self.category,
            title='Безкоштовна страва',
            slug='free-dish',
            price=Decimal('0.00'),
            weight_grams=300,
        )
        with self.assertRaises(ValidationError):
            dish.save()

    def test_dish_price_validation_negative(self):
        dish = Dish(
            category=self.category,
            title='Від\'ємна страва',
            slug='negative-dish',
            price=Decimal('-10.00'),
            weight_grams=300,
        )
        with self.assertRaises(ValidationError):
            dish.save()

    def test_dish_weight_validation_zero(self):
        dish = Dish(
            category=self.category,
            title='Невагома страва',
            slug='zero-weight',
            price=Decimal('100.00'),
            weight_grams=0,
        )
        with self.assertRaises(ValidationError):
            dish.save()

    def test_category_cascade_delete(self):
        self.category.delete()
        self.assertFalse(Dish.objects.filter(pk=self.dish.pk).exists())


class DishOptionModelTest(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name='Піца',
            slug='pizza',
        )
        self.dish = Dish.objects.create(
            category=self.category,
            title='Пепероні',
            slug='pepperoni',
            price=Decimal('250.00'),
            weight_grams=480,
        )
        self.option = DishOption.objects.create(
            dish=self.dish,
            name='Подвійний сир',
            price_delta=Decimal('45.00'),
        )

    def test_option_creation(self):
        self.assertEqual(self.option.name, 'Подвійний сир')
        self.assertEqual(self.option.price_delta, Decimal('45.00'))
        self.assertEqual(self.option.dish, self.dish)

    def test_option_str(self):
        self.assertEqual(str(self.option), 'Пепероні - Подвійний сир (+45.00 грн)')

    def test_dish_cascade_delete(self):
        self.dish.delete()
        self.assertFalse(DishOption.objects.filter(pk=self.option.pk).exists())

    def test_invalid_negative_price_delta(self):
        option = DishOption(
            dish=self.dish,
            name='Величезна знижка',
            price_delta=Decimal('-300.00'),
        )
        with self.assertRaises(ValidationError):
            option.save()


class AsciiSlugAndImageFieldTest(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name='Піца',
            slug='pizza',
        )

    def test_transliterate_to_ascii_ukrainian_example(self):
        self.assertEqual(transliterate_to_ascii('Кальцоне з рікотою'), 'Kaltsone z rikotoiu')

    def test_cyrillic_category_auto_slug_collision_and_url(self):
        cat1 = Category.objects.create(name='Кальцоне з рікотою')
        self.assertEqual(cat1.slug, 'kaltsone-z-rikotoiu')
        self.assertTrue(cat1.slug.isascii())
        url = cat1.get_absolute_url()
        self.assertEqual(url, '/menu/kaltsone-z-rikotoiu/')
        self.assertEqual(resolve(url).url_name, 'dish_detail')

        cat2 = Category.objects.create(name='Кальцоне з рікотою')
        self.assertEqual(cat2.slug, 'kaltsone-z-rikotoiu-2')
        self.assertEqual(cat2.get_absolute_url(), '/menu/kaltsone-z-rikotoiu-2/')

    def test_cyrillic_dish_auto_slug_collision_and_url(self):
        dish1 = Dish.objects.create(
            category=self.category,
            title='Кальцоне з рікотою',
            price=Decimal('240.00'),
            weight_grams=420,
        )
        self.assertEqual(dish1.slug, 'kaltsone-z-rikotoiu')
        self.assertTrue(dish1.slug.isascii())
        url1 = dish1.get_absolute_url()
        self.assertEqual(url1, '/menu/pizza/kaltsone-z-rikotoiu/')
        self.assertEqual(resolve(url1).url_name, 'category_dish_detail')

        dish2 = Dish.objects.create(
            category=self.category,
            title='Кальцоне з рікотою',
            price=Decimal('255.00'),
            weight_grams=430,
        )
        self.assertEqual(dish2.slug, 'kaltsone-z-rikotoiu-2')
        self.assertEqual(dish2.get_absolute_url(), '/menu/pizza/kaltsone-z-rikotoiu-2/')

    def test_explicit_non_ascii_slug_fails_validation(self):
        cat = Category(name='Тест', slug='піца')
        with self.assertRaises(ValidationError):
            cat.save()

        dish = Dish(
            category=self.category,
            title='Тест',
            slug='кальцоне',
            price=Decimal('200.00'),
            weight_grams=300,
        )
        with self.assertRaises(ValidationError):
            dish.save()

    def test_migration_0005_transliterates_non_ascii_slugs_uniquely(self):
        migration_0005 = import_module('menu.migrations.0005_ascii_slugs_and_indexes')

        cat_existing = Category.objects.create(name='Кальцоне база', slug='kaltsone')
        cat_cyrillic = Category.objects.create(name='Кальцоне', slug='temp-cat-slug')
        Category.objects.filter(pk=cat_cyrillic.pk).update(slug='кальцоне')

        dish_existing = Dish.objects.create(
            category=self.category,
            title='Кальцоне з рікотою база',
            slug='kaltsone-z-rikotoiu',
            price=Decimal('240.00'),
            weight_grams=400,
        )
        dish_cyrillic = Dish.objects.create(
            category=self.category,
            title='Кальцоне з рікотою',
            slug='temp-dish-slug',
            price=Decimal('250.00'),
            weight_grams=410,
        )
        Dish.objects.filter(pk=dish_cyrillic.pk).update(slug='Кальцоне з рікотою')

        migration_0005.transliterate_existing_slugs(django_apps, connection.schema_editor())

        cat_existing.refresh_from_db()
        cat_cyrillic.refresh_from_db()
        dish_existing.refresh_from_db()
        dish_cyrillic.refresh_from_db()

        self.assertEqual(cat_existing.slug, 'kaltsone')
        self.assertEqual(cat_cyrillic.slug, 'kaltsone-2')
        self.assertEqual(dish_existing.slug, 'kaltsone-z-rikotoiu')
        self.assertEqual(dish_cyrillic.slug, 'kaltsone-z-rikotoiu-2')

    def test_dish_image_field_rejects_data_uri_and_caches_exists(self):
        dish_data = Dish(
            category=self.category,
            title='Data URI Dish',
            slug='data-uri-dish',
            price=Decimal('190.00'),
            weight_grams=300,
            image='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB',
        )
        self.assertEqual(dish_data.image.url, '')
        self.assertEqual(dish_data.image.path, '')

        dish_file = Dish(
            category=self.category,
            title='Cached Image Dish',
            slug='cached-image-dish',
            price=Decimal('210.00'),
            weight_grams=320,
            image='dishes/cached-image.jpg',
        )
        with (
            patch.object(dish_file.image.storage, 'exists', return_value=True) as mock_exists,
            patch.object(
                dish_file.image.storage,
                'url',
                return_value='/media/dishes/cached-image.webp',
            ) as mock_url,
        ):
            first_url = dish_file.image.url
            second_url = dish_file.image.url
            third_url = dish_file.image.url

        self.assertEqual(first_url, '/media/dishes/cached-image.webp')
        self.assertEqual(second_url, '/media/dishes/cached-image.webp')
        self.assertEqual(third_url, '/media/dishes/cached-image.webp')
        self.assertLessEqual(mock_exists.call_count, 1)
        self.assertEqual(mock_exists.call_count, 1)
        mock_url.assert_called_once_with('dishes/cached-image.webp')

    def test_model_indexes_configured(self):
        cat_index_names = {idx.name for idx in Category._meta.indexes}
        self.assertIn('cat_active_order_idx', cat_index_names)
        dish_index_names = {idx.name for idx in Dish._meta.indexes}
        self.assertIn('dish_avail_cat_idx', dish_index_names)
        for field_name in ('price', 'calories', 'is_vegetarian', 'is_spicy', 'is_available'):
            self.assertTrue(Dish._meta.get_field(field_name).db_index)
