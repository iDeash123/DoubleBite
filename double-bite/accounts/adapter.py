from allauth.account.models import EmailAddress
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin


class AccountSecuritySocialAdapter(DefaultSocialAccountAdapter):
    def can_authenticate_by_email(self, login: SocialLogin, email: str) -> bool:
        if not super().can_authenticate_by_email(login, email):
            return False
        return not EmailAddress.objects.filter(email__iexact=email, verified=False).exists()
