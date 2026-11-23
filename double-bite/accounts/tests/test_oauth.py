from unittest.mock import patch

from allauth.account.models import EmailAddress
from allauth.core.context import request_context
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin
from allauth.socialaccount.providers.base.constants import AuthProcess
from allauth.socialaccount.providers.github.provider import GitHubProvider
from allauth.socialaccount.providers.google.provider import GoogleProvider
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from menu.models import Category, Dish
from orders.models import Cart, CartItem
from orders.services import CartService

from accounts.models import Role

User = get_user_model()


class OAuthConfigurationTest(TestCase):
    def test_allauth_installed_apps(self):
        expected_apps = [
            'allauth',
            'allauth.account',
            'allauth.socialaccount',
            'allauth.socialaccount.providers.google',
            'allauth.socialaccount.providers.github',
        ]
        for app in expected_apps:
            self.assertIn(app, settings.INSTALLED_APPS)

    def test_authentication_backends(self):
        self.assertIn(
            'allauth.account.auth_backends.AuthenticationBackend',
            settings.AUTHENTICATION_BACKENDS,
        )
        self.assertIn(
            'django.contrib.auth.backends.ModelBackend',
            settings.AUTHENTICATION_BACKENDS,
        )

    def test_account_middleware_installed(self):
        self.assertIn(
            'allauth.account.middleware.AccountMiddleware',
            settings.MIDDLEWARE,
        )

    def test_allauth_custom_user_settings(self):
        for deprecated_setting in (
            'ACCOUNT_EMAIL_REQUIRED',
            'ACCOUNT_USERNAME_REQUIRED',
            'ACCOUNT_AUTHENTICATION_METHOD',
        ):
            self.assertFalse(
                hasattr(settings, deprecated_setting),
                f'Deprecated allauth setting {deprecated_setting} should be absent',
            )

        self.assertIsNone(settings.ACCOUNT_USER_MODEL_USERNAME_FIELD)
        self.assertEqual(settings.ACCOUNT_LOGIN_METHODS, {'email'})
        self.assertEqual(settings.ACCOUNT_SIGNUP_FIELDS, ['email*'])
        self.assertEqual(settings.ACCOUNT_EMAIL_VERIFICATION, 'mandatory')
        self.assertEqual(settings.SOCIALACCOUNT_EMAIL_VERIFICATION, 'mandatory')
        self.assertTrue(settings.SOCIALACCOUNT_AUTO_SIGNUP)
        self.assertFalse(settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION)
        self.assertFalse(settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT)
        self.assertEqual(
            settings.SOCIALACCOUNT_ADAPTER,
            'accounts.adapter.AccountSecuritySocialAdapter',
        )
        self.assertEqual(settings.LOGIN_REDIRECT_URL, 'accounts:profile')

    def test_socialaccount_providers_configured(self):
        self.assertIn('google', settings.SOCIALACCOUNT_PROVIDERS)
        self.assertIn('github', settings.SOCIALACCOUNT_PROVIDERS)
        self.assertEqual(
            settings.SOCIALACCOUNT_PROVIDERS['google']['SCOPE'],
            ['profile', 'email'],
        )
        self.assertEqual(
            settings.SOCIALACCOUNT_PROVIDERS['github']['SCOPE'],
            ['user:email'],
        )

    def test_oauth_urls_resolve(self):
        self.assertEqual(reverse('google_login'), '/accounts/google/login/')
        self.assertEqual(reverse('github_login'), '/accounts/github/login/')

    def test_google_login_post_redirects_to_oauth(self):
        client = Client()
        response = client.post(reverse('google_login'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('accounts.google.com', response.get('Location', ''))

    def test_github_login_post_redirects_to_oauth(self):
        client = Client()
        response = client.post(reverse('github_login'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('github.com', response.get('Location', ''))

    def test_allauth_models_exist_in_db(self):
        self.assertEqual(SocialAccount.objects.count(), 0)
        self.assertEqual(SocialApp.objects.count(), 0)
        self.assertEqual(EmailAddress.objects.count(), 0)


class OAuthTemplateUITest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()

    def tearDown(self):
        cache.clear()

    def test_login_page_renders_social_buttons_and_separator(self):
        response = self.client.get(reverse('accounts:login'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Або увійти через', content)
        self.assertIn(reverse('google_login'), content)
        self.assertIn(reverse('github_login'), content)
        self.assertIn('Google', content)
        self.assertIn('GitHub', content)
        self.assertIn('border-db-linen', content)
        self.assertIn('bg-db-noir', content)
        self.assertIn('text-db-muted', content)

    def test_register_page_renders_social_buttons_and_separator(self):
        response = self.client.get(reverse('accounts:register'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Або увійти через', content)
        self.assertIn(reverse('google_login'), content)
        self.assertIn(reverse('github_login'), content)
        self.assertIn('Google', content)
        self.assertIn('GitHub', content)
        self.assertIn('border-db-linen', content)
        self.assertIn('bg-db-noir', content)
        self.assertIn('text-db-muted', content)

    def test_login_and_register_propagate_safe_next_parameter(self):
        next_path = '/orders/checkout/'
        encoded_next = '%2Forders%2Fcheckout%2F'

        login_resp = self.client.get(f"{reverse('accounts:login')}?next={next_path}")
        self.assertEqual(login_resp.status_code, 200)
        login_html = login_resp.content.decode('utf-8')
        self.assertIn(f'<input type="hidden" name="next" value="{next_path}">', login_html)
        self.assertIn(f'action="{reverse("google_login")}?next={encoded_next}"', login_html)
        self.assertIn(f'action="{reverse("github_login")}?next={encoded_next}"', login_html)
        self.assertIn(f'href="{reverse("accounts:register")}?next={next_path}"', login_html)

        register_resp = self.client.get(f"{reverse('accounts:register')}?next={next_path}")
        self.assertEqual(register_resp.status_code, 200)
        register_html = register_resp.content.decode('utf-8')
        self.assertIn(f'<input type="hidden" name="next" value="{next_path}">', register_html)
        self.assertIn(f'action="{reverse("google_login")}?next={encoded_next}"', register_html)
        self.assertIn(f'action="{reverse("github_login")}?next={encoded_next}"', register_html)
        self.assertIn(f'href="{reverse("accounts:login")}?next={next_path}"', register_html)

    def test_login_and_register_strip_unsafe_next_parameter(self):
        for route_name, switch_name in (
            ('accounts:login', 'accounts:register'),
            ('accounts:register', 'accounts:login'),
        ):
            response = self.client.get(f'{reverse(route_name)}?next=https://evil.com/phishing')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context['next'], '')
            html = response.content.decode('utf-8')
            body_html = html.split('<body', 1)[1]
            self.assertNotIn('evil.com', body_html)
            self.assertIn('<input type="hidden" name="next" value="">', body_html)
            self.assertIn(f'action="{reverse("google_login")}"', body_html)
            self.assertIn(f'action="{reverse("github_login")}"', body_html)
            self.assertIn(f'href="{reverse(switch_name)}"', body_html)

    def test_allauth_styled_templates_render(self):
        user = User.objects.create_user(
            email='styled.user@doublebite.ua',
            password='StyledPassword123!',
        )
        self.client.force_login(user)

        for url_name in (
            'socialaccount_connections',
            'account_email',
            'account_change_password',
        ):
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, 200, f'{url_name} failed to render')

        user.set_unusable_password()
        user.save(update_fields=['password'])
        self.client.force_login(user)
        set_pw_resp = self.client.get(reverse('account_set_password'))
        self.assertEqual(set_pw_resp.status_code, 200)

        self.client.logout()
        sent_resp = self.client.get(reverse('account_email_verification_sent'))
        self.assertEqual(sent_resp.status_code, 200)


class OAuthCallbackLoginTest(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        mail.outbox.clear()

    def tearDown(self):
        cache.clear()

    def _create_request(self, path: str = '/', user=None):
        request = self.factory.get(path)
        request.user = user if user is not None else AnonymousUser()
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)
        return request

    def _build_sociallogin(
        self,
        request,
        provider_id: str,
        uid: str,
        email: str,
        *,
        verified: bool = True,
        first_name: str = '',
        last_name: str = '',
        process: str = AuthProcess.LOGIN,
    ) -> SocialLogin:
        adapter = get_adapter(request)
        provider = adapter.get_provider(request, provider_id)
        account = SocialAccount(
            provider=provider.id,
            uid=uid,
            extra_data={'email': email},
        )
        user = User(
            email=email,
            first_name=first_name,
            last_name=last_name,
        )
        sociallogin = SocialLogin(user=user, account=account)
        sociallogin.provider = provider
        sociallogin.state['process'] = process
        sociallogin.email_addresses = [
            EmailAddress(
                email=email,
                verified=verified,
                primary=True,
            )
        ]
        return sociallogin

    def test_google_oauth_new_user_signup_and_welcome_email(self):
        request = self._create_request()
        sociallogin = self._build_sociallogin(
            request,
            GoogleProvider.id,
            'google-uid-1001',
            'google.user@doublebite.ua',
            first_name='Тарас',
            last_name='Шевченко',
        )

        with self.captureOnCommitCallbacks(execute=True), request_context(request):
            response = complete_social_login(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/profile/', response.url)

        created_user = User.objects.get(email='google.user@doublebite.ua')
        self.assertEqual(created_user.first_name, 'Тарас')
        self.assertEqual(created_user.last_name, 'Шевченко')
        self.assertEqual(created_user.role, Role.CUSTOMER)

        social_acc = SocialAccount.objects.get(uid='google-uid-1001')
        self.assertEqual(social_acc.user, created_user)
        self.assertEqual(social_acc.provider, 'google')

        email_addr = EmailAddress.objects.get(email='google.user@doublebite.ua')
        self.assertTrue(email_addr.verified)
        self.assertEqual(email_addr.user, created_user)

        self.assertEqual(len(mail.outbox), 1)
        welcome_email = mail.outbox[0]
        self.assertEqual(welcome_email.subject, 'Ласкаво просимо до Double Bite!')
        self.assertEqual(welcome_email.to, ['google.user@doublebite.ua'])
        self.assertIn('border-radius: 8px', welcome_email.alternatives[0][0])

    def test_google_oauth_normalizes_mixed_case_email_on_signup(self):
        request = self._create_request()
        adapter = get_adapter(request)
        provider = adapter.get_provider(request, GoogleProvider.id)
        google_data = {
            'id': 'google-uid-mixed-case',
            'email': 'Mixed.Google@DoubleBite.UA',
            'verified_email': True,
            'given_name': 'Олена',
            'family_name': 'Теліга',
        }
        sociallogin = provider.sociallogin_from_response(request, google_data)
        with self.captureOnCommitCallbacks(execute=True), request_context(request):
            response = complete_social_login(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        created_user = User.objects.get(email='mixed.google@doublebite.ua')
        self.assertEqual(created_user.email, 'mixed.google@doublebite.ua')

        fresh_login = SocialLogin(user=User())
        populated = adapter.populate_user(
            request,
            fresh_login,
            {'email': 'Mixed.Manual@DoubleBite.UA'},
        )
        self.assertEqual(populated.email, 'mixed.manual@doublebite.ua')

    def test_github_oauth_new_user_signup(self):
        request = self._create_request()
        sociallogin = self._build_sociallogin(
            request,
            GitHubProvider.id,
            'github-uid-3003',
            'developer@doublebite.ua',
            first_name='Леся',
            last_name='Українка',
        )

        with self.captureOnCommitCallbacks(execute=True), request_context(request):
            response = complete_social_login(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        created_user = User.objects.get(email='developer@doublebite.ua')
        self.assertEqual(created_user.role, Role.CUSTOMER)

        social_acc = SocialAccount.objects.get(uid='github-uid-3003')
        self.assertEqual(social_acc.user, created_user)
        self.assertEqual(social_acc.provider, 'github')

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, 'Ласкаво просимо до Double Bite!')

    def test_oauth_rejects_auto_connect_and_preserves_password_for_existing_local_users(self):
        for provider_id, email_addr, uid in (
            (GoogleProvider.id, 'existing.google@doublebite.ua', 'google-uid-2002'),
            (GitHubProvider.id, 'existing.github@doublebite.ua', 'github-uid-4004'),
        ):
            existing_user = User.objects.create_user(
                email=email_addr,
                password='ExistingPassword123!',
                first_name='Оксана',
                last_name='Петренко',
            )
            request = self._create_request()
            sociallogin = self._build_sociallogin(
                request,
                provider_id,
                uid,
                email_addr.upper(),
            )

            with request_context(request):
                response = complete_social_login(request, sociallogin)

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, reverse('socialaccount_signup'))
            self.assertIsNone(request.session.get('_auth_user_id'))
            self.assertFalse(SocialAccount.objects.filter(uid=uid).exists())
            existing_user.refresh_from_db()
            self.assertTrue(existing_user.has_usable_password())
            self.assertTrue(existing_user.check_password('ExistingPassword123!'))

    def test_oauth_rejects_auto_connect_and_preserves_password_when_email_address_deleted(self):
        local_user = User.objects.create_user(
            email='no.emailrow@doublebite.ua',
            password='LocalPassword123!',
        )
        superuser = User.objects.create_superuser(
            email='admin.no.emailrow@doublebite.ua',
            password='AdminPassword123!',
        )
        EmailAddress.objects.filter(user__in=[local_user, superuser]).delete()

        for target_user, provider_id, uid in (
            (local_user, GoogleProvider.id, 'google-wipe-attempt-local'),
            (superuser, GitHubProvider.id, 'github-wipe-attempt-admin'),
        ):
            request = self._create_request()
            sociallogin = self._build_sociallogin(
                request,
                provider_id,
                uid,
                target_user.email,
            )

            with request_context(request):
                response = complete_social_login(request, sociallogin)

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, reverse('socialaccount_signup'))
            self.assertIsNone(request.session.get('_auth_user_id'))
            self.assertFalse(SocialAccount.objects.filter(uid=uid).exists())

            target_user.refresh_from_db()
            self.assertTrue(target_user.has_usable_password())

        self.assertTrue(local_user.check_password('LocalPassword123!'))
        self.assertTrue(superuser.check_password('AdminPassword123!'))

    def test_social_login_rejects_auto_connect_for_unverified_local_user(self):
        existing_user = User.objects.create_user(
            email='unverified.victim@doublebite.ua',
            password='VictimPassword123!',
            _skip_email_address=True,
        )
        EmailAddress.objects.create(
            user=existing_user,
            email=existing_user.email,
            verified=False,
            primary=True,
        )

        request = self._create_request()
        sociallogin = self._build_sociallogin(
            request,
            GoogleProvider.id,
            'google-hijack-attempt',
            'unverified.victim@doublebite.ua',
        )

        with request_context(request):
            response = complete_social_login(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('socialaccount_signup'))
        self.assertNotEqual(request.session.get('_auth_user_id'), str(existing_user.pk))
        self.assertFalse(
            SocialAccount.objects.filter(uid='google-hijack-attempt', user=existing_user).exists()
        )
        existing_user.refresh_from_db()
        self.assertTrue(existing_user.has_usable_password())
        self.assertTrue(existing_user.check_password('VictimPassword123!'))

    def test_explicit_oauth_connect_links_account_and_keeps_password_intact(self):
        existing_user = User.objects.create_user(
            email='explicit.connect@doublebite.ua',
            password='ConnectPassword123!',
        )
        request = self._create_request(user=existing_user)
        sociallogin = self._build_sociallogin(
            request,
            GoogleProvider.id,
            'google-explicit-connect-1',
            'explicit.connect@doublebite.ua',
            process=AuthProcess.CONNECT,
        )

        with request_context(request):
            response = complete_social_login(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('socialaccount_connections'))
        self.assertTrue(
            SocialAccount.objects.filter(
                user=existing_user,
                provider='google',
                uid='google-explicit-connect-1',
            ).exists()
        )
        existing_user.refresh_from_db()
        self.assertTrue(existing_user.has_usable_password())
        self.assertTrue(existing_user.check_password('ConnectPassword123!'))

    def test_already_linked_social_account_login_succeeds_and_keeps_password(self):
        existing_user = User.objects.create_user(
            email='linked.login@doublebite.ua',
            password='LinkedPassword123!',
        )
        SocialAccount.objects.create(
            user=existing_user,
            provider=GoogleProvider.id,
            uid='google-already-linked-1',
            extra_data={'email': existing_user.email},
        )
        request = self._create_request()
        sociallogin = self._build_sociallogin(
            request,
            GoogleProvider.id,
            'google-already-linked-1',
            existing_user.email,
        )

        with request_context(request):
            response = complete_social_login(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('accounts:profile'))
        self.assertEqual(request.session.get('_auth_user_id'), str(existing_user.pk))
        existing_user.refresh_from_db()
        self.assertTrue(existing_user.has_usable_password())
        self.assertTrue(existing_user.check_password('LinkedPassword123!'))

    def test_google_oauth_from_response_creates_user(self):
        request = self._create_request()
        adapter = get_adapter(request)
        provider = adapter.get_provider(request, GoogleProvider.id)
        google_data = {
            'id': 'google-uid-5005',
            'email': 'real.google.user@doublebite.ua',
            'verified_email': True,
            'name': 'Іван Франко',
            'given_name': 'Іван',
            'family_name': 'Франко',
        }
        sociallogin = provider.sociallogin_from_response(request, google_data)
        with self.captureOnCommitCallbacks(execute=True), request_context(request):
            response = complete_social_login(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/profile/', response.url)
        created_user = User.objects.get(email='real.google.user@doublebite.ua')
        self.assertEqual(created_user.first_name, 'Іван')
        self.assertEqual(created_user.last_name, 'Франко')
        self.assertEqual(created_user.role, Role.CUSTOMER)
        social_acc = SocialAccount.objects.get(uid='google-uid-5005')
        self.assertEqual(social_acc.user, created_user)
        self.assertEqual(len(mail.outbox), 1)

    def test_github_oauth_from_response_creates_user(self):
        request = self._create_request()
        adapter = get_adapter(request)
        provider = adapter.get_provider(request, GitHubProvider.id)
        github_data = {
            'id': 6006,
            'login': 'frankodev',
            'name': 'Іван Франко',
            'email': 'real.github.user@doublebite.ua',
            'emails': [
                {
                    'email': 'real.github.user@doublebite.ua',
                    'primary': True,
                    'verified': True,
                }
            ],
        }
        sociallogin = provider.sociallogin_from_response(request, github_data)
        with self.captureOnCommitCallbacks(execute=True), request_context(request):
            response = complete_social_login(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/profile/', response.url)
        created_user = User.objects.get(email='real.github.user@doublebite.ua')
        self.assertEqual(created_user.role, Role.CUSTOMER)
        social_acc = SocialAccount.objects.get(uid='6006')
        self.assertEqual(social_acc.user, created_user)
        self.assertEqual(len(mail.outbox), 1)

    def test_oauth_login_with_next_parameter_redirects_to_next_url(self):
        request = self._create_request('/?next=/orders/checkout/')
        adapter = get_adapter(request)
        provider = adapter.get_provider(request, GoogleProvider.id)
        google_data = {
            'id': 'google-uid-7007',
            'email': 'checkout.redirect@doublebite.ua',
            'verified_email': True,
            'name': 'Redirect User',
        }
        sociallogin = provider.sociallogin_from_response(request, google_data)
        with request_context(request):
            response = complete_social_login(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/orders/checkout/')

    def test_oauth_provider_access_denied_returns_401(self):
        client = Client()
        google_resp = client.get('/accounts/google/login/callback/?error=access_denied')
        self.assertEqual(google_resp.status_code, 401)
        github_resp = client.get('/accounts/github/login/callback/?error=access_denied')
        self.assertEqual(github_resp.status_code, 401)

    def test_google_login_post_forwards_next_to_provider_redirect(self):
        client = Client()
        with patch.object(
            GoogleProvider,
            'get_login_url',
            wraps=GoogleProvider.get_login_url,
        ):
            response = client.post(
                reverse('google_login'),
                {'next': '/orders/checkout/'},
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn('accounts.google.com', response.get('Location', ''))


class OAuthCartMergeTest(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.category = Category.objects.create(name='Піца', slug='pizza-oauth')
        self.dish1 = Dish.objects.create(
            category=self.category,
            title='Маргарита',
            slug='margherita-oauth',
            price=220,
            weight_grams=450,
            calories=750,
        )
        self.dish2 = Dish.objects.create(
            category=self.category,
            title='Пепероні',
            slug='pepperoni-oauth',
            price=280,
            weight_grams=480,
            calories=920,
        )

    def tearDown(self):
        cache.clear()

    def _create_request_with_guest_cart(self):
        request = self.factory.get('/')
        request.user = AnonymousUser()
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)

        guest_cart = CartService.get_or_create_cart(request)
        CartItem.objects.create(cart=guest_cart, dish=self.dish1, quantity=2)
        return request, guest_cart

    def test_social_login_merges_guest_cart_to_new_user(self):
        request, guest_cart = self._create_request_with_guest_cart()
        guest_cart_id = guest_cart.id

        adapter = get_adapter(request)
        provider = adapter.get_provider(request, GoogleProvider.id)
        account = SocialAccount(
            provider=provider.id,
            uid='google-cart-merge-1',
            extra_data={'email': 'new.cart.user@doublebite.ua'},
        )
        user = User(email='new.cart.user@doublebite.ua')
        sociallogin = SocialLogin(user=user, account=account)
        sociallogin.provider = provider
        sociallogin.email_addresses = [
            EmailAddress(
                email='new.cart.user@doublebite.ua',
                verified=True,
                primary=True,
            )
        ]

        with self.captureOnCommitCallbacks(execute=True), request_context(request):
            complete_social_login(request, sociallogin)

        created_user = User.objects.get(email='new.cart.user@doublebite.ua')
        user_cart = Cart.objects.get(user=created_user)
        self.assertEqual(user_cart.items.count(), 1)
        self.assertEqual(user_cart.items.first().dish, self.dish1)
        self.assertEqual(user_cart.items.first().quantity, 2)
        self.assertFalse(Cart.objects.filter(id=guest_cart_id).exists())

    def test_social_login_merges_guest_cart_to_linked_user_with_existing_items(self):
        existing_user = User.objects.create_user(
            email='existing.cart.user@doublebite.ua',
            password='CartPassword123!',
        )
        SocialAccount.objects.create(
            user=existing_user,
            provider=GitHubProvider.id,
            uid='github-cart-merge-2',
            extra_data={'email': existing_user.email},
        )
        user_cart = Cart.objects.create(user=existing_user)
        CartItem.objects.create(cart=user_cart, dish=self.dish1, quantity=1)
        CartItem.objects.create(cart=user_cart, dish=self.dish2, quantity=1)

        request, guest_cart = self._create_request_with_guest_cart()
        guest_cart_id = guest_cart.id

        adapter = get_adapter(request)
        provider = adapter.get_provider(request, GitHubProvider.id)
        account = SocialAccount(
            provider=provider.id,
            uid='github-cart-merge-2',
            extra_data={'email': 'existing.cart.user@doublebite.ua'},
        )
        sociallogin = SocialLogin(
            user=User(email='existing.cart.user@doublebite.ua'),
            account=account,
        )
        sociallogin.provider = provider
        sociallogin.email_addresses = [
            EmailAddress(
                email='existing.cart.user@doublebite.ua',
                verified=True,
                primary=True,
            )
        ]

        with request_context(request):
            complete_social_login(request, sociallogin)

        user_cart.refresh_from_db()
        self.assertEqual(user_cart.items.count(), 2)

        margherita_item = user_cart.items.get(dish=self.dish1)
        self.assertEqual(margherita_item.quantity, 3)

        pepperoni_item = user_cart.items.get(dish=self.dish2)
        self.assertEqual(pepperoni_item.quantity, 1)

        self.assertFalse(Cart.objects.filter(id=guest_cart_id).exists())
        existing_user.refresh_from_db()
        self.assertTrue(existing_user.check_password('CartPassword123!'))

    def test_cart_service_merge_guest_cart_to_user_alias(self):
        request, guest_cart = self._create_request_with_guest_cart()
        guest_cart_id = guest_cart.id

        user = User.objects.create_user(
            email='alias.user@doublebite.ua',
            password='AliasPassword123!',
        )

        CartService.merge_guest_cart_to_user(request, user)

        user_cart = Cart.objects.get(user=user)
        self.assertEqual(user_cart.items.count(), 1)
        self.assertEqual(user_cart.items.first().quantity, 2)
        self.assertFalse(Cart.objects.filter(id=guest_cart_id).exists())

    def test_social_login_merges_guest_cart_caps_at_99(self):
        existing_user = User.objects.create_user(
            email='capped.cart.user@doublebite.ua',
            password='CappedPassword123!',
        )
        SocialAccount.objects.create(
            user=existing_user,
            provider=GoogleProvider.id,
            uid='google-cart-merge-cap',
            extra_data={'email': existing_user.email},
        )
        user_cart = Cart.objects.create(user=existing_user)
        CartItem.objects.create(cart=user_cart, dish=self.dish1, quantity=70)

        request, guest_cart = self._create_request_with_guest_cart()
        guest_cart.items.filter(dish=self.dish1).update(quantity=40)

        adapter = get_adapter(request)
        provider = adapter.get_provider(request, GoogleProvider.id)
        account = SocialAccount(
            provider=provider.id,
            uid='google-cart-merge-cap',
            extra_data={'email': 'capped.cart.user@doublebite.ua'},
        )
        sociallogin = SocialLogin(
            user=User(email='capped.cart.user@doublebite.ua'),
            account=account,
        )
        sociallogin.provider = provider
        sociallogin.email_addresses = [
            EmailAddress(
                email='capped.cart.user@doublebite.ua',
                verified=True,
                primary=True,
            )
        ]

        with request_context(request):
            complete_social_login(request, sociallogin)

        user_cart.refresh_from_db()
        self.assertEqual(user_cart.items.get(dish=self.dish1).quantity, 99)
        existing_user.refresh_from_db()
        self.assertTrue(existing_user.check_password('CappedPassword123!'))

    def test_social_login_with_empty_guest_cart_succeeds(self):
        request = self.factory.get('/')
        request.user = AnonymousUser()
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)
        empty_guest_cart = CartService.get_or_create_cart(request)
        empty_cart_id = empty_guest_cart.id

        user = User.objects.create_user(
            email='empty.guest.cart@doublebite.ua',
            password='EmptyPassword123!',
        )
        CartService.merge_guest_cart_to_user(request, user)

        self.assertFalse(Cart.objects.filter(id=empty_cart_id).exists())
