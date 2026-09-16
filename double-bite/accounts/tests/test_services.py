from django.contrib.auth import get_user_model
from django.test import TestCase

from accounts.models import Role
from accounts.services import UserService

User = get_user_model()


class UserServiceTest(TestCase):
    def test_register_user_service(self):
        user = UserService.register_user(
            email='service_user@doublebite.ua',
            password='service_pass_123',
            first_name='Леся',
            last_name='Українка',
            phone='+380630001122',
        )
        self.assertEqual(user.email, 'service_user@doublebite.ua')
        self.assertEqual(user.first_name, 'Леся')
        self.assertEqual(user.role, Role.CUSTOMER)

    def test_add_address_and_get_addresses(self):
        user = UserService.register_user(
            email='user_addr@doublebite.ua',
            password='pass',
        )
        addr = UserService.add_address(
            user=user,
            title='Офіс',
            city='Київ',
            street='вул. Богдана Хмельницького',
            building='50',
            is_default=True,
        )
        self.assertEqual(addr.title, 'Офіс')
        addresses = UserService.get_user_addresses(user)
        self.assertEqual(addresses.count(), 1)
        self.assertEqual(addresses.first(), addr)
