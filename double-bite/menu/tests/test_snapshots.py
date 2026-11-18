import io
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from accounts.models import Role, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from orders.models import Order
from PIL import Image

from menu.management.commands import load_snapshot as load_snapshot_module
from menu.models import Category, Dish


class SnapshotAndMediaCommandTests(TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.tmp_path = Path(self._temp_dir.name)
        self.category = Category.objects.create(
            name='Піца',
            slug='pizza',
            display_order=1,
            is_active=True,
        )
        self.dish = Dish.objects.create(
            category=self.category,
            title='Маргарита',
            slug='margherita',
            description='Класична піца',
            price=Decimal('250.00'),
            weight_grams=450,
            calories=780,
            is_available=True,
        )

    def _write_snapshot(self, payload: dict, filename: str = 'snap.json') -> Path:
        path = self.tmp_path / filename
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return path

    def test_dump_snapshot_rejects_traversal_and_invalid_tags(self):
        for bad_tag in ('../../evil', '../escape', 'bad/slash', 'space tag', 'tag.dot'):
            with self.assertRaises(CommandError):
                call_command('dump_snapshot', tag=bad_tag, output=str(self.tmp_path))

        self.assertEqual(list(self.tmp_path.glob('*.json')), [])

    def test_dump_snapshot_valid_tag_creates_snapshot_in_output_dir(self):
        out = io.StringIO()
        call_command(
            'dump_snapshot',
            tag='pre_v2-backup_01',
            output=str(self.tmp_path),
            stdout=out,
        )
        files = list(self.tmp_path.glob('snapshot_*_pre_v2-backup_01.json'))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text(encoding='utf-8'))
        self.assertEqual(data['_meta']['tag'], 'pre_v2-backup_01')
        self.assertEqual(len(data['dishes']), 1)
        self.assertEqual(data['dishes'][0]['slug'], 'margherita')

    def test_load_snapshot_filters_unwhitelisted_fields_and_warns_embedding(self):
        snap_path = self._write_snapshot(
            {
                'categories': [
                    {
                        'id': 501,
                        'name': 'Бургери',
                        'slug': 'burgers',
                        'icon': 'burger',
                        'display_order': 2,
                        'is_active': True,
                        'unexpected_cat_field': 'ignore-me',
                    }
                ],
                'dishes': [
                    {
                        'id': 601,
                        'category_id': 501,
                        'title': 'Трюфельний бургер',
                        'slug': 'truffle-burger',
                        'description': 'Опис',
                        'price': '340.00',
                        'image': 'dishes/truffle-burger.jpg',
                        'weight_grams': 380,
                        'calories': 720,
                        'allergens': 'глютен',
                        'is_vegetarian': False,
                        'is_spicy': False,
                        'is_available': True,
                        'embedding': [0.5] * 1024,
                        'evil_extra_field': 'should-not-pass',
                    }
                ],
            }
        )

        stdout = io.StringIO()
        call_command('load_snapshot', str(snap_path), only='menu', stdout=stdout)

        loaded_dish = Dish.objects.get(pk=601)
        self.assertEqual(loaded_dish.title, 'Трюфельний бургер')
        self.assertIsNone(loaded_dish.embedding)
        output_text = stdout.getvalue()
        self.assertIn('Dish.embedding', output_text)
        self.assertIn('None', output_text)

    def test_load_snapshot_blocks_superuser_and_staff_injection_and_overwrite(self):
        existing_regular = User.objects.create_user(
            email='regular@doublebite.ua',
            password='StrongPassword123!',
            first_name='Олена',
        )
        existing_superuser = User.objects.create_superuser(
            email='boss@doublebite.ua',
            password='BossPassword123!',
            first_name='Super',
        )
        existing_staff = User.objects.create_user(
            email='staff@doublebite.ua',
            password='StaffPassword123!',
            first_name='Staff',
            role=Role.RESTAURANT_ADMIN,
            is_staff=True,
        )

        snap_path = self._write_snapshot(
            {
                'users': [
                    {
                        'id': existing_regular.pk,
                        'email': 'regular@doublebite.ua',
                        'first_name': 'Олена-Оновлена',
                        'last_name': '',
                        'phone': '',
                        'role': Role.CUSTOMER,
                        'is_active': True,
                        'is_superuser': True,
                        'is_staff': True,
                        'password': 'pbkdf2_sha256$1$hack$hash',
                    },
                    {
                        'id': 9001,
                        'email': 'injected-admin@doublebite.ua',
                        'first_name': 'Injected',
                        'last_name': 'Admin',
                        'phone': '',
                        'role': Role.CUSTOMER,
                        'is_active': True,
                        'is_superuser': True,
                        'is_staff': True,
                        'password': 'pbkdf2_sha256$1$hack$hash',
                    },
                    {
                        'id': existing_superuser.pk,
                        'email': 'boss@doublebite.ua',
                        'first_name': 'HackedBoss',
                        'is_superuser': False,
                        'is_staff': False,
                    },
                    {
                        'id': 9002,
                        'email': 'STAFF@doublebite.ua',
                        'first_name': 'HackedStaff',
                        'is_superuser': False,
                        'is_staff': False,
                    },
                ]
            }
        )

        call_command('load_snapshot', str(snap_path), only='accounts', stdout=io.StringIO())

        existing_regular.refresh_from_db()
        self.assertEqual(existing_regular.first_name, 'Олена-Оновлена')
        self.assertFalse(existing_regular.is_superuser)
        self.assertFalse(existing_regular.is_staff)
        self.assertTrue(existing_regular.check_password('StrongPassword123!'))

        injected_user = User.objects.get(pk=9001)
        self.assertFalse(injected_user.is_superuser)
        self.assertFalse(injected_user.is_staff)
        self.assertFalse(injected_user.has_usable_password())

        existing_superuser.refresh_from_db()
        self.assertEqual(existing_superuser.first_name, 'Super')
        self.assertTrue(existing_superuser.is_superuser)

        existing_staff.refresh_from_db()
        self.assertEqual(existing_staff.first_name, 'Staff')
        self.assertTrue(existing_staff.is_staff)

    def test_load_snapshot_accounts_replace_requires_force_when_user_orders_exist(self):
        customer = User.objects.create_user(
            email='buyer@doublebite.ua',
            password='BuyerPassword123!',
        )
        Order.objects.create(
            user=customer,
            customer_name='Покупець',
            customer_phone='+380501112233',
            delivery_address='Київ, вул. Тестова, 1',
            total_amount=Decimal('250.00'),
        )
        snap_path = self._write_snapshot({'users': []})

        with patch('builtins.input', return_value='y'):
            with self.assertRaises(CommandError):
                call_command(
                    'load_snapshot',
                    str(snap_path),
                    only='accounts',
                    replace=True,
                )

        self.assertTrue(User.objects.filter(pk=customer.pk).exists())

        call_command(
            'load_snapshot',
            str(snap_path),
            only='accounts',
            replace=True,
            force=True,
            stdout=io.StringIO(),
        )
        self.assertFalse(User.objects.filter(pk=customer.pk).exists())

    def test_load_snapshot_executes_sqlsequencereset(self):
        snap_path = self._write_snapshot(
            {
                'categories': [
                    {
                        'id': 777,
                        'name': 'Десерти',
                        'slug': 'desserts',
                        'icon': 'cake',
                        'display_order': 3,
                        'is_active': True,
                    }
                ]
            }
        )

        executed_sql: list[str] = []
        real_call_command = call_command

        def fake_call_command(name, *args, **kwargs):
            if name == 'sqlsequencereset':
                stdout = kwargs['stdout']
                stdout.write(
                    'BEGIN;\n'
                    'SELECT 1;\n'
                    'COMMIT;\n'
                )
                return None
            return real_call_command(name, *args, **kwargs)

        def record_sql(execute, sql, params, many, context):
            executed_sql.append(sql)
            return execute(sql, params, many, context)

        with (
            patch.object(load_snapshot_module, 'call_command', side_effect=fake_call_command) as mock_cc,
            load_snapshot_module.connection.execute_wrapper(record_sql),
        ):
            call_command('load_snapshot', str(snap_path), only='menu', stdout=io.StringIO())

        mock_cc.assert_called_once()
        self.assertEqual(mock_cc.call_args.args[0], 'sqlsequencereset')
        self.assertEqual(mock_cc.call_args.args[1:], ('menu',))
        self.assertIn('SELECT 1', executed_sql)
        self.assertNotIn('BEGIN', executed_sql)
        self.assertNotIn('COMMIT', executed_sql)

    def test_convert_to_webp_delete_originals_and_negative_net_savings(self):
        dishes_dir = self.tmp_path / 'dishes'
        dishes_dir.mkdir(parents=True, exist_ok=True)
        src_img = dishes_dir / 'sample.jpg'
        Image.new('RGB', (40, 40), color=(200, 100, 50)).save(src_img, format='JPEG', quality=20)
        orig_size = src_img.stat().st_size

        def fake_webp_save(image_self, fp, format=None, **kwargs):
            Path(fp).write_bytes(b'WEBP-FAKE-LARGE-DATA-' + b'X' * (orig_size + 2048))

        stdout = io.StringIO()
        with (
            override_settings(MEDIA_ROOT=str(self.tmp_path)),
            patch.object(Image.Image, 'save', new=fake_webp_save),
        ):
            call_command('convert_to_webp', delete_originals=True, stdout=stdout)

        webp_path = dishes_dir / 'sample.webp'
        self.assertTrue(webp_path.exists())
        self.assertFalse(src_img.exists())
        out_text = stdout.getvalue()
        self.assertIn('Заощаджено -', out_text)
