from typing import Any

from allauth.account.models import EmailAddress
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin
from config.validators import normalize_email
from django.contrib.auth import get_user_model
from django.http import HttpRequest


class AccountSecuritySocialAdapter(DefaultSocialAccountAdapter):
    """Social account adapter enforcing no automatic account merging by email."""

    def can_authenticate_by_email(self, login: SocialLogin, email: str) -> bool:
        return False

    def is_auto_signup_allowed(
        self,
        request: HttpRequest,
        sociallogin: SocialLogin,
    ) -> bool:
        candidate_emails: set[str] = set()
        user_email = normalize_email(getattr(sociallogin.user, 'email', None))
        if user_email:
            candidate_emails.add(user_email)
        for address in getattr(sociallogin, 'email_addresses', []) or []:
            normalized = normalize_email(getattr(address, 'email', None))
            if normalized:
                candidate_emails.add(normalized)

        user_model = get_user_model()
        for email in candidate_emails:
            if (
                user_model.objects.filter(email__iexact=email).exists()
                or EmailAddress.objects.filter(email__iexact=email).exists()
            ):
                return False

        return super().is_auto_signup_allowed(request, sociallogin)

    def populate_user(
        self,
        request: HttpRequest,
        sociallogin: SocialLogin,
        data: dict[str, Any],
    ) -> Any:
        user = super().populate_user(request, sociallogin, data)
        if getattr(user, 'email', None):
            user.email = normalize_email(user.email)
        return user
