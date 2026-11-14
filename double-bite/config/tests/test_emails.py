from unittest.mock import patch

from django.core import mail
from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings

from config.emails import send_templated_email


class TemplatedEmailServiceTests(SimpleTestCase):
    def setUp(self):
        mail.outbox.clear()

    def test_send_templated_email_with_html_and_txt(self):
        count = send_templated_email(
            subject='Тестове повідомлення',
            template_prefix='emails/base_email',
            context={'content': 'Привіт, світ!'},
            recipient_list=['client@example.com'],
        )
        self.assertEqual(count, 1)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.subject, 'Тестове повідомлення')
        self.assertEqual(sent.to, ['client@example.com'])
        self.assertIn('Double Bite', sent.body)
        self.assertEqual(len(sent.alternatives), 1)
        self.assertEqual(sent.alternatives[0][1], 'text/html')
        self.assertIn('#F6F6F6', sent.alternatives[0][0])
        self.assertIn('Double Bite', sent.alternatives[0][0])

    def test_send_templated_email_single_string_recipient(self):
        count = send_templated_email(
            subject='Один отримувач',
            template_prefix='emails/base_email.html',
            context={'content': 'Тест'},
            recipient_list='single@example.com',
        )
        self.assertEqual(count, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['single@example.com'])

    def test_send_templated_email_custom_from_email(self):
        send_templated_email(
            subject='Кастомний відправник',
            template_prefix='emails/base_email',
            context={},
            recipient_list=['user@example.com'],
            from_email='custom@doublebite.ua',
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].from_email, 'custom@doublebite.ua')

    def test_send_templated_email_empty_recipient_list(self):
        count = send_templated_email(
            subject='Порожній список',
            template_prefix='emails/base_email',
            context={},
            recipient_list=[],
        )
        self.assertEqual(count, 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_templated_email_none_recipient_list(self):
        count = send_templated_email(
            subject='None список',
            template_prefix='emails/base_email',
            context={},
            recipient_list=None,
        )
        self.assertEqual(count, 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_templated_email_missing_templates(self):
        count = send_templated_email(
            subject='Неіснуючий шаблон',
            template_prefix='emails/non_existent_template_xyz',
            context={},
            recipient_list=['user@example.com'],
        )
        self.assertEqual(count, 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_templated_email_fail_silently_true_on_error(self):
        with patch('django.core.mail.message.EmailMultiAlternatives.send', side_effect=RuntimeError('SMTP error')):
            count = send_templated_email(
                subject='Помилка',
                template_prefix='emails/base_email',
                context={},
                recipient_list=['user@example.com'],
                fail_silently=True,
            )
            self.assertEqual(count, 0)

    def test_send_templated_email_fail_silently_false_raises(self):
        with (
            patch('django.core.mail.message.EmailMultiAlternatives.send', side_effect=RuntimeError('SMTP error')),
            self.assertRaises(RuntimeError),
        ):
            send_templated_email(
                subject='Помилка з виключенням',
                template_prefix='emails/base_email',
                context={},
                recipient_list=['user@example.com'],
                fail_silently=False,
            )

    def test_base_email_template_brand_elements(self):
        rendered = render_to_string('emails/base_email.html', {})
        self.assertIn('#F6F6F6', rendered)
        self.assertIn('#FFFFFF', rendered)
        self.assertIn('#262626', rendered)
        self.assertIn('JetBrains Mono', rendered)
        self.assertIn('Double Bite', rendered)

    @override_settings(DEFAULT_FROM_EMAIL='Double Bite Test <test@doublebite.ua>')
    def test_default_from_email_used_when_not_provided(self):
        send_templated_email(
            subject='Тест налаштувань',
            template_prefix='emails/base_email',
            context={},
            recipient_list=['client@example.com'],
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].from_email, 'Double Bite Test <test@doublebite.ua>')

    def test_plain_text_body_strips_style_and_script_blocks(self):
        from config.emails import _html_to_plain_text

        plain = _html_to_plain_text(
            '<style>.btn-primary { color: #fff; font-family: Inter; }</style>'
            '<script>window.alert("xss")</script>'
            '<p>Дякуємо &amp; смачного!</p>'
        )
        self.assertEqual(plain, 'Дякуємо & смачного!')

    def test_templated_email_plain_body_has_no_css_and_uses_unified_contacts(self):
        from django.conf import settings

        send_templated_email(
            subject='Перевірка тексту',
            template_prefix='emails/base_email',
            context={},
            recipient_list=['client@example.com'],
        )
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertNotIn('.email-container', email.body)
        self.assertNotIn('font-family:', email.body)
        self.assertNotIn('{', email.body)
        self.assertIn(settings.RESTAURANT_ADDRESS, email.body)
        self.assertIn(settings.RESTAURANT_HOTLINE, email.body)

    @override_settings(
        RESTAURANT_ADDRESS='вул. Сагайдачного, 15, Київ',
        RESTAURANT_HOTLINE='+380 44 999 00 11',
        SITE_DOMAIN='shop.doublebite.ua',
        SITE_PROTOCOL='http',
    )
    def test_txt_emails_and_urls_respect_unified_contacts_and_protocol(self):
        from types import SimpleNamespace

        from orders.emails import get_tracking_url

        send_templated_email(
            subject='Вітаємо',
            template_prefix='emails/welcome',
            context={
                'user': SimpleNamespace(email='guest@doublebite.ua'),
                'customer_name': 'Гість',
                'protocol': 'http',
                'domain': 'shop.doublebite.ua',
            },
            recipient_list=['guest@doublebite.ua'],
        )
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn('вул. Сагайдачного, 15, Київ', body)
        self.assertIn('+380 44 999 00 11', body)
        self.assertNotIn('вул. Володимирська, 42, Київ', body)
        self.assertNotIn('+380 44 123 45 67', body)

        dummy_order = SimpleNamespace(order_number='DB-777', access_token='tok-123')
        url = get_tracking_url(dummy_order)
        self.assertEqual(url, 'http://shop.doublebite.ua/orders/tracking/DB-777/?t=tok-123')

    @override_settings(
        SITE_DOMAIN='shop.doublebite.ua',
        SITE_PROTOCOL='https',
    )
    def test_order_and_support_emails_pass_resolved_domain_in_context(self):
        from types import SimpleNamespace

        from orders.emails import (
            send_order_confirmation_email,
            send_order_status_update_email,
            send_refund_notice_email,
            send_stripe_receipt_email,
        )
        from support.emails import send_ticket_escalated_email

        dummy_order = SimpleNamespace(
            order_number='DB-888',
            access_token='tok-888',
            customer_name='Тарас',
            customer_email='client@example.com',
            delivery_address='Київ',
            eta_minutes=30,
            total_amount='500.00',
            status='PREPARING',
            stripe_payment_intent_id='pi_test_1',
            user=None,
            items=SimpleNamespace(all=list),
            get_payment_method_display=lambda: 'Готівка',
            get_status_display=lambda: 'Готується',
        )
        dummy_ticket = SimpleNamespace(
            id=42,
            reason='ORDER_ISSUE',
            details='Потрібна допомога',
            customer_phone='+380501234567',
            order=None,
            session=None,
            get_reason_display=lambda: 'Проблема із замовленням',
        )

        with (
            patch('orders.emails.send_templated_email', return_value=1) as order_send_mock,
            patch('support.emails.send_templated_email', return_value=1) as support_send_mock,
        ):
            send_order_confirmation_email(dummy_order)
            send_order_status_update_email(dummy_order)
            send_stripe_receipt_email(dummy_order)
            send_refund_notice_email(dummy_order)
            send_ticket_escalated_email(dummy_ticket, recipient_email='admin@doublebite.ua')

        self.assertEqual(order_send_mock.call_count, 4)
        for call in order_send_mock.call_args_list:
            ctx = call.kwargs['context']
            self.assertEqual(ctx['domain'], 'shop.doublebite.ua')
            self.assertEqual(ctx['protocol'], 'https')
            self.assertTrue(ctx['tracking_url'].startswith('https://shop.doublebite.ua/'))

        support_send_mock.assert_called_once()
        support_ctx = support_send_mock.call_args.kwargs['context']
        self.assertEqual(support_ctx['domain'], 'shop.doublebite.ua')
        self.assertEqual(support_ctx['protocol'], 'https')
        self.assertTrue(support_ctx['admin_ticket_url'].startswith('https://shop.doublebite.ua/'))

    def test_resolve_site_domain_fallbacks(self):
        from config.emails import resolve_site_domain, resolve_site_protocol

        with override_settings(SITE_DOMAIN='primary.doublebite.ua', ALLOWED_HOSTS=['fallback.doublebite.ua']):
            self.assertEqual(resolve_site_domain('override.doublebite.ua'), 'override.doublebite.ua')
            self.assertEqual(resolve_site_domain(None), 'primary.doublebite.ua')
            self.assertEqual(resolve_site_domain('  '), 'primary.doublebite.ua')

        with override_settings(SITE_DOMAIN='', ALLOWED_HOSTS=['testserver', '*', '0.0.0.0', 'fallback.doublebite.ua']):
            self.assertEqual(resolve_site_domain(None), 'fallback.doublebite.ua')

        with override_settings(SITE_DOMAIN='', ALLOWED_HOSTS=['testserver', '*']):
            self.assertEqual(resolve_site_domain(None), 'localhost:8000')

        with override_settings(SITE_PROTOCOL='http'):
            self.assertEqual(resolve_site_protocol(None), 'http')
            self.assertEqual(resolve_site_protocol('https'), 'https')
