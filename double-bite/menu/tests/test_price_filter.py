from decimal import Decimal

from django.template import Context, Template
from django.test import SimpleTestCase
from django.utils.functional import SimpleLazyObject

from menu.templatetags.menu_extras import format_price, format_price_delta


class PriceFilterTests(SimpleTestCase):
    def test_format_price_whole_numbers(self):
        self.assertEqual(format_price(Decimal("485.00")), "485 ₴")
        self.assertEqual(format_price(Decimal("0.00")), "0 ₴")
        self.assertEqual(format_price(0), "0 ₴")
        self.assertEqual(format_price(485), "485 ₴")
        self.assertEqual(format_price(485.0), "485 ₴")
        self.assertEqual(format_price("485.00"), "485 ₴")
        self.assertEqual(format_price("485,00"), "485 ₴")
        self.assertEqual(format_price(Decimal("1250.00")), "1250 ₴")

    def test_format_price_fractional_numbers(self):
        self.assertEqual(format_price(Decimal("485.50")), "485,50 ₴")
        self.assertEqual(format_price(Decimal("485.5")), "485,50 ₴")
        self.assertEqual(format_price(485.1), "485,10 ₴")
        self.assertEqual(format_price("485.25"), "485,25 ₴")
        self.assertEqual(format_price("485,75"), "485,75 ₴")
        self.assertEqual(format_price(Decimal("19.995")), "20 ₴")
        self.assertEqual(format_price(Decimal("19.994")), "19,99 ₴")

    def test_format_price_negative_and_zero_normalization(self):
        self.assertEqual(format_price(Decimal("-25.00")), "-25 ₴")
        self.assertEqual(format_price(Decimal("-25.50")), "-25,50 ₴")
        self.assertEqual(format_price(Decimal("-0.001")), "0 ₴")
        self.assertEqual(format_price(Decimal("-0.00")), "0 ₴")

    def test_format_price_lazy_value(self):
        lazy_whole = SimpleLazyObject(lambda: Decimal("340.00"))
        lazy_frac = SimpleLazyObject(lambda: Decimal("340.50"))
        lazy_none = SimpleLazyObject(lambda: None)
        self.assertEqual(format_price(lazy_whole), "340 ₴")
        self.assertEqual(format_price(lazy_frac), "340,50 ₴")
        self.assertEqual(format_price(lazy_none), "")

    def test_format_price_invalid_and_none(self):
        self.assertEqual(format_price(None), "")
        self.assertEqual(format_price(""), "")
        self.assertEqual(format_price("   "), "")
        self.assertEqual(format_price("not-a-price"), "")
        self.assertEqual(format_price(Decimal("NaN")), "")
        self.assertEqual(format_price(Decimal("Infinity")), "")
        self.assertEqual(format_price(float("nan")), "")
        self.assertEqual(format_price(True), "")
        self.assertEqual(format_price(False), "")

    def test_format_price_uses_regular_space(self):
        formatted = format_price(Decimal("485.00"))
        self.assertIn(" ₴", formatted)
        self.assertNotIn("\u00a0", formatted)

    def test_format_price_delta_positive_negative_zero(self):
        self.assertEqual(format_price_delta(Decimal("60.00")), "+60 ₴")
        self.assertEqual(format_price_delta(60), "+60 ₴")
        self.assertEqual(format_price_delta(Decimal("25.50")), "+25,50 ₴")
        self.assertEqual(format_price_delta("25,5"), "+25,50 ₴")
        self.assertEqual(format_price_delta(Decimal("-25.00")), "-25 ₴")
        self.assertEqual(format_price_delta(Decimal("-25.50")), "-25,50 ₴")
        self.assertEqual(format_price_delta(0), "0 ₴")
        self.assertEqual(format_price_delta(Decimal("0.00")), "0 ₴")
        self.assertEqual(format_price_delta(Decimal("-0.001")), "0 ₴")
        self.assertEqual(format_price_delta(None), "")
        self.assertEqual(format_price_delta("invalid"), "")

    def test_template_rendering(self):
        tpl = Template(
            "{% load menu_extras %}"
            "{{ price|format_price }} | {{ pos|format_price_delta }} | "
            "{{ neg|format_price_delta }} | {{ zero|format_price_delta }}"
        )
        rendered = tpl.render(
            Context(
                {
                    "price": Decimal("485.00"),
                    "pos": Decimal("60.00"),
                    "neg": Decimal("-25.50"),
                    "zero": Decimal("0.00"),
                }
            )
        )
        self.assertEqual(rendered, "485 ₴ | +60 ₴ | -25,50 ₴ | 0 ₴")
