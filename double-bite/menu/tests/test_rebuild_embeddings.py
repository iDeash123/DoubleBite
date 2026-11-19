from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase
from support.vector_search import EMBEDDING_DIM

from menu.models import Category, Dish


class RebuildDishEmbeddingsCommandTest(TestCase):
    def setUp(self):
        self.active_cat = Category.objects.create(
            name='Піца',
            slug='pizza-rebuild',
            is_active=True,
        )
        self.inactive_cat = Category.objects.create(
            name='Архів',
            slug='archive-rebuild',
            is_active=False,
        )

        self.sentinel_vec = [0.5] * EMBEDDING_DIM
        self.dish_missing = Dish.objects.create(
            category=self.active_cat,
            title='Маргарита без вектора',
            slug='margherita-missing',
            description='Томати та сир моцарела',
            price=Decimal('220.00'),
            weight_grams=400,
            is_available=True,
            embedding=None,
        )
        self.dish_existing = Dish.objects.create(
            category=self.active_cat,
            title='Пепероні з вектором',
            slug='pepperoni-existing',
            description='Гостра салямі та чилі',
            price=Decimal('260.00'),
            weight_grams=430,
            is_available=True,
            embedding=self.sentinel_vec,
        )
        self.dish_unavailable = Dish.objects.create(
            category=self.active_cat,
            title='Недоступна страва',
            slug='unavailable-rebuild',
            price=Decimal('280.00'),
            weight_grams=400,
            is_available=False,
            embedding=None,
        )
        self.dish_inactive_cat = Dish.objects.create(
            category=self.inactive_cat,
            title='Архівна страва',
            slug='inactive-cat-rebuild',
            price=Decimal('180.00'),
            weight_grams=300,
            is_available=True,
            embedding=None,
        )

    def test_only_missing_skips_existing_and_unavailable(self):
        out = StringIO()
        call_command('rebuild_dish_embeddings', only_missing=True, stdout=out)
        text = out.getvalue()
        self.assertIn('всього=1', text)
        self.assertIn('оновлено=1', text)

        self.dish_missing.refresh_from_db()
        self.dish_existing.refresh_from_db()
        self.dish_unavailable.refresh_from_db()
        self.dish_inactive_cat.refresh_from_db()

        self.assertIsNotNone(self.dish_missing.embedding)
        self.assertEqual(len(self.dish_missing.embedding), EMBEDDING_DIM)
        self.assertEqual(list(self.dish_existing.embedding), self.sentinel_vec)
        self.assertIsNone(self.dish_unavailable.embedding)
        self.assertIsNone(self.dish_inactive_cat.embedding)

    def test_force_and_batch_size_rebuilds_existing(self):
        out = StringIO()
        call_command(
            'rebuild_dish_embeddings',
            only_missing=True,
            force=True,
            batch_size=1,
            stdout=out,
        )
        text = out.getvalue()
        self.assertIn('всього=2', text)
        self.assertIn('оновлено=2', text)

        self.dish_existing.refresh_from_db()
        self.assertNotEqual(list(self.dish_existing.embedding), self.sentinel_vec)

    def test_include_unavailable_updates_all_dishes(self):
        out = StringIO()
        call_command(
            'rebuild_dish_embeddings',
            only_missing=True,
            include_unavailable=True,
            batch_size=2,
            stdout=out,
        )
        text = out.getvalue()
        self.assertIn('всього=3', text)
        self.assertIn('оновлено=3', text)

        self.dish_unavailable.refresh_from_db()
        self.dish_inactive_cat.refresh_from_db()
        self.assertIsNotNone(self.dish_unavailable.embedding)
        self.assertIsNotNone(self.dish_inactive_cat.embedding)

    def test_invalid_batch_size_raises_command_error(self):
        with self.assertRaises(CommandError):
            call_command('rebuild_dish_embeddings', batch_size=0)

    def test_partial_embedding_failure_counted_cleanly(self):
        out = StringIO()
        with patch(
            'menu.management.commands.rebuild_dish_embeddings.get_embeddings_batch',
            return_value=[None, [0.1] * EMBEDDING_DIM],
        ):
            call_command('rebuild_dish_embeddings', batch_size=2, stdout=out)
        text = out.getvalue()
        self.assertIn('всього=2', text)
        self.assertIn('оновлено=1', text)
        self.assertIn('помилок=1', text)
