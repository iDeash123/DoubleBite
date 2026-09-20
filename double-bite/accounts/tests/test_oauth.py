from allauth.account.models import EmailAddress
from allauth.core.context import request_context
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialLogin
from allauth.socialaccount.providers.github.provider import GitHubProvider
from allauth.socialaccount.providers.google.provider import GoogleProvider
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
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
        self.assertIsNone(settings.ACCOUNT_USER_MODEL_USERNAME_FIELD)
        self.assertTrue(settings.ACCOUNT_EMAIL_REQUIRED)
        self.assertFalse(settings.ACCOUNT_USERNAME_REQUIRED)
        self.assertEqual(settings.ACCOUNT_AUTHENTICATION_METHOD, 'email')
        self.assertTrue(settings.SOCIALACCOUNT_AUTO_SIGNUP)
        self.assertTrue(settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION)
        self.assertTrue(settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT)

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
        self.client = Client()

    def test_login_page_renders_social_buttons_and_separator(self):
        response = self.client.get(reverse('accounts:login'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('АБО УВІЙТИ ЧЕРЕЗ', content)
        self.assertIn(reverse('google_login'), content)
        self.assertIn(reverse('github_login'), content)
        self.assertIn('Google', content)
        self.assertIn('GitHub', content)
        self.assertIn('border-[#E5E5E5]', content)
        self.assertIn('bg-fv-noir', content)
        self.assertIn('text-fv-slate', content)

    def test_register_page_renders_social_buttons_and_separator(self):
        response = self.client.get(reverse('accounts:register'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('АБО УВІЙТИ ЧЕРЕЗ', content)
        self.assertIn(reverse('google_login'), content)
        self.assertIn(reverse('github_login'), content)
        self.assertIn('Google', content)
        self.assertIn('GitHub', content)
        self.assertIn('border-[#E5E5E5]', content)
        self.assertIn('bg-fv-noir', content)
        self.assertIn('text-fv-slate', content)


class OAuthCallbackLoginTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _create_request(self):
        request = self.factory.get('/')
        request.user = AnonymousUser()
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)
        return request

    def test_google_oauth_new_user_signup(self):
        request = self._create_request()
        adapter = get_adapter(request)
        provider = adapter.get_provider(request, GoogleProvider.id)

        account = SocialAccount(
            provider=provider.id,
            uid='google-uid-1001',
            extra_data={
                'email': 'google.user@doublebite.ua',
                'given_name': 'Тарас',
                'family_name': 'Шевченко',
            },
        )
        user = User(
            email='google.user@doublebite.ua',
            first_name='Тарас',
            last_name='Шевченко',
        )
        sociallogin = SocialLogin(user=user, account=account)
        sociallogin.provider = provider
        sociallogin.email_addresses = [
            EmailAddress(
                email='google.user@doublebite.ua',
                verified=True,
                primary=True,
            )
        ]

        with request_context(request):
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

    def test_google_oauth_existing_user_auto_connect(self):
        existing_user = User.objects.create_user(
            email='existing.client@doublebite.ua',
            password='ExistingPassword123!',
            first_name='Оксана',
            last_name='Петренко',
        )

        request = self._create_request()
        adapter = get_adapter(request)
        provider = adapter.get_provider(request, GoogleProvider.id)

        account = SocialAccount(
            provider=provider.id,
            uid='google-uid-2002',
            extra_data={'email': 'existing.client@doublebite.ua'},
        )
        sociallogin = SocialLogin(
            user=User(email='existing.client@doublebite.ua'),
            account=account,
        )
        sociallogin.provider = provider
        sociallogin.email_addresses = [
            EmailAddress(
                email='existing.client@doublebite.ua',
                verified=True,
                primary=True,
            )
        ]

        with request_context(request):
            response = complete_social_login(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/profile/', response.url)

        self.assertEqual(
            User.objects.filter(email='existing.client@doublebite.ua').count(),
            1,
        )
        self.assertEqual(request.session.get('_auth_user_id'), str(existing_user.pk))

        social_acc = SocialAccount.objects.get(uid='google-uid-2002')
        self.assertEqual(social_acc.user, existing_user)

    def test_github_oauth_new_user_signup(self):
        request = self._create_request()
        adapter = get_adapter(request)
        provider = adapter.get_provider(request, GitHubProvider.id)

        account = SocialAccount(
            provider=provider.id,
            uid='github-uid-3003',
            extra_data={
                'email': 'developer@doublebite.ua',
                'name': 'Леся Українка',
            },
        )
        user = User(
            email='developer@doublebite.ua',
            first_name='Леся',
            last_name='Українка',
        )
        sociallogin = SocialLogin(user=user, account=account)
        sociallogin.provider = provider
        sociallogin.email_addresses = [
            EmailAddress(
                email='developer@doublebite.ua',
                verified=True,
                primary=True,
            )
        ]

        with request_context(request):
            response = complete_social_login(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        created_user = User.objects.get(email='developer@doublebite.ua')
        self.assertEqual(created_user.role, Role.CUSTOMER)

        social_acc = SocialAccount.objects.get(uid='github-uid-3003')
        self.assertEqual(social_acc.user, created_user)
        self.assertEqual(social_acc.provider, 'github')

    def test_github_oauth_existing_user_auto_connect(self):
        existing_user = User.objects.create_user(
            email='dev.existing@doublebite.ua',
            password='DevPassword123!',
        )

        request = self._create_request()
        adapter = get_adapter(request)
        provider = adapter.get_provider(request, GitHubProvider.id)

        account = SocialAccount(
            provider=provider.id,
            uid='github-uid-4004',
            extra_data={'email': 'dev.existing@doublebite.ua'},
        )
        sociallogin = SocialLogin(
            user=User(email='dev.existing@doublebite.ua'),
            account=account,
        )
        sociallogin.provider = provider
        sociallogin.email_addresses = [
            EmailAddress(
                email='dev.existing@doublebite.ua',
                verified=True,
                primary=True,
            )
        ]

        with request_context(request):
            response = complete_social_login(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(request.session.get('_auth_user_id'), str(existing_user.pk))

        social_acc = SocialAccount.objects.get(uid='github-uid-4004')
        self.assertEqual(social_acc.user, existing_user)

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
        with request_context(request):
            response = complete_social_login(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        created_user = User.objects.get(email='real.google.user@doublebite.ua')
        self.assertEqual(created_user.first_name, 'Іван')
        self.assertEqual(created_user.last_name, 'Франко')
        self.assertEqual(created_user.role, Role.CUSTOMER)
        social_acc = SocialAccount.objects.get(uid='google-uid-5005')
        self.assertEqual(social_acc.user, created_user)

    def test_github_oauth_from_response_creates_user(self):
        request = self._create_request()
        adapter = get_adapter(request)
        provider = adapter.get_provider(request, GitHubProvider.id)
        github_data = {
            'id': 6006,
            'login': 'frankodev',
            'name': 'Іван Франко',
            'email': 'real.github.user@doublebite.ua',
        }
        sociallogin = provider.sociallogin_from_response(request, github_data)
        with request_context(request):
            response = complete_social_login(request, sociallogin)

        self.assertEqual(response.status_code, 302)
        created_user = User.objects.get(email='real.github.user@doublebite.ua')
        self.assertEqual(created_user.role, Role.CUSTOMER)
        social_acc = SocialAccount.objects.get(uid='6006')
        self.assertEqual(social_acc.user, created_user)

    def test_oauth_login_with_next_parameter_redirects_to_next_url(self):
        request = self.factory.get('/?next=/orders/checkout/')
        request.user = AnonymousUser()
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)

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



class OAuthCartMergeTest(TestCase):
    def setUp(self):
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

        with request_context(request):
            complete_social_login(request, sociallogin)

        created_user = User.objects.get(email='new.cart.user@doublebite.ua')
        user_cart = Cart.objects.get(user=created_user)
        self.assertEqual(user_cart.items.count(), 1)
        self.assertEqual(user_cart.items.first().dish, self.dish1)
        self.assertEqual(user_cart.items.first().quantity, 2)
        self.assertFalse(Cart.objects.filter(id=guest_cart_id).exists())

    def test_social_login_merges_guest_cart_to_existing_user_with_existing_items(self):
        existing_user = User.objects.create_user(
            email='existing.cart.user@doublebite.ua',
            password='CartPassword123!',
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

