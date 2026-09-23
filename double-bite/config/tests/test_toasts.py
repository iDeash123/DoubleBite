from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.template.loader import get_template, render_to_string
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

User = get_user_model()


class ToastNotificationComponentTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.client = Client()

    def _get_request_with_messages(self, url='/'):
        request = self.factory.get(url)
        session_middleware = SessionMiddleware(lambda req: None)
        session_middleware.process_request(request)
        request.session.save()
        message_middleware = MessageMiddleware(lambda req: None)
        message_middleware.process_request(request)
        return request

    def test_base_template_does_not_contain_static_message_banners(self):
        content = render_to_string('base.html', {})
        self.assertNotIn('max-w-[1440px] w-full mx-auto px-4 sm:px-8 pt-4', content)
        self.assertIn('toast-notifications-container', content)

    def test_toast_component_included_in_base_html(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="toast-notifications-container"')
        self.assertContains(response, 'window.showToast')
        self.assertContains(response, '@show-toast.window="addToast($event.detail)"')
        self.assertContains(response, 'fixed bottom-6 right-6')
        self.assertContains(response, 'sm:bottom-8 sm:right-8')

    def test_render_toast_with_success_message(self):
        request = self._get_request_with_messages()
        messages.success(request, 'Операція пройшла успішно!')
        template = get_template('base.html')
        rendered = template.render(request=request)
        self.assertIn('Операція пройшла успішно!', rendered)
        self.assertIn("type: 'success'", rendered)
        self.assertIn('УСПІШНО', rendered)
        self.assertIn('border-l-emerald-600', rendered)
        self.assertIn('text-emerald-700 bg-emerald-50 border-emerald-200', rendered)

    def test_render_toast_with_error_message(self):
        request = self._get_request_with_messages()
        messages.error(request, 'Виникла помилка під час обробки.')
        template = get_template('base.html')
        rendered = template.render(request=request)
        self.assertIn('Виникла помилка під час обробки.', rendered)
        self.assertIn("type: 'error'", rendered)
        self.assertIn('ПОМИЛКА', rendered)
        self.assertIn('border-l-red-600', rendered)
        self.assertIn('text-red-700 bg-red-50 border-red-200', rendered)

    def test_render_toast_with_warning_message(self):
        request = self._get_request_with_messages()
        messages.warning(request, 'Увага: термін дії сесії спливає.')
        template = get_template('base.html')
        rendered = template.render(request=request)
        self.assertIn('Увага: термін дії сесії спливає.', rendered)
        self.assertIn("type: 'warning'", rendered)
        self.assertIn('УВАГА', rendered)
        self.assertIn('border-l-amber-500', rendered)
        self.assertIn('text-amber-800 bg-amber-50 border-amber-200', rendered)

    def test_render_toast_with_info_message(self):
        request = self._get_request_with_messages()
        messages.info(request, 'Інформаційне повідомлення.')
        template = get_template('base.html')
        rendered = template.render(request=request)
        self.assertIn('Інформаційне повідомлення.', rendered)
        self.assertIn("type: 'info'", rendered)
        self.assertIn('ІНФО', rendered)
        self.assertIn('border-l-fv-noir', rendered)
        self.assertIn('text-fv-noir bg-fv-offwhite border-fv-noir/20', rendered)

    def test_render_toast_multiple_messages(self):
        request = self._get_request_with_messages()
        messages.success(request, 'Перше повідомлення успіху')
        messages.error(request, 'Друге повідомлення помилки')
        template = get_template('base.html')
        rendered = template.render(request=request)
        self.assertIn('Перше повідомлення успіху', rendered)
        self.assertIn('Друге повідомлення помилки', rendered)
        self.assertIn('toast-srv-1', rendered)
        self.assertIn('toast-srv-2', rendered)

    def test_render_toast_special_characters_escaping(self):
        request = self._get_request_with_messages()
        messages.info(request, '<script>alert("xss")</script> & "лапки" \'одинарні\'')
        template = get_template('base.html')
        rendered = template.render(request=request)
        self.assertNotIn('<script>alert("xss")</script>', rendered)
        self.assertIn('\\u003Cscript\\u003Ealert(', rendered)

    def test_toast_component_brandbook_styling_tokens(self):
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'rounded-none')
        self.assertContains(response, 'font-mono')
        self.assertContains(response, 'shadow-xl')
        self.assertContains(response, 'border-fv-noir/20')
        self.assertContains(response, 'text-fv-noir')
        self.assertContains(response, 'pointer-events-none')
        self.assertContains(response, 'pointer-events-auto')
        self.assertContains(response, 'aria-live="polite"')

    def test_toast_component_hover_pause_and_resume(self):
        response = self.client.get(reverse('home'))
        self.assertContains(response, '@mouseenter="pause(toast)"')
        self.assertContains(response, '@mouseleave="resume(toast)"')
        self.assertContains(response, 'startTimer(toast)')

    def test_toast_component_close_button_and_timeout(self):
        response = self.client.get(reverse('home'))
        self.assertContains(response, '@click="removeToast(toast.id)"')
        self.assertContains(response, 'aria-label="Закрити"')
        self.assertContains(response, '4500')

    def test_toast_component_empty_when_no_messages(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('toasts: [', response.content.decode('utf-8'))
        self.assertNotIn('toast-srv-', response.content.decode('utf-8'))

    def test_toast_component_noscript_fallback(self):
        request = self._get_request_with_messages()
        messages.error(request, 'Помилка без JavaScript')
        template = get_template('base.html')
        rendered = template.render(request=request)
        self.assertIn('<noscript>', rendered)
        self.assertIn('Помилка без JavaScript', rendered)
        self.assertIn('ПОМИЛКА', rendered)

    def test_integration_login_message_rendered_in_toasts(self):
        User.objects.create_user(
            email='toastuser@doublebite.ua',
            password='TestPassword123!',
            first_name='Остап',
        )
        login_response = self.client.post(
            reverse('accounts:login'),
            {'email': 'toastuser@doublebite.ua', 'password': 'TestPassword123!'},
            follow=True,
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertContains(login_response, 'Ви успішно увійшли в систему.')
        self.assertContains(login_response, 'id="toast-notifications-container"')
        self.assertNotContains(login_response, 'max-w-[1440px] w-full mx-auto px-4 sm:px-8 pt-4')
