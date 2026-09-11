from typing import Any

from django import forms
from django.contrib.auth import authenticate, get_user_model

from .models import DeliveryAddress

User = get_user_model()


class UserRegistrationForm(forms.ModelForm):
    password = forms.CharField(
        label='Пароль',
        widget=forms.PasswordInput(
            attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
        ),
    )
    password_confirm = forms.CharField(
        label='Підтвердження пароля',
        widget=forms.PasswordInput(
            attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
        ),
    )

    class Meta:
        model = User
        fields = ('email', 'first_name', 'last_name', 'phone')
        widgets = {
            'email': forms.EmailInput(
                attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
            ),
            'first_name': forms.TextInput(
                attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
            ),
            'last_name': forms.TextInput(
                attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
            ),
            'phone': forms.TextInput(
                attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
            ),
        }

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean()
        password = cleaned_data.get('password')
        password_confirm = cleaned_data.get('password_confirm')
        if password and password_confirm and password != password_confirm:
            self.add_error('password_confirm', 'Паролі не співпадають.')
        return cleaned_data

    def save(self, commit: bool = True) -> User:
        user = super().save(commit=False)
        user.set_password(self.cleaned_data['password'])
        if commit:
            user.save()
        return user


class UserLoginForm(forms.Form):
    email = forms.EmailField(
        label='Електронна пошта',
        widget=forms.EmailInput(
            attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
        ),
    )
    password = forms.CharField(
        label='Пароль',
        widget=forms.PasswordInput(
            attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
        ),
    )

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean()
        email = cleaned_data.get('email')
        password = cleaned_data.get('password')
        if email and password:
            self.user_cache = authenticate(email=email, password=password)
            if self.user_cache is None:
                raise forms.ValidationError('Невірний email або пароль.')
            if not self.user_cache.is_active:
                raise forms.ValidationError('Обліковий запис деактивовано.')
        return cleaned_data

    def get_user(self) -> User | None:
        return getattr(self, 'user_cache', None)


class UserProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'phone')
        widgets = {
            'first_name': forms.TextInput(
                attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
            ),
            'last_name': forms.TextInput(
                attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
            ),
            'phone': forms.TextInput(
                attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
            ),
        }


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
                attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors', 'placeholder': 'Дім, Робота...'}
            ),
            'city': forms.TextInput(
                attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
            ),
            'street': forms.TextInput(
                attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
            ),
            'building': forms.TextInput(
                attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
            ),
            'apartment': forms.TextInput(
                attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
            ),
            'floor': forms.TextInput(
                attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
            ),
            'intercom': forms.TextInput(
                attrs={'class': 'w-full px-4 py-2.5 border border-fv-noir/20 bg-white text-xs font-mono text-fv-noir focus:border-fv-noir outline-none rounded-none transition-colors'}
            ),
            'is_default': forms.CheckboxInput(
                attrs={'class': 'h-4 w-4 text-fv-noir border-fv-noir/30 rounded-none focus:ring-0'}
            ),
        }
