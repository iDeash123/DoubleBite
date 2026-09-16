import os
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest
from accounts.models import DeliveryAddress, Role, User
from django.conf import settings
from django.core.management import call_command
from orders.models import Order, OrderItem
from support.models import FAQKnowledge

from menu.models import Category, Dish, DishOption


@pytest.mark.django_db
def test_seed_db_creates_expected_data_and_local_media_images():
    call_command('seed_db', skip_images=True)

    assert Category.objects.count() == 7
    assert Dish.objects.count() >= 105
    assert DishOption.objects.count() >= 100
    assert FAQKnowledge.objects.count() >= 5
    assert DeliveryAddress.objects.count() >= 3
    assert Order.objects.count() >= 2
    assert OrderItem.objects.count() >= 2

    admin_user = User.objects.filter(role=Role.RESTAURANT_ADMIN).first()
    assert admin_user is not None
    assert admin_user.is_superuser is True

    customer_user = User.objects.filter(role=Role.CUSTOMER).first()
    assert customer_user is not None

    dishes = list(Dish.objects.all())
    for dish in dishes:
        assert dish.image is not None
        assert dish.image.name.startswith('dishes/')
        assert not dish.image.name.startswith('http')
        assert os.path.exists(dish.image.path)
        assert os.path.getsize(dish.image.path) > 0


@pytest.mark.django_db
def test_seed_db_downloads_missing_image(tmp_path, monkeypatch):
    mock_media = tmp_path / 'media'
    mock_dishes = mock_media / 'dishes'
    mock_dishes.mkdir(parents=True)
    monkeypatch.setattr(settings, 'MEDIA_ROOT', mock_media)

    fake_bytes = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' \",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9'

    mock_resp = MagicMock()
    mock_resp.read.return_value = fake_bytes
    mock_resp.__enter__.return_value = mock_resp

    with patch('urllib.request.urlopen', return_value=mock_resp):
        call_command('seed_db')

    dishes = list(Dish.objects.all())
    assert len(dishes) >= 105
    for dish in dishes:
        assert dish.image.name.startswith('dishes/')
        assert os.path.exists(dish.image.path)
        assert os.path.getsize(dish.image.path) > 0


@pytest.mark.django_db
def test_seed_db_fallback_when_download_fails(tmp_path, monkeypatch):
    mock_media = tmp_path / 'media'
    mock_dishes = mock_media / 'dishes'
    mock_dishes.mkdir(parents=True)
    monkeypatch.setattr(settings, 'MEDIA_ROOT', mock_media)

    with patch('urllib.request.urlopen', side_effect=URLError('Network error')):
        call_command('seed_db')

    dishes = list(Dish.objects.all())
    assert len(dishes) >= 105
    for dish in dishes:
        assert dish.image.name.startswith('dishes/')
        assert os.path.exists(dish.image.path)
        assert os.path.getsize(dish.image.path) > 0
