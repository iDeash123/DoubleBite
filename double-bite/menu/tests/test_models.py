from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase

from menu.models import Category, Dish, DishOption


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
