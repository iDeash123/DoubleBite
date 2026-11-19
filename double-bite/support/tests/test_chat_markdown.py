import json
import re
import shutil
import subprocess
from pathlib import Path

from django.test import SimpleTestCase


class ChatMarkdownFormatterXssTest(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        template_path = (
            Path(__file__).resolve().parent.parent.parent
            / 'templates'
            / 'support'
            / 'chat_widget.html'
        )
        content = template_path.read_text(encoding='utf-8')
        match = re.search(
            r'window\.__formatMarkdown\s*=\s*(function\(text\)\s*\{[\s\S]*?\n\};)',
            content,
        )
        if not match:
            raise ValueError('Could not find window.__formatMarkdown in chat_widget.html')
        cls.js_function = match.group(1)
        cls.node_bin = shutil.which('node')

    def _format_markdown(self, text: str) -> str:
        if not self.node_bin:
            self.fail('Node.js executable is required to verify chat markdown XSS.')
        js_code = f"""
        const fn = {self.js_function};
        const input = {json.dumps(text)};
        process.stdout.write(fn(input));
        """
        result = subprocess.run(
            [self.node_bin, '-e', js_code],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
        )
        return result.stdout

    def test_script_tag_escaped(self):
        output = self._format_markdown('<script>alert("xss")</script>')
        self.assertNotIn('<script>', output)
        self.assertIn('&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;', output)

    def test_img_onerror_escaped(self):
        output = self._format_markdown('<img src=x onerror=alert(1)>')
        self.assertNotIn('<img', output)
        self.assertIn('&lt;img', output)

    def test_quotes_escaped(self):
        output = self._format_markdown('Hello "world" and \'test\'')
        self.assertIn('&quot;world&quot;', output)
        self.assertIn('&#39;test&#39;', output)

    def test_javascript_protocol_link_rejected(self):
        output = self._format_markdown('[click](javascript:alert(document.cookie))')
        self.assertNotIn('<a href="javascript:', output)
        self.assertNotIn('<a', output)
        self.assertIn('click (javascript:alert(document.cookie))', output)

    def test_data_protocol_link_rejected(self):
        output = self._format_markdown(
            '[click](data:text/html,<script>alert(1)</script>)'
        )
        self.assertNotIn('<a href="data:', output)
        self.assertNotIn('<a', output)

    def test_protocol_relative_and_backslash_urls_rejected(self):
        for bad_url in (
            '//evil.tld',
            '//evil.tld/phish',
            '///evil.tld/phish',
            r'/\evil.tld',
            '   //evil.tld/spaced   ',
        ):
            output = self._format_markdown(f'[phish]({bad_url})')
            self.assertNotIn('<a', output, f'Protocol-relative URL allowed: {bad_url}')
            self.assertIn('phish (', output)

    def test_attribute_injection_in_link_neutralized(self):
        payload = '[link](https://example.com" onmouseover="alert(1))'
        output = self._format_markdown(payload)
        self.assertNotIn('<a', output)
        self.assertIn('&quot;', output)

    def test_xss_in_link_label_neutralized(self):
        valid_link = self._format_markdown(
            '[<img src=x onerror=alert(1)>](https://doublebite.ua/menu/)'
        )
        self.assertIn('href="https://doublebite.ua/menu/"', valid_link)
        self.assertNotIn('<img', valid_link)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', valid_link)

        invalid_link = self._format_markdown(
            '[<script>alert(1)</script>](javascript:alert(1))'
        )
        self.assertNotIn('<a', invalid_link)
        self.assertNotIn('<script>', invalid_link)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', invalid_link)

    def test_valid_https_link_with_ampersand_query_preserved(self):
        output = self._format_markdown(
            '[Меню](https://doublebite.ua/menu?category=pizza&max_price=300)'
        )
        expected = (
            '<a href="https://doublebite.ua/menu?category=pizza&amp;max_price=300" '
            'target="_blank" rel="noopener noreferrer" class="underline">Меню</a>'
        )
        self.assertIn(expected, output)

    def test_valid_relative_link_with_ampersand_query_preserved(self):
        output = self._format_markdown(
            '[Замовлення](/orders/tracking/DB-0001/?query=1&sort=2)'
        )
        expected = (
            '<a href="/orders/tracking/DB-0001/?query=1&amp;sort=2" '
            'target="_blank" rel="noopener noreferrer" class="underline">Замовлення</a>'
        )
        self.assertIn(expected, output)

    def test_sentinel_text_in_input_does_not_collide_with_links(self):
        output = self._format_markdown('@@DBLINK_0@@ [Меню](/menu/)')
        self.assertIn('@@DBLINK_0@@', output)
        self.assertIn('href="/menu/"', output)

    def test_markdown_basic_formatting(self):
        output = self._format_markdown('**жирний** та *курсив*')
        self.assertIn('<strong>жирний</strong>', output)
        self.assertIn('<em>курсив</em>', output)
