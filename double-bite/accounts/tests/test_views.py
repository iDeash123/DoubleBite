import html
import re
from unittest.mock import patch

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.db import IntegrityError
from django.test import AsyncClient, Client, TestCase, override_settings
from django.urls import reverse
from orders.models import Order

from accounts.forms import UserRegistrationForm
from accounts.models import DeliveryAddress, Role

User = get_user_model()


class HomeViewTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()

    def test_home_page_renders_200(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'double bite')
        self.assertContains(response, 'db-noir')
        self.assertContains(response, 'fonts.css')
        self.assertContains(response, 'htmx.min.js')
        self.assertContains(response, 'alpine.min.js')

    async def test_home_page_renders_200_async(self):
        async_client = AsyncClient()
        response = await async_client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'double bite')


class RegisterViewTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        mail.outbox.clear()

    def tearDown(self):
        cache.clear()

    def test_register_page_get(self):
        response = self.client.get(reverse('accounts:register'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/register.html')

    def test_register_user_success(self):
        payload = {
            'email': 'NewUser@DoubleBite.UA',
            'first_name': 'Тарас',
            'last_name': 'Шевченко',
            'phone': '+380671112233',
            'password': 'SecurePassword123!',
            'password_confirm': 'SecurePassword123!',
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('accounts:register'), payload)

        self.assertRedirects(response, reverse('account_email_verification_sent'))
        self.assertNotIn('_auth_user_id', self.client.session)

        user = User.objects.filter(email='newuser@doublebite.ua').first()
        self.assertIsNotNone(user)
        self.assertEqual(user.email, 'newuser@doublebite.ua')
        self.assertEqual(user.first_name, 'Тарас')
        self.assertEqual(user.role, Role.CUSTOMER)

        email_addr = EmailAddress.objects.get(user=user, email='newuser@doublebite.ua')
        self.assertFalse(email_addr.verified)
        self.assertTrue(email_addr.primary)

        self.assertEqual(len(mail.outbox), 2)
        subjects = [msg.subject for msg in mail.outbox]
        self.assertIn('Ласкаво просимо до Double Bite!', subjects)
        confirm_email = next(
            msg for msg in mail.outbox if msg.subject != 'Ласкаво просимо до Double Bite!'
        )
        self.assertIn('/accounts/confirm-email/', confirm_email.body)

    def test_register_email_confirmation_e2e_enables_login(self):
        payload = {
            'email': 'ConfirmFlow@DoubleBite.UA',
            'first_name': 'Оксана',
            'last_name': 'Забужко',
            'phone': '+380671112299',
            'password': 'SecurePassword123!',
            'password_confirm': 'SecurePassword123!',
        }
        with self.captureOnCommitCallbacks(execute=True):
            reg_response = self.client.post(reverse('accounts:register'), payload)

        self.assertRedirects(reg_response, reverse('account_email_verification_sent'))
        self.assertNotIn('_auth_user_id', self.client.session)

        user = User.objects.get(email='confirmflow@doublebite.ua')
        email_addr = EmailAddress.objects.get(user=user, email='confirmflow@doublebite.ua')
        self.assertFalse(email_addr.verified)

        blocked_login = self.client.post(
            reverse('accounts:login'),
            {'email': 'confirmflow@doublebite.ua', 'password': 'SecurePassword123!'},
        )
        self.assertEqual(blocked_login.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

        confirm_email = next(
            msg for msg in mail.outbox if msg.subject != 'Ласкаво просимо до Double Bite!'
        )
        match = re.search(r'(/accounts/confirm-email/[^\s]+)', confirm_email.body)
        self.assertIsNotNone(match, f'Confirmation URL missing in:\n{confirm_email.body}')
        confirm_path = match.group(1)

        confirm_get = self.client.get(confirm_path)
        self.assertEqual(confirm_get.status_code, 200)
        self.assertTemplateUsed(confirm_get, 'account/email_confirm.html')

        confirm_post = self.client.post(confirm_path)
        self.assertEqual(confirm_post.status_code, 302)

        email_addr.refresh_from_db()
        self.assertTrue(email_addr.verified)

        login_response = self.client.post(
            reverse('accounts:login'),
            {'email': 'confirmflow@doublebite.ua', 'password': 'SecurePassword123!'},
        )
        self.assertRedirects(login_response, reverse('accounts:profile'))
        self.assertEqual(self.client.session.get('_auth_user_id'), str(user.pk))

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

    def test_register_duplicate_email_case_insensitive(self):
        User.objects.create_user(email='existing@doublebite.ua', password='password123')
        payload = {
            'email': 'EXISTING@DoubleBite.UA',
            'password': 'Password123!',
            'password_confirm': 'Password123!',
        }
        response = self.client.post(reverse('accounts:register'), payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn('email', response.context['form'].errors)

    def test_register_invalid_phone_rejected(self):
        payload = {
            'email': 'badphone@doublebite.ua',
            'phone': '12345',
            'password': 'SecurePassword123!',
            'password_confirm': 'SecurePassword123!',
        }
        response = self.client.post(reverse('accounts:register'), payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn('phone', response.context['form'].errors)
        self.assertFalse(User.objects.filter(email='badphone@doublebite.ua').exists())

    def test_register_concurrent_integrity_error_returns_400(self):
        payload = {
            'email': 'race@doublebite.ua',
            'first_name': 'Тест',
            'last_name': 'Гонка',
            'phone': '+380671112233',
            'password': 'SecurePassword123!',
            'password_confirm': 'SecurePassword123!',
        }
        with (
            patch.object(
                UserRegistrationForm,
                'save',
                side_effect=IntegrityError('UNIQUE constraint failed'),
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(reverse('accounts:register'), payload)

        self.assertEqual(response.status_code, 400)
        self.assertTemplateUsed(response, 'accounts/register.html')
        self.assertIn('email', response.context['form'].errors)
        self.assertEqual(len(mail.outbox), 0)

    def test_authenticated_user_redirected(self):
        user = User.objects.create_user(email='auth@doublebite.ua', password='password123')
        self.client.force_login(user)
        response = self.client.get(reverse('accounts:register'))
        self.assertRedirects(response, reverse('accounts:profile'))

    @override_settings(ACCOUNT_EMAIL_VERIFICATION='none')
    def test_register_open_redirect_prevented(self):
        payload = {
            'email': 'openredirect@doublebite.ua',
            'first_name': 'Тест',
            'last_name': 'Юзер',
            'phone': '+380671112244',
            'password': 'SecurePassword123!',
            'password_confirm': 'SecurePassword123!',
        }
        response = self.client.post(
            reverse('accounts:register') + '?next=https://evil.com/phishing',
            payload,
        )
        self.assertRedirects(
            response,
            reverse('accounts:profile'),
            fetch_redirect_response=False,
        )

    @override_settings(ACCOUNT_EMAIL_VERIFICATION='none')
    def test_register_safe_redirect_allowed_and_require_https_enforced(self):
        payload = {
            'email': 'safenext@doublebite.ua',
            'first_name': 'Тест',
            'last_name': 'Юзер',
            'phone': '+380671112255',
            'password': 'SecurePassword123!',
            'password_confirm': 'SecurePassword123!',
        }
        response = self.client.post(
            reverse('accounts:register') + '?next=/menu/',
            payload,
        )
        self.assertRedirects(response, '/menu/', fetch_redirect_response=False)

        downgrade_resp = self.client.post(
            reverse('accounts:register') + '?next=http://testserver/menu/',
            {**payload, 'email': 'httpscheck@doublebite.ua'},
            secure=True,
        )
        self.assertRedirects(
            downgrade_resp,
            reverse('accounts:profile'),
            fetch_redirect_response=False,
        )

    def test_register_weak_password_rejected(self):
        payload = {
            'email': 'weakpass@doublebite.ua',
            'first_name': 'Тест',
            'last_name': 'Юзер',
            'phone': '+380671112266',
            'password': '123',
            'password_confirm': '123',
        }
        response = self.client.post(reverse('accounts:register'), payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn('password', response.context['form'].errors)
        self.assertFalse(User.objects.filter(email='weakpass@doublebite.ua').exists())

    def test_register_rate_limited_after_repeated_attempts(self):
        for i in range(10):
            client = Client()
            client.post(
                reverse('accounts:register'),
                {
                    'email': f'ratelimit{i}@doublebite.ua',
                    'first_name': 'Тест',
                    'last_name': 'Юзер',
                    'phone': '+380671112288',
                    'password': 'Password123!',
                    'password_confirm': 'Password123!',
                },
            )
        client = Client()
        response = client.post(
            reverse('accounts:register'),
            {
                'email': 'ratelimit11@doublebite.ua',
                'first_name': 'Тест',
                'last_name': 'Юзер',
                'phone': '+380671112288',
                'password': 'Password123!',
                'password_confirm': 'Password123!',
            },
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response.headers)
        self.assertGreaterEqual(int(response.headers['Retry-After']), 1)
        self.assertContains(response, 'Забагато спроб реєстрації', status_code=429)

    def test_allauth_signup_path_uses_rate_limited_register_view(self):
        payload = {
            'email': 'signupalias@doublebite.ua',
            'password': 'Password123!',
            'password_confirm': 'Mismatch123!',
        }
        for _ in range(10):
            resp = self.client.post('/accounts/signup/', payload)
            self.assertEqual(resp.status_code, 200)
        blocked = self.client.post('/accounts/signup/', payload)
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('Retry-After', blocked.headers)


class LoginViewTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.user = User.objects.create_user(
            email='user@doublebite.ua',
            password='TestPassword123!',
        )

    def tearDown(self):
        cache.clear()

    def test_login_page_get(self):
        response = self.client.get(reverse('accounts:login'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/login.html')

    def test_login_success_and_case_insensitive_email(self):
        response = self.client.post(
            reverse('accounts:login'),
            {'email': '  USER@DoubleBite.UA  ', 'password': 'TestPassword123!'},
        )
        self.assertRedirects(response, reverse('accounts:profile'))
        self.assertEqual(self.client.session.get('_auth_user_id'), str(self.user.pk))

    def test_login_unverified_user_blocked_and_verified_allowed(self):
        unverified_user = User.objects.create_user(
            email='unverified@doublebite.ua',
            password='TestPassword123!',
            _skip_email_address=True,
        )
        email_addr = EmailAddress.objects.create(
            user=unverified_user,
            email=unverified_user.email,
            verified=False,
            primary=True,
        )

        blocked_resp = self.client.post(
            reverse('accounts:login'),
            {'email': unverified_user.email, 'password': 'TestPassword123!'},
        )
        self.assertEqual(blocked_resp.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertContains(blocked_resp, 'Email не підтверджено')
        self.assertContains(blocked_resp, reverse('account_email'))

        email_addr.verified = True
        email_addr.save(update_fields=['verified'])

        allowed_resp = self.client.post(
            reverse('accounts:login'),
            {'email': unverified_user.email, 'password': 'TestPassword123!'},
        )
        self.assertRedirects(allowed_resp, reverse('accounts:profile'))
        self.assertEqual(self.client.session.get('_auth_user_id'), str(unverified_user.pk))

    def test_login_open_redirect_prevented(self):
        response = self.client.post(
            reverse('accounts:login') + '?next=https://evil.com/phishing',
            {'email': 'user@doublebite.ua', 'password': 'TestPassword123!'},
        )
        self.assertRedirects(response, reverse('accounts:profile'))

    def test_login_safe_redirect_allowed(self):
        response = self.client.post(
            reverse('accounts:login') + '?next=/menu/',
            {'email': 'user@doublebite.ua', 'password': 'TestPassword123!'},
        )
        self.assertRedirects(response, '/menu/')

    def test_login_require_https_for_next_parameter(self):
        blocked_http_next = self.client.post(
            reverse('accounts:login') + '?next=http://testserver/menu/',
            {'email': 'user@doublebite.ua', 'password': 'TestPassword123!'},
            secure=True,
        )
        self.assertRedirects(
            blocked_http_next,
            reverse('accounts:profile'),
            fetch_redirect_response=False,
        )

        self.client.logout()
        allowed_https_next = self.client.post(
            reverse('accounts:login') + '?next=https://testserver/menu/',
            {'email': 'user@doublebite.ua', 'password': 'TestPassword123!'},
            secure=True,
        )
        self.assertRedirects(
            allowed_https_next,
            'https://testserver/menu/',
            fetch_redirect_response=False,
        )

    def test_login_invalid_password(self):
        response = self.client.post(
            reverse('accounts:login'),
            {'email': 'user@doublebite.ua', 'password': 'WrongPassword!'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Невірний email або пароль.')

    def test_login_rate_limited_after_repeated_failures_without_calling_authenticate(self):
        for _ in range(5):
            resp = self.client.post(
                reverse('accounts:login'),
                {'email': 'user@doublebite.ua', 'password': 'WrongPassword!'},
            )
            self.assertEqual(resp.status_code, 200)

        with patch('accounts.forms.authenticate') as mock_authenticate:
            response = self.client.post(
                reverse('accounts:login'),
                {'email': 'user@doublebite.ua', 'password': 'TestPassword123!'},
            )

        mock_authenticate.assert_not_called()
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response.headers)
        self.assertGreaterEqual(int(response.headers['Retry-After']), 1)
        self.assertContains(response, 'Забагато невдалих спроб входу', status_code=429)

    def test_login_email_rate_limited_across_different_ips(self):
        for idx in range(5):
            resp = self.client.post(
                reverse('accounts:login'),
                {'email': 'user@doublebite.ua', 'password': 'WrongPassword!'},
                REMOTE_ADDR=f'198.51.100.{10 + idx}',
            )
            self.assertEqual(resp.status_code, 200)

        with patch('accounts.forms.authenticate') as mock_authenticate:
            blocked = self.client.post(
                reverse('accounts:login'),
                {'email': '  USER@DoubleBite.UA  ', 'password': 'TestPassword123!'},
                REMOTE_ADDR='203.0.113.99',
            )

        mock_authenticate.assert_not_called()
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('Retry-After', blocked.headers)
        self.assertContains(blocked, 'Забагато невдалих спроб входу', status_code=429)

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

    def test_logout_get_not_allowed(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('accounts:logout'))
        self.assertEqual(response.status_code, 405)


class ProfileViewTest(TestCase):
    def setUp(self):
        cache.clear()
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
        self.assertContains(response, 'Активний')

    def test_profile_contains_logout_button(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('accounts:profile'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'action="{reverse("accounts:logout")}"', count=2)
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, 'Вийти з кабінету', count=1)

    def test_profile_displays_security_and_oauth_connections(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('accounts:profile'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('account_change_password'))
        self.assertNotContains(response, reverse('account_set_password'))
        self.assertContains(response, reverse('socialaccount_connections'))

        SocialAccount.objects.create(
            user=self.user,
            provider='google',
            uid='profile-google-1',
            extra_data={'email': 'profile@gmail.com'},
        )
        self.user.set_unusable_password()
        self.user.save(update_fields=['password'])
        self.client.force_login(self.user)

        oauth_profile_resp = self.client.get(reverse('accounts:profile'))
        self.assertEqual(oauth_profile_resp.status_code, 200)
        self.assertContains(oauth_profile_resp, reverse('account_set_password'))
        self.assertContains(oauth_profile_resp, 'profile@gmail.com')

    def test_profile_logout_post_terminates_session(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('accounts:logout'))
        self.assertRedirects(response, reverse('home'))
        self.assertNotIn('_auth_user_id', self.client.session)
        profile_response = self.client.get(reverse('accounts:profile'))
        self.assertEqual(profile_response.status_code, 302)
        self.assertIn('/accounts/login/', profile_response.url)

    def test_update_profile(self):
        self.client.force_login(self.user)
        payload = {
            'action': 'update_profile',
            'first_name': 'Богдан',
            'last_name': 'Хмельницький',
            'phone': '050 999-88-77',
        }
        response = self.client.post(reverse('accounts:profile'), payload)
        self.assertRedirects(response, reverse('accounts:profile'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Богдан')
        self.assertEqual(self.user.phone, '+380509998877')

        bad_phone_resp = self.client.post(
            reverse('accounts:profile'),
            {'action': 'update_profile', 'first_name': 'Богдан', 'phone': 'invalid'},
        )
        self.assertEqual(bad_phone_resp.status_code, 200)
        self.assertIn('phone', bad_phone_resp.context['user_form'].errors)

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
        address = DeliveryAddress.objects.filter(
            user=self.user,
            street='вул. Велика Васильківська',
        ).first()
        self.assertIsNotNone(address)
        self.assertTrue(address.is_default)

        profile_resp = self.client.get(reverse('accounts:profile'))
        self.assertContains(profile_resp, address.format_full_address())

    def test_profile_address_form_field_errors_rendered(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('accounts:profile'),
            {
                'action': 'add_address',
                'title': 'X' * 60,
                'city': 'Київ',
                'street': '',
                'building': '',
            },
        )
        self.assertEqual(response.status_code, 200)
        address_form = response.context['address_form']
        self.assertIn('title', address_form.errors)
        self.assertIn('street', address_form.errors)
        self.assertIn('building', address_form.errors)
        rendered = html.unescape(response.content.decode('utf-8'))
        self.assertIn(address_form.errors['title'][0], rendered)
        self.assertIn(address_form.errors['street'][0], rendered)
        self.assertIn(address_form.errors['building'][0], rendered)

    def test_profile_address_edit_and_set_default_crud(self):
        other_user = User.objects.create_user(email='other@doublebite.ua', password='password123')
        other_addr = DeliveryAddress.objects.create(
            user=other_user,
            title='Чужа',
            city='Київ',
            street='вул. Інша',
            building='99',
        )
        addr1 = DeliveryAddress.objects.create(
            user=self.user,
            title='Дім',
            city='Київ',
            street='вул. Хрещатик',
            building='10',
            is_default=True,
        )
        addr2 = DeliveryAddress.objects.create(
            user=self.user,
            title='Офіс',
            city='Київ',
            street='вул. Богдана Хмельницького',
            building='5',
            is_default=False,
        )
        self.client.force_login(self.user)

        edit_get = self.client.get(reverse('accounts:address_edit', kwargs={'pk': addr2.pk}))
        self.assertEqual(edit_get.status_code, 200)
        self.assertEqual(edit_get.context['editing_address'], addr2)

        bad_edit = self.client.post(
            reverse('accounts:address_edit', kwargs={'pk': addr2.pk}),
            {
                'action': 'edit_address',
                'title': 'Офіс',
                'city': 'Київ',
                'street': '',
                'building': '5',
            },
        )
        self.assertEqual(bad_edit.status_code, 200)
        self.assertIn('street', bad_edit.context['edit_address_form'].errors)
        self.assertIn(
            bad_edit.context['edit_address_form'].errors['street'][0],
            html.unescape(bad_edit.content.decode('utf-8')),
        )

        good_edit = self.client.post(
            reverse('accounts:address_edit', kwargs={'pk': addr2.pk}),
            {
                'action': 'edit_address',
                'title': 'Студія',
                'city': 'Львів',
                'street': 'пл. Ринок',
                'building': '14',
                'apartment': '',
                'floor': '2',
                'intercom': '',
            },
        )
        self.assertRedirects(good_edit, reverse('accounts:profile'))
        addr2.refresh_from_db()
        self.assertEqual(addr2.title, 'Студія')
        self.assertEqual(addr2.city, 'Львів')
        self.assertEqual(addr2.street, 'пл. Ринок')
        self.assertEqual(addr2.building, '14')

        set_default_resp = self.client.post(
            reverse('accounts:address_set_default', kwargs={'pk': addr2.pk}),
            {'action': 'set_default_address'},
        )
        self.assertRedirects(set_default_resp, reverse('accounts:profile'))
        addr1.refresh_from_db()
        addr2.refresh_from_db()
        self.assertFalse(addr1.is_default)
        self.assertTrue(addr2.is_default)

        other_edit = self.client.post(
            reverse('accounts:address_edit', kwargs={'pk': other_addr.pk}),
            {'title': 'Hack', 'city': 'Київ', 'street': 'X', 'building': '1'},
        )
        self.assertEqual(other_edit.status_code, 404)
        other_default = self.client.post(
            reverse('accounts:address_set_default', kwargs={'pk': other_addr.pk}),
        )
        self.assertEqual(other_default.status_code, 404)

    def test_profile_unknown_action_returns_400(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('accounts:profile'),
            {'action': 'unknown_profile_action'},
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Невідома дія.', status_code=400)


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
            is_default=False,
        )

    def test_delete_own_address(self):
        self.client.force_login(self.user1)
        response = self.client.post(
            reverse('accounts:delete_address', kwargs={'pk': self.address1.pk})
        )
        self.assertRedirects(response, reverse('accounts:profile'))
        self.assertFalse(DeliveryAddress.objects.filter(pk=self.address1.pk).exists())

    def test_delete_default_address_promotes_latest_remaining_address(self):
        addr2 = DeliveryAddress.objects.create(
            user=self.user1,
            title='Офіс',
            city='Київ',
            street='вул. Володимирська',
            building='42',
            is_default=False,
        )
        addr3 = DeliveryAddress.objects.create(
            user=self.user1,
            title='Студія',
            city='Київ',
            street='вул. Саксаганського',
            building='7',
            is_default=True,
        )

        self.client.force_login(self.user1)
        response = self.client.post(
            reverse('accounts:delete_address', kwargs={'pk': addr3.pk})
        )
        self.assertRedirects(response, reverse('accounts:profile'))
        self.assertFalse(DeliveryAddress.objects.filter(pk=addr3.pk).exists())

        self.address1.refresh_from_db()
        addr2.refresh_from_db()
        self.assertFalse(self.address1.is_default)
        self.assertTrue(addr2.is_default)

    def test_cannot_delete_other_user_address(self):
        self.client.force_login(self.user2)
        response = self.client.post(
            reverse('accounts:delete_address', kwargs={'pk': self.address1.pk})
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(DeliveryAddress.objects.filter(pk=self.address1.pk).exists())


class AccountsSecurityAndRateLimitTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()

    def tearDown(self):
        cache.clear()

    def test_register_rate_limited_after_10_attempts(self):
        payload = {
            'email': 'ratelimit@doublebite.ua',
            'password': 'Password123!',
            'password_confirm': 'Mismatch123!',
        }
        for _ in range(10):
            resp = self.client.post(
                reverse('accounts:register'),
                payload,
                REMOTE_ADDR='198.51.100.1',
            )
            self.assertEqual(resp.status_code, 200)
        response = self.client.post(
            reverse('accounts:register'),
            payload,
            REMOTE_ADDR='198.51.100.1',
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response.headers)
        self.assertContains(response, 'Забагато спроб реєстрації', status_code=429)

    def test_login_rate_limited_after_5_failed_attempts(self):
        payload = {'email': 'nobody@doublebite.ua', 'password': 'wrongpassword'}
        for _ in range(5):
            resp = self.client.post(
                reverse('accounts:login'),
                payload,
                REMOTE_ADDR='198.51.100.2',
            )
            self.assertEqual(resp.status_code, 200)

        with patch('accounts.forms.authenticate') as mock_authenticate:
            response = self.client.post(
                reverse('accounts:login'),
                payload,
                REMOTE_ADDR='198.51.100.2',
            )
        mock_authenticate.assert_not_called()
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response.headers)
        self.assertContains(response, 'Забагато невдалих спроб входу', status_code=429)

    def test_password_reset_rate_limited_after_5_attempts(self):
        payload = {'email': 'reset@doublebite.ua'}
        for _ in range(5):
            resp = self.client.post(
                reverse('accounts:password_reset'),
                payload,
                REMOTE_ADDR='198.51.100.3',
            )
            self.assertEqual(resp.status_code, 302)
        response = self.client.post(
            reverse('accounts:password_reset'),
            payload,
            REMOTE_ADDR='198.51.100.3',
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response.headers)
        self.assertContains(response, 'Забагато запитів на скидання пароля', status_code=429)

        for _ in range(5):
            resp = self.client.post(
                '/accounts/password/reset/',
                payload,
                REMOTE_ADDR='198.51.100.4',
            )
            self.assertEqual(resp.status_code, 302)
        alias_blocked = self.client.post(
            '/accounts/password/reset/',
            payload,
            REMOTE_ADDR='198.51.100.4',
        )
        self.assertEqual(alias_blocked.status_code, 429)
        self.assertIn('Retry-After', alias_blocked.headers)

    def test_xff_spoofing_cannot_bypass_rate_limit(self):
        payload = {'email': 'spoof@doublebite.ua', 'password': 'wrongpassword'}
        for idx in range(5):
            resp = self.client.post(
                reverse('accounts:login'),
                payload,
                REMOTE_ADDR='198.51.100.50',
                HTTP_X_FORWARDED_FOR=f'203.0.113.{idx + 1}',
            )
            self.assertEqual(resp.status_code, 200)

        with patch('accounts.forms.authenticate') as mock_authenticate:
            blocked = self.client.post(
                reverse('accounts:login'),
                payload,
                REMOTE_ADDR='198.51.100.50',
                HTTP_X_FORWARDED_FOR='203.0.113.250',
            )
        mock_authenticate.assert_not_called()
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('Retry-After', blocked.headers)

    def test_profile_post_validation_error_preserves_orders_in_context(self):
        user = User.objects.create_user(email='profileuser@doublebite.ua', password='password123')
        Order.objects.create(
            user=user,
            customer_name='Profile User',
            customer_phone='+380501112233',
            delivery_address='Kyiv',
            total_amount=150,
        )
        self.client.force_login(user)
        response = self.client.post(reverse('accounts:profile'), {'action': 'add_address'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('orders', response.context)
        self.assertEqual(len(response.context['orders']), 1)
