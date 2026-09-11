from typing import Any

from django.contrib.auth import get_user_model

from .models import DeliveryAddress

User = get_user_model()


class UserService:
    @staticmethod
    def register_user(
        email: str,
        password: str,
        first_name: str = '',
        last_name: str = '',
        phone: str = '',
    ) -> User:
        return User.objects.create_user(
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
        )

    @staticmethod
    def add_address(user: User, **kwargs: Any) -> DeliveryAddress:
        return DeliveryAddress.objects.create(user=user, **kwargs)

    @staticmethod
    def get_user_addresses(user: User):
        return DeliveryAddress.objects.filter(user=user)
