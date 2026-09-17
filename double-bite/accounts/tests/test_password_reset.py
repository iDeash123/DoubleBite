from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

User = get_user_model()


class PasswordResetTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            email='resetuser@example.com',
            password='OldPassword123!',
            first_name='Олександр',
            last_name='Петренко',
            phone='+380501112233',
        )
        mail.outbox.clear()

    def test_password_reset_form_view_renders_200(self):
        response = self.client.get(reverse('accounts:password_reset'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/password_reset.html')
        self.assertContains(response, 'Відновлення пароля')

    def test_password_reset_post_valid_email_sends_email_and_redirects(self):
        response = self.client.post(
            reverse('accounts:password_reset'),
            {'email': self.user.email},
        )
        self.assertRedirects(response, reverse('accounts:password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)

        email = mail.outbox[0]
        self.assertEqual(email.subject, 'Double Bite: Відновлення пароля')
        self.assertEqual(email.to, [self.user.email])
        self.assertIn('Double Bite', email.body)
        self.assertIn('/accounts/reset/', email.body)

        self.assertEqual(len(email.alternatives), 1)
        self.assertEqual(email.alternatives[0][1], 'text/html')
        html = email.alternatives[0][0]
        self.assertIn('#F6F6F6', html)
        self.assertIn('#262626', html)
        self.assertIn('JetBrains Mono', html)
        self.assertIn('Double Bite', html)

    def test_password_reset_post_nonexistent_email_does_not_send_email(self):
        response = self.client.post(
            reverse('accounts:password_reset'),
            {'email': 'nobody@example.com'},
        )
        self.assertRedirects(response, reverse('accounts:password_reset_done'))
        self.assertEqual(len(mail.outbox), 0)

    def test_password_reset_done_view_renders_200(self):
        response = self.client.get(reverse('accounts:password_reset_done'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/password_reset_done.html')
        self.assertContains(response, 'Лист надіслано')

    def test_password_reset_confirm_view_valid_token(self):
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        url = reverse('accounts:password_reset_confirm', kwargs={'uidb64': uidb64, 'token': token})

        response = self.client.get(url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/password_reset_confirm.html')
        self.assertTrue(response.context['validlink'])
        self.assertContains(response, 'Введіть новий пароль')

    def test_password_reset_confirm_view_invalid_token(self):
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        url = reverse('accounts:password_reset_confirm', kwargs={'uidb64': uidb64, 'token': 'invalid-token-123'})

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/password_reset_confirm.html')
        self.assertFalse(response.context['validlink'])
        self.assertContains(response, 'Посилання недійсне')

    def test_password_reset_confirm_post_changes_password(self):
        uidb64 = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        url = reverse('accounts:password_reset_confirm', kwargs={'uidb64': uidb64, 'token': token})

        get_response = self.client.get(url, follow=True)
        self.assertEqual(get_response.status_code, 200)

        response = self.client.post(
            get_response.request['PATH_INFO'],
            {
                'new_password1': 'NewSecretPass2026!',
                'new_password2': 'NewSecretPass2026!',
            },
        )
        self.assertRedirects(response, reverse('accounts:password_reset_complete'))

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('NewSecretPass2026!'))

    def test_password_reset_complete_view_renders_200(self):
        response = self.client.get(reverse('accounts:password_reset_complete'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/password_reset_complete.html')
        self.assertContains(response, 'Пароль оновлено')

    def test_registration_sends_welcome_email(self):
        mail.outbox.clear()
        register_url = reverse('accounts:register')
        response = self.client.post(
            register_url,
            {
                'email': 'newregistered@example.com',
                'first_name': 'Іван',
                'last_name': 'Франко',
                'phone': '+380509998877',
                'password': 'StrongPass12345!',
                'password_confirm': 'StrongPass12345!',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)

        welcome_email = mail.outbox[0]
        self.assertEqual(welcome_email.subject, 'Ласкаво просимо до Double Bite!')
        self.assertEqual(welcome_email.to, ['newregistered@example.com'])
        self.assertIn('Double Bite', welcome_email.body)
        self.assertEqual(len(welcome_email.alternatives), 1)
        self.assertEqual(welcome_email.alternatives[0][1], 'text/html')
        self.assertIn('#F6F6F6', welcome_email.alternatives[0][0])
        self.assertIn('#262626', welcome_email.alternatives[0][0])
