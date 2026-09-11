from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase

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

    def test_create_user_no_email_raises_error(self):
        with self.assertRaises(ValueError):
            User.objects.create_user(email='', password='secretpassword123')

    def test_create_user_email_normalized(self):
        user = User.objects.create_user(
            email='User@Example.COM',
            password='secretpassword123',
        )
        self.assertEqual(user.email, 'User@example.com')

    def test_create_superuser_successful(self):
        admin_user = User.objects.create_superuser(
            email='admin@doublebite.ua',
            password='adminpassword123',
        )
        self.assertEqual(admin_user.email, 'admin@doublebite.ua')
        self.assertTrue(admin_user.is_staff)
        self.assertTrue(admin_user.is_superuser)
        self.assertEqual(admin_user.role, Role.RESTAURANT_ADMIN)

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

    def test_user_email_uniqueness(self):
        User.objects.create_user(
            email='duplicate@example.com',
            password='password1',
        )
        with self.assertRaises(IntegrityError):
            User.objects.create_user(
                email='duplicate@example.com',
                password='password2',
            )

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
