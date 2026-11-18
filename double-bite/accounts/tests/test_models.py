import threading
from concurrent.futures import ThreadPoolExecutor

from allauth.account.models import EmailAddress
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections
from django.test import TestCase, TransactionTestCase

from accounts.models import DeliveryAddress, Role

User = get_user_model()


class AccountsConfigTest(TestCase):
    def test_app_is_installed(self):
        self.assertTrue(apps.is_installed('accounts'))


class UserModelTest(TestCase):
    def test_create_user_successful(self):
        user = User.objects.create_user(
            email='customer@example.com',
            password='secretpassword123',
            first_name='Олена',
            last_name='Коваленко',
            phone='+380501234567',
        )
        self.assertEqual(user.email, 'customer@example.com')
        self.assertTrue(user.check_password('secretpassword123'))
        self.assertEqual(user.role, Role.CUSTOMER)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_active)
        self.assertEqual(str(user), 'customer@example.com')
        self.assertEqual(user.first_name, 'Олена')
        self.assertEqual(user.phone, '+380501234567')
        email_addr = EmailAddress.objects.get(user=user, email='customer@example.com')
        self.assertTrue(email_addr.verified)
        self.assertTrue(email_addr.primary)

    def test_create_user_no_email_raises_error(self):
        with self.assertRaises(ValueError):
            User.objects.create_user(email='', password='secretpassword123')

    def test_create_user_email_normalized(self):
        user = User.objects.create_user(
            email='  User@Example.COM  ',
            password='secretpassword123',
            phone='050 123-45-67',
        )
        self.assertEqual(user.email, 'user@example.com')
        self.assertEqual(user.phone, '+380501234567')
        self.assertTrue(
            EmailAddress.objects.filter(
                user=user,
                email='user@example.com',
                verified=True,
                primary=True,
            ).exists()
        )

        user.email = '  Updated.User@Example.COM  '
        user.save()
        user.refresh_from_db()
        self.assertEqual(user.email, 'updated.user@example.com')

    def test_create_user_skip_email_address_flag(self):
        user = User.objects.create_user(
            email='noemailrow@example.com',
            password='secretpassword123',
            _skip_email_address=True,
        )
        self.assertFalse(EmailAddress.objects.filter(user=user).exists())

    def test_create_superuser_successful(self):
        admin_user = User.objects.create_superuser(
            email='Admin@DoubleBite.UA',
            password='adminpassword123',
        )
        self.assertEqual(admin_user.email, 'admin@doublebite.ua')
        self.assertTrue(admin_user.is_staff)
        self.assertTrue(admin_user.is_superuser)
        self.assertEqual(admin_user.role, Role.RESTAURANT_ADMIN)
        self.assertTrue(
            EmailAddress.objects.filter(
                user=admin_user,
                email='admin@doublebite.ua',
                verified=True,
                primary=True,
            ).exists()
        )

    def test_create_superuser_missing_is_staff(self):
        with self.assertRaises(ValueError):
            User.objects.create_superuser(
                email='fakeadmin@doublebite.ua',
                password='adminpassword123',
                is_staff=False,
            )

    def test_create_superuser_missing_is_superuser(self):
        with self.assertRaises(ValueError):
            User.objects.create_superuser(
                email='fakeadmin2@doublebite.ua',
                password='adminpassword123',
                is_superuser=False,
            )

    def test_user_email_uniqueness_case_insensitive(self):
        User.objects.create_user(
            email='duplicate@example.com',
            password='password1',
        )
        with self.assertRaises(IntegrityError):
            User.objects.create_user(
                email='Duplicate@Example.COM',
                password='password2',
            )

    def test_user_ci_unique_constraint_enforced_at_db_level(self):
        User.objects.create_user(
            email='ciconstraint@example.com',
            password='password1',
        )
        with self.assertRaises(IntegrityError):
            User.objects.bulk_create(
                [User(email='CiConstraint@Example.COM', password='password2')]
            )

    def test_user_phone_validation_via_full_clean(self):
        valid_user = User(
            email='validphone@example.com',
            phone='+380501234567',
        )
        valid_user.full_clean(exclude=['password'])

        blank_phone_user = User(
            email='blankphone@example.com',
            phone='',
        )
        blank_phone_user.full_clean(exclude=['password'])

        for bad_phone in ('12345', '0501234567', '+14155552671', 'invalid-phone'):
            invalid_user = User(
                email='badphone@example.com',
                phone=bad_phone,
            )
            with self.assertRaises(ValidationError) as ctx:
                invalid_user.full_clean(exclude=['password'])
            self.assertIn('phone', ctx.exception.error_dict)

    def test_user_roles(self):
        courier = User.objects.create_user(
            email='courier@doublebite.ua',
            password='courierpassword',
            role=Role.COURIER,
        )
        self.assertEqual(courier.role, Role.COURIER)


class DeliveryAddressModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='client@example.com',
            password='testpassword',
        )

    def test_create_delivery_address(self):
        address = DeliveryAddress.objects.create(
            user=self.user,
            title='Дім',
            city='Київ',
            street='вул. Хрещатик',
            building='22',
            apartment='15',
            floor='4',
            intercom='15K',
            is_default=True,
        )
        self.assertEqual(address.user, self.user)
        self.assertEqual(address.title, 'Дім')
        self.assertEqual(str(address), 'Дім: Київ, вул. Хрещатик 22')
        self.assertTrue(address.is_default)

    def test_format_full_address_with_and_without_apartment(self):
        full_address = DeliveryAddress(
            user=self.user,
            title='Дім',
            city='Київ',
            street='вул. Хрещатик',
            building='22',
            apartment='15',
            floor='4',
            intercom='15K',
        )
        self.assertEqual(
            full_address.format_full_address(),
            'Київ, вул. Хрещатик 22, кв. 15, поверх 4, домофон 15K',
        )

        house_address = DeliveryAddress(
            user=self.user,
            title='Приватний будинок',
            city='Київ',
            street='вул. Садова',
            building='7',
            apartment='',
            floor='',
            intercom='',
        )
        formatted_house = house_address.format_full_address()
        self.assertEqual(formatted_house, 'Київ, вул. Садова 7')
        self.assertNotIn(', кв. ', formatted_house)
        self.assertNotIn('кв.', formatted_house)

        no_apt_with_floor = DeliveryAddress(
            user=self.user,
            title='Офіс',
            city='Київ',
            street='вул. Січових Стрільців',
            building='10А',
            apartment='   ',
            floor='3',
            intercom='301',
        )
        formatted_office = no_apt_with_floor.format_full_address()
        self.assertEqual(
            formatted_office,
            'Київ, вул. Січових Стрільців 10А, поверх 3, домофон 301',
        )
        self.assertNotIn(', кв. ', formatted_office)

    def test_is_default_single_per_user(self):
        addr1 = DeliveryAddress.objects.create(
            user=self.user,
            title='Дім',
            city='Київ',
            street='вул. Хрещатик',
            building='22',
            is_default=True,
        )
        addr2 = DeliveryAddress.objects.create(
            user=self.user,
            title='Робота',
            city='Київ',
            street='вул. Володимирська',
            building='10',
            is_default=True,
        )
        addr1.refresh_from_db()
        addr2.refresh_from_db()
        self.assertFalse(addr1.is_default)
        self.assertTrue(addr2.is_default)

    def test_unique_default_delivery_address_constraint_enforced_at_db_level(self):
        with self.assertRaises(IntegrityError):
            DeliveryAddress.objects.bulk_create(
                [
                    DeliveryAddress(
                        user=self.user,
                        title='Перша',
                        city='Київ',
                        street='вул. Хрещатик',
                        building='1',
                        is_default=True,
                    ),
                    DeliveryAddress(
                        user=self.user,
                        title='Друга',
                        city='Київ',
                        street='вул. Хрещатик',
                        building='2',
                        is_default=True,
                    ),
                ]
            )

    def test_cascade_deletion(self):
        DeliveryAddress.objects.create(
            user=self.user,
            title='Дім',
            city='Київ',
            street='вул. Сагайдачного',
            building='5',
        )
        self.assertEqual(DeliveryAddress.objects.filter(user=self.user).count(), 1)
        self.user.delete()
        self.assertEqual(DeliveryAddress.objects.count(), 0)


class DeliveryAddressConcurrencyTest(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='concurrent.addr@example.com',
            password='testpassword',
        )

    def test_concurrent_save_is_default_maintains_single_default(self):
        workers = 6
        barrier = threading.Barrier(workers)

        def create_default_address(idx: int) -> int:
            try:
                barrier.wait(timeout=10)
                address = DeliveryAddress.objects.create(
                    user=self.user,
                    title=f'Адреса {idx}',
                    city='Київ',
                    street='вул. Тестова',
                    building=str(idx + 1),
                    is_default=True,
                )
                return address.pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            created_pks = list(pool.map(create_default_address, range(workers)))

        self.assertEqual(len(created_pks), workers)
        self.assertEqual(DeliveryAddress.objects.filter(user=self.user).count(), workers)
        self.assertEqual(
            DeliveryAddress.objects.filter(user=self.user, is_default=True).count(),
            1,
        )
