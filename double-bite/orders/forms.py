from django import forms

from .models import PaymentMethod


class OrderCheckoutForm(forms.Form):
    customer_name = forms.CharField(
        label="Ваше ім'я",
        max_length=100,
        widget=forms.TextInput(attrs={
            'placeholder': 'Олександр Коваленко',
            'class': 'w-full px-4 py-3 bg-fv-offwhite border border-fv-noir/20 focus:outline-none focus:border-fv-noir text-xs font-mono text-fv-noir placeholder:text-fv-slate',
        }),
    )
    customer_phone = forms.CharField(
        label='Контактний телефон',
        max_length=20,
        widget=forms.TextInput(attrs={
            'placeholder': '+380 50 123 45 67',
            'class': 'w-full px-4 py-3 bg-fv-offwhite border border-fv-noir/20 focus:outline-none focus:border-fv-noir text-xs font-mono text-fv-noir placeholder:text-fv-slate',
        }),
    )
    delivery_address = forms.CharField(
        label='Адреса доставки (Київ)',
        widget=forms.Textarea(attrs={
            'rows': 3,
            'placeholder': 'вул. Хрещатик, 24, під\'їзд 2, поверх 4, кв. 18 (код: 42K)',
            'class': 'w-full px-4 py-3 bg-fv-offwhite border border-fv-noir/20 focus:outline-none focus:border-fv-noir text-xs font-mono text-fv-noir placeholder:text-fv-slate',
        }),
    )
    payment_method = forms.ChoiceField(
        label='Спосіб оплати',
        choices=PaymentMethod.choices,
        initial=PaymentMethod.CARD,
        widget=forms.RadioSelect(attrs={
            'class': 'accent-fv-noir',
        }),
    )
    notes = forms.CharField(
        label='Коментар до замовлення',
        required=False,
        widget=forms.TextInput(attrs={
            'placeholder': 'Залишити біля дверей / не дзвонити в домофон',
            'class': 'w-full px-4 py-3 bg-fv-offwhite border border-fv-noir/20 focus:outline-none focus:border-fv-noir text-xs font-mono text-fv-noir placeholder:text-fv-slate',
        }),
    )
