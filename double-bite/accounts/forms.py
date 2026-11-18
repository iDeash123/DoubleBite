from typing import Any

from allauth.account.models import EmailAddress
from config import emails as config_emails
from config.validators import normalize_email, normalize_phone, phone_validator
from django import forms
from django.apps import apps
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import format_html

from .models import DeliveryAddress

User = get_user_model()

INPUT_CSS_CLASS = (
    'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs '
    'font-mono text-fv-noir focus:border-fv-noir outline-none '
    'rounded-none transition-colors'
)
CHECKBOX_CSS_CLASS = (
    'h-4 w-4 text-fv-noir border-fv-noir/30 rounded-none focus:ring-0'
)


def _normalize_and_validate_phone(raw_phone: str | None) -> str:
    phone = normalize_phone(raw_phone)
    if phone:
        phone_validator(phone)
    return phone


class UserRegistrationForm(forms.ModelForm):
    password = forms.CharField(
        label='Пароль',
        widget=forms.PasswordInput(attrs={'class': INPUT_CSS_CLASS}),
    )
    password_confirm = forms.CharField(
        label='Підтвердження пароля',
        widget=forms.PasswordInput(attrs={'class': INPUT_CSS_CLASS}),
    )

    class Meta:
        model = User
        fields = ('email', 'first_name', 'last_name', 'phone')
        widgets = {
            'email': forms.EmailInput(attrs={'class': INPUT_CSS_CLASS}),
            'first_name': forms.TextInput(attrs={'class': INPUT_CSS_CLASS}),
            'last_name': forms.TextInput(attrs={'class': INPUT_CSS_CLASS}),
            'phone': forms.TextInput(attrs={'class': INPUT_CSS_CLASS}),
        }

    def clean_email(self) -> str:
        email = normalize_email(self.cleaned_data.get('email'))
        if email and User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('Користувач з таким email вже існує.')
        return email

    def clean_phone(self) -> str:
        return _normalize_and_validate_phone(self.cleaned_data.get('phone'))

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean()
        password = cleaned_data.get('password')
        password_confirm = cleaned_data.get('password_confirm')
        if password and password_confirm and password != password_confirm:
            self.add_error('password_confirm', 'Паролі не співпадають.')
        if password:
            for field in ('email', 'first_name', 'last_name', 'phone'):
                if field in cleaned_data:
                    setattr(self.instance, field, cleaned_data[field])
            try:
                validate_password(password, self.instance)
            except forms.ValidationError as error:
                self.add_error('password', error)
        return cleaned_data

    def save(self, commit: bool = True) -> User:
        user = super().save(commit=False)
        user.email = normalize_email(user.email)
        user.set_password(self.cleaned_data['password'])
        if commit:
            user.save()
        return user


class UserLoginForm(forms.Form):
    email = forms.EmailField(
        label='Електронна пошта',
        widget=forms.EmailInput(attrs={'class': INPUT_CSS_CLASS}),
    )
    password = forms.CharField(
        label='Пароль',
        widget=forms.PasswordInput(attrs={'class': INPUT_CSS_CLASS}),
    )

    def clean_email(self) -> str:
        return normalize_email(self.cleaned_data.get('email'))

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean()
        email = normalize_email(cleaned_data.get('email'))
        password = cleaned_data.get('password')
        if email and password:
            user = authenticate(email=email, password=password)
            if user is None:
                existing_user = User.objects.filter(email__iexact=email).first()
                if existing_user and existing_user.email != email:
                    user = authenticate(
                        email=existing_user.email,
                        password=password,
                    )
            if user is None:
                raise forms.ValidationError('Невірний email або пароль.')
            if not user.is_active:
                raise forms.ValidationError('Обліковий запис деактивовано.')
            if (
                getattr(settings, 'ACCOUNT_EMAIL_VERIFICATION', 'optional')
                == 'mandatory'
                and not user.is_superuser
                and not EmailAddress.objects.filter(
                    user=user,
                    verified=True,
                ).exists()
            ):
                email_manage_url = reverse('account_email')
                raise forms.ValidationError(
                    format_html(
                        'Email не підтверджено. Перевірте пошту або '
                        '<a href="{}" class="underline font-bold">'
                        'надішліть лист підтвердження повторно</a>.',
                        email_manage_url,
                    )
                )
            self.user_cache = user
        return cleaned_data

    def get_user(self) -> User | None:
        return getattr(self, 'user_cache', None)


class UserProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'phone')
        widgets = {
            'first_name': forms.TextInput(attrs={'class': INPUT_CSS_CLASS}),
            'last_name': forms.TextInput(attrs={'class': INPUT_CSS_CLASS}),
            'phone': forms.TextInput(attrs={'class': INPUT_CSS_CLASS}),
        }

    def clean_phone(self) -> str:
        return _normalize_and_validate_phone(self.cleaned_data.get('phone'))


class DeliveryAddressForm(forms.ModelForm):
    class Meta:
        model = DeliveryAddress
        fields = (
            'title',
            'city',
            'street',
            'building',
            'apartment',
            'floor',
            'intercom',
            'is_default',
        )
        widgets = {
            'title': forms.TextInput(
                attrs={'class': INPUT_CSS_CLASS, 'placeholder': 'Дім, Робота...'}
            ),
            'city': forms.TextInput(attrs={'class': INPUT_CSS_CLASS}),
            'street': forms.TextInput(attrs={'class': INPUT_CSS_CLASS}),
            'building': forms.TextInput(attrs={'class': INPUT_CSS_CLASS}),
            'apartment': forms.TextInput(attrs={'class': INPUT_CSS_CLASS}),
            'floor': forms.TextInput(attrs={'class': INPUT_CSS_CLASS}),
            'intercom': forms.TextInput(attrs={'class': INPUT_CSS_CLASS}),
            'is_default': forms.CheckboxInput(
                attrs={'class': CHECKBOX_CSS_CLASS}
            ),
        }


class CustomPasswordResetForm(PasswordResetForm):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields['email'].widget.attrs.update({
            'class': INPUT_CSS_CLASS,
            'placeholder': 'name@example.com',
        })

    def clean_email(self) -> str:
        return normalize_email(self.cleaned_data.get('email'))

    def save(
        self,
        domain_override: str | None = None,
        subject_template_name: str = 'registration/password_reset_subject.txt',
        email_template_name: str = 'registration/password_reset_email.html',
        use_https: bool = False,
        token_generator: Any = default_token_generator,
        from_email: str | None = None,
        request: Any = None,
        html_email_template_name: str | None = None,
        extra_email_context: dict[str, Any] | None = None,
    ) -> None:
        if (
            request is None
            and not domain_override
            and not apps.is_installed('django.contrib.sites')
        ):
            domain_override = config_emails.resolve_site_domain()
            use_https = use_https or (
                config_emails.resolve_site_protocol() == 'https'
            )
        super().save(
            domain_override=domain_override,
            subject_template_name=subject_template_name,
            email_template_name=email_template_name,
            use_https=use_https,
            token_generator=token_generator,
            from_email=from_email,
            request=request,
            html_email_template_name=html_email_template_name,
            extra_email_context=extra_email_context,
        )

    def send_mail(
        self,
        subject_template_name: str,
        email_template_name: str,
        context: dict[str, Any],
        from_email: str | None,
        to_email: str,
        html_email_template_name: str | None = None,
    ) -> None:
        subject = render_to_string(subject_template_name, context)
        subject = ''.join(subject.splitlines())
        config_emails.send_templated_email(
            subject=subject,
            template_prefix=email_template_name,
            context=context,
            recipient_list=[to_email],
            from_email=from_email,
            fail_silently=False,
            html_template=html_email_template_name,
            txt_template=email_template_name,
            include_html=html_email_template_name is not None,
        )


class CustomSetPasswordForm(SetPasswordForm):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({'class': INPUT_CSS_CLASS})
