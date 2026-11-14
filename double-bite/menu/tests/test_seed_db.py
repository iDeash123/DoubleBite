import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from accounts.models import DeliveryAddress, Role, User
from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings
from orders.models import Order, OrderItem
from PIL import Image
from support.models import FAQKnowledge

from menu.models import Category, Dish, DishOption

PROJECT_MEDIA_ROOT = Path(settings.BASE_DIR) / 'media'


def _snapshot_media_tree(root: Path) -> set[tuple[str, int, int]]:
    if not root.exists():
        return set()
    return {
        (str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob('*')
        if p.is_file()
    }


class SeedDbCommandTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._media_before = _snapshot_media_tree(PROJECT_MEDIA_ROOT)
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.mock_media = Path(self._temp_dir.name)
        (self.mock_media / 'dishes').mkdir(parents=True, exist_ok=True)
        self._media_override = override_settings(MEDIA_ROOT=str(self.mock_media))
        self._media_override.enable()
        self.addCleanup(self._assert_project_media_untouched)
        self.addCleanup(self._media_override.disable)
        self.assertEqual(
            Path(Dish._meta.get_field('image').storage.location).resolve(),
            self.mock_media.resolve(),
        )

    def _assert_project_media_untouched(self) -> None:
        self.assertEqual(
            _snapshot_media_tree(PROJECT_MEDIA_ROOT),
            self._media_before,
            'seed_db tests must not write or modify files in project MEDIA_ROOT',
        )

    def _assert_all_dish_images_isolated_and_valid(self, dishes: list[Dish]) -> None:
        resolved_tmp_media = self.mock_media.resolve()
        for dish in dishes:
            self.assertIsNotNone(dish.image)
            self.assertTrue(dish.image.name.startswith('dishes/'))
            self.assertFalse(dish.image.name.startswith('http'))
            dish_path = Path(dish.image.path).resolve()
            self.assertTrue(
                dish_path.is_relative_to(resolved_tmp_media),
                f'Expected dish image path {dish_path} to live inside {resolved_tmp_media}',
            )
            self.assertTrue(os.path.exists(dish_path))
            self.assertGreater(os.path.getsize(dish_path), 0)
            with Image.open(dish_path) as img:
                img.verify()

    def test_seed_db_creates_expected_data_and_local_media_images(self) -> None:
        call_command('seed_db', skip_images=True)

        self.assertEqual(Category.objects.count(), 7)
        self.assertGreaterEqual(Dish.objects.count(), 105)
        self.assertGreaterEqual(DishOption.objects.count(), 100)
        self.assertGreaterEqual(FAQKnowledge.objects.count(), 5)
        self.assertGreaterEqual(DeliveryAddress.objects.count(), 3)
        self.assertGreaterEqual(Order.objects.count(), 2)
        self.assertGreaterEqual(OrderItem.objects.count(), 2)

        admin_user = User.objects.filter(role=Role.RESTAURANT_ADMIN).first()
        self.assertIsNotNone(admin_user)
        self.assertTrue(admin_user.is_superuser)

        customer_user = User.objects.filter(role=Role.CUSTOMER).first()
        self.assertIsNotNone(customer_user)

        dishes = list(Dish.objects.all())
        self._assert_all_dish_images_isolated_and_valid(dishes)
        self._assert_project_media_untouched()

    def test_seed_db_downloads_missing_image(self) -> None:
        fake_bytes = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' \",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9'

        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_bytes
        mock_resp.__enter__.return_value = mock_resp

        with patch('urllib.request.urlopen', return_value=mock_resp):
            call_command('seed_db')

        self.assertTrue(mock_resp.read.called)
        dishes = list(Dish.objects.all())
        self.assertGreaterEqual(len(dishes), 105)
        self._assert_all_dish_images_isolated_and_valid(dishes)
        self._assert_project_media_untouched()

    def test_seed_db_handles_corrupted_download_with_fallback(self) -> None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'<html>500 Internal Server Error</html>'
        mock_resp.__enter__.return_value = mock_resp

        with patch('urllib.request.urlopen', return_value=mock_resp):
            call_command('seed_db')

        dishes = list(Dish.objects.all())
        self.assertGreaterEqual(len(dishes), 105)
        self._assert_all_dish_images_isolated_and_valid(dishes)
        self._assert_project_media_untouched()

    def test_seed_db_fallback_when_download_fails(self) -> None:
        with patch('urllib.request.urlopen', side_effect=URLError('Network error')):
            call_command('seed_db')

        dishes = list(Dish.objects.all())
        self.assertGreaterEqual(len(dishes), 105)
        self._assert_all_dish_images_isolated_and_valid(dishes)
        self._assert_project_media_untouched()

    def test_seed_db_finds_image_in_media_root(self) -> None:
        sample_img = Image.new('RGB', (200, 200), color=(100, 150, 200))
        sample_path = self.mock_media / 'pizza-margherita-dop.jpg'
        sample_img.save(sample_path, format='JPEG')

        call_command('seed_db', skip_images=True)

        margherita = Dish.objects.get(slug='pizza-margherita-dop')
        self.assertEqual(margherita.image.name, 'dishes/pizza-margherita-dop.jpg')
        self._assert_all_dish_images_isolated_and_valid([margherita])
        self._assert_project_media_untouched()
