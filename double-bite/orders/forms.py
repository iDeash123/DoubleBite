from config.validators import normalize_email, normalize_phone, phone_validator
from django import forms

from .models import PaymentMethod

_INPUT_CLASS = (
    'w-full px-4 py-3 bg-fv-offwhite border border-fv-noir/20 '
    'focus:outline-none focus:border-fv-noir text-xs font-mono '
    'text-fv-noir placeholder:text-fv-slate'
)


class OrderCheckoutForm(forms.Form):
    customer_name = forms.CharField(
        label="Ваше ім'я",
        max_length=100,
        widget=forms.TextInput(attrs={
            'placeholder': 'Олександр Коваленко',
            'class': _INPUT_CLASS,
        }),
    )
    customer_phone = forms.CharField(
        label='Контактний телефон',
        max_length=20,
        widget=forms.TextInput(attrs={
            'placeholder': '+380 50 123 45 67',
            'class': _INPUT_CLASS,
        }),
    )
    customer_email = forms.EmailField(
        label='Email (для чеку та трекінгу)',
        required=False,
        max_length=254,
        widget=forms.EmailInput(attrs={
            'placeholder': 'client@example.com',
            'class': _INPUT_CLASS,
        }),
    )
    delivery_address = forms.CharField(
        label='Адреса доставки (Київ)',
        max_length=500,
        widget=forms.Textarea(attrs={
            'rows': 3,
            'placeholder': "вул. Хрещатик, 24, під'їзд 2, поверх 4, кв. 18 (код: 42K)",
            'class': _INPUT_CLASS,
        }),
    )
    payment_method = forms.ChoiceField(
        label='Спосіб оплати',
        choices=PaymentMethod.choices,
        initial=PaymentMethod.ONLINE,
        widget=forms.RadioSelect(attrs={
            'class': 'accent-fv-noir',
        }),
    )
    notes = forms.CharField(
        label='Коментар до замовлення',
        required=False,
        max_length=500,
        widget=forms.TextInput(attrs={
            'placeholder': 'Залишити біля дверей / не дзвонити в домофон',
            'class': _INPUT_CLASS,
        }),
    )

    def clean_customer_phone(self) -> str:
        cleaned_phone = normalize_phone(self.cleaned_data.get('customer_phone'))
        phone_validator(cleaned_phone)
        return cleaned_phone

    def clean_customer_email(self) -> str:
        return normalize_email(self.cleaned_data.get('customer_email'))
