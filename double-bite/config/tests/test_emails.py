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
