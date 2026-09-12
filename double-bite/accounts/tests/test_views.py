from django.contrib.auth import get_user_model
from django.test import AsyncClient, Client, TestCase
from django.urls import reverse

from accounts.models import DeliveryAddress, Role

User = get_user_model()


class HomeViewTest(TestCase):
    def setUp(self):
        self.client = Client()

    def test_home_page_renders_200(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'double bite')
        self.assertContains(response, 'fv-noir')
        self.assertContains(response, 'Plus Jakarta Sans')
        self.assertContains(response, 'htmx.org')
        self.assertContains(response, 'alpinejs')

    async def test_home_page_renders_200_async(self):
        async_client = AsyncClient()
        response = await async_client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'double bite')


class RegisterViewTest(TestCase):
    def setUp(self):
        self.client = Client()

    def test_register_page_get(self):
        response = self.client.get(reverse('accounts:register'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/register.html')

    def test_register_user_success(self):
        payload = {
            'email': 'newuser@doublebite.ua',
            'first_name': 'Тарас',
            'last_name': 'Шевченко',
            'phone': '+380671112233',
            'password': 'SecurePassword123!',
            'password_confirm': 'SecurePassword123!',
        }
        response = self.client.post(reverse('accounts:register'), payload)
        self.assertRedirects(response, reverse('accounts:profile'))
        user = User.objects.filter(email='newuser@doublebite.ua').first()
        self.assertIsNotNone(user)
        self.assertEqual(user.first_name, 'Тарас')
        self.assertEqual(user.role, Role.CUSTOMER)

    def test_register_password_mismatch(self):
        payload = {
            'email': 'mismatch@doublebite.ua',
            'password': 'PasswordOne1!',
            'password_confirm': 'PasswordTwo2!',
        }
        response = self.client.post(reverse('accounts:register'), payload)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context['form'], 'password_confirm', 'Паролі не співпадають.')
        self.assertFalse(User.objects.filter(email='mismatch@doublebite.ua').exists())

    def test_register_duplicate_email(self):
        User.objects.create_user(email='existing@doublebite.ua', password='password123')
        payload = {
            'email': 'existing@doublebite.ua',
            'password': 'Password123!',
            'password_confirm': 'Password123!',
        }
        response = self.client.post(reverse('accounts:register'), payload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)

    def test_authenticated_user_redirected(self):
        user = User.objects.create_user(email='auth@doublebite.ua', password='password123')
        self.client.force_login(user)
        response = self.client.get(reverse('accounts:register'))
        self.assertRedirects(response, reverse('accounts:profile'))


class LoginViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            email='user@doublebite.ua',
            password='TestPassword123!',
        )

    def test_login_page_get(self):
        response = self.client.get(reverse('accounts:login'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/login.html')

    def test_login_success(self):
        response = self.client.post(
            reverse('accounts:login'),
            {'email': 'user@doublebite.ua', 'password': 'TestPassword123!'},
        )
        self.assertRedirects(response, reverse('accounts:profile'))

    def test_login_invalid_password(self):
        response = self.client.post(
            reverse('accounts:login'),
            {'email': 'user@doublebite.ua', 'password': 'WrongPassword!'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Невірний email або пароль.')

    def test_authenticated_user_redirected(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('accounts:login'))
        self.assertRedirects(response, reverse('accounts:profile'))


class LogoutViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            email='loggedout@doublebite.ua',
            password='password123',
        )

    def test_logout_post(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('accounts:logout'))
        self.assertRedirects(response, reverse('home'))

    def test_logout_get(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('accounts:logout'))
        self.assertRedirects(response, reverse('home'))


class ProfileViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            email='profile@doublebite.ua',
            password='password123',
            first_name='Іван',
            last_name='Франко',
        )

    def test_unauthenticated_redirects(self):
        response = self.client.get(reverse('accounts:profile'))
        self.assertEqual(response.status_code, 302)

    def test_profile_get_authenticated(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('accounts:profile'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'profile@doublebite.ua')
        self.assertContains(response, 'Іван')

    def test_update_profile(self):
        self.client.force_login(self.user)
        payload = {
            'action': 'update_profile',
            'first_name': 'Богдан',
            'last_name': 'Хмельницький',
            'phone': '+380509998877',
        }
        response = self.client.post(reverse('accounts:profile'), payload)
        self.assertRedirects(response, reverse('accounts:profile'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Богдан')
        self.assertEqual(self.user.phone, '+380509998877')

    def test_add_delivery_address(self):
        self.client.force_login(self.user)
        payload = {
            'action': 'add_address',
            'title': 'Дім',
            'city': 'Київ',
            'street': 'вул. Велика Васильківська',
            'building': '12',
            'apartment': '4',
            'floor': '2',
            'intercom': '4',
            'is_default': True,
        }
        response = self.client.post(reverse('accounts:profile'), payload)
        self.assertRedirects(response, reverse('accounts:profile'))
        address = DeliveryAddress.objects.filter(user=self.user, street='вул. Велика Васильківська').first()
        self.assertIsNotNone(address)
        self.assertTrue(address.is_default)


class AddressDeleteViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user1 = User.objects.create_user(email='user1@doublebite.ua', password='password123')
        self.user2 = User.objects.create_user(email='user2@doublebite.ua', password='password123')
        self.address1 = DeliveryAddress.objects.create(
            user=self.user1,
            title='Дім',
            city='Київ',
            street='вул. Шота Руставелі',
            building='15',
        )

    def test_delete_own_address(self):
        self.client.force_login(self.user1)
        response = self.client.post(reverse('accounts:delete_address', kwargs={'pk': self.address1.pk}))
        self.assertRedirects(response, reverse('accounts:profile'))
        self.assertFalse(DeliveryAddress.objects.filter(pk=self.address1.pk).exists())

    def test_cannot_delete_other_user_address(self):
        self.client.force_login(self.user2)
        response = self.client.post(reverse('accounts:delete_address', kwargs={'pk': self.address1.pk}))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(DeliveryAddress.objects.filter(pk=self.address1.pk).exists())
