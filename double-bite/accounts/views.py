
import json
from typing import Any

from config.emails import send_templated_email
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import (
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.core.exceptions import SynchronousOnlyOperation
from django.db import DatabaseError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import TemplateView

from .forms import (
    CustomPasswordResetForm,
    CustomSetPasswordForm,
    DeliveryAddressForm,
    UserLoginForm,
    UserProfileForm,
    UserRegistrationForm,
)
from .models import DeliveryAddress

DEFAULT_HERO_DISHES = [
    {
        'id': 1,
        'title': 'Truffle Wagyu Burger',
        'subtitle': 'Бріош · Мармурова яловичина · Трюфельний айолі',
        'tag': '01 · HAUTE BURGER',
        'badge': 'ФЛАГМАНСЬКА СТРАВА',
        'description': 'Фірмовий бургер з витриманою мармуровою яловичиною сухого визрівання, соусом з чорного трюфеля, карамелізованою цибулею та вершковим чедером у свіжоспеченій булочці бріош.',
        'taste_notes': 'Насичений умамі, земляні ноти чорного трюфеля, соковита мармурова текстура',
        'price': '485',
        'weight_grams': 380,
        'calories': 680,
        'prep_time': '15 хв',
        'image': 'https://images.unsplash.com/photo-1568901346375-23c9450c58cd?auto=format&fit=crop&w=1000&q=85',
        'bg_watermark': 'HAUTE BURGER',
    },
    {
        'id': 2,
        'title': 'Quattro Formaggi al Tartufo',
        'subtitle': 'Горгонзола · Fior di Latte · Пармезан 24 міс. · Трюфельний мед',
        'tag': '02 · NEAPOLITAN PIZZA',
        'badge': '48H FERMENTATION',
        'description': 'Біла неаполітанська піца на повітряному тісті 48-годинної холодної ферментації з чотирма витриманими сирами та акацієвим медом з білим трюфелем.',
        'taste_notes': 'Вершково-пікантний контраст, медово-трюфельний посмак, хрусткий бортик',
        'price': '420',
        'weight_grams': 460,
        'calories': 820,
        'prep_time': '12 хв',
        'image': 'https://images.unsplash.com/photo-1513104890138-7c749659a591?auto=format&fit=crop&w=1000&q=85',
        'bg_watermark': 'NEAPOLITAN PIZZA',
    },
    {
        'id': 3,
        'title': 'Wild Salmon & Avocado Bowl',
        'subtitle': 'Норвезький лосось · Авокадо Hass · Кіноа · Едамаме',
        'tag': '03 · FRESH BOWL',
        'badge': 'HEALTHY FLOW',
        'description': 'Збалансований боул з охолодженим норвезьким лососем, стиглим кремовим авокадо Hass, органічною кіноа, бобами едамаме та цитрусово-кунжутною заправкою.',
        'taste_notes': 'Свіжий океанічний смак, оксамитова ніжність авокадо, хрусткі нутрієнти',
        'price': '390',
        'weight_grams': 340,
        'calories': 420,
        'prep_time': '10 хв',
        'image': 'https://images.unsplash.com/photo-1546069901-ba9599a7e63c?auto=format&fit=crop&w=1000&q=85',
        'bg_watermark': 'FRESH BOWL',
    },
    {
        'id': 4,
        'title': 'Basque Burnt Cheesecake',
        'subtitle': 'Карамелізована скоринка · Ваніль Bourbon · Тануча серцевина',
        'tag': '04 · CRAFT PASTRY',
        'badge': 'CHEF SIGNATURE',
        'description': 'Легендарний сан-себастьянський чізкейк з випаленою до гірко-солодкого карамельного відтінку поверхнею та оксамитовою рідкою текстурою в середині.',
        'taste_notes': 'Карамелізована скоринка, тануча вершкова ніжність, бурбонська мадагаскарська ваніль',
        'price': '220',
        'weight_grams': 190,
        'calories': 340,
        'prep_time': '5 хв',
        'image': 'https://images.unsplash.com/photo-1533134242443-d4fd215305ad?auto=format&fit=crop&w=1000&q=85',
        'bg_watermark': 'BURNT CHEESECAKE',
    },
]


class HomeView(TemplateView):
    template_name = 'home/index.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        hero_dishes = [dict(d) for d in DEFAULT_HERO_DISHES]
        featured_dishes = []
        categories = []
        try:
            from menu.models import Category, Dish
            db_dishes = list(Dish.objects.filter(is_available=True).select_related('category'))
            if db_dishes:
                featured_dishes = db_dishes[:8]
                for hero in hero_dishes:
                    matched = None
                    for dish in db_dishes:
                        if hero['title'].lower() in dish.title.lower() or dish.title.lower() in hero['title'].lower():
                            matched = dish
                            break
                    if not matched:
                        for dish in db_dishes:
                            if any(k in dish.title.lower() for k in hero['title'].lower().split() if len(k) > 4):
                                matched = dish
                                break
                    if matched:
                        hero['id'] = matched.id
                        hero['slug'] = matched.slug
                        hero['price'] = str(int(matched.price) if matched.price % 1 == 0 else matched.price)
                        if matched.image:
                            hero['image'] = matched.image.url or str(matched.image)
                        if matched.weight_grams:
                            hero['weight_grams'] = matched.weight_grams
                        if matched.calories:
                            hero['calories'] = matched.calories
                for i, hero in enumerate(hero_dishes):
                    if not any(d.id == hero.get('id') for d in db_dishes):
                        hero['id'] = db_dishes[i % len(db_dishes)].id
            categories = list(Category.objects.filter(is_active=True).order_by('display_order', 'name'))
        except (SynchronousOnlyOperation, DatabaseError, Exception):
            pass

        context['hero_dishes'] = hero_dishes
        context['hero_dishes_json'] = json.dumps(hero_dishes, ensure_ascii=False)
        context['featured_dishes'] = featured_dishes
        context['categories'] = categories
        return context


class RegisterView(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect('accounts:profile')
        form = UserRegistrationForm()
        return render(request, 'accounts/register.html', {'form': form})

    def post(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect('accounts:profile')
        form = UserRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            if request.session.session_key:
                request.session['_pre_login_session_key'] = request.session.session_key
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            send_templated_email(
                subject='Ласкаво просимо до Double Bite!',
                template_prefix='emails/welcome',
                context={
                    'user': user,
                    'customer_name': user.get_full_name() or user.email,
                    'protocol': 'https' if request.is_secure() else 'http',
                    'domain': request.get_host(),
                },
                recipient_list=[user.email],
            )
            messages.success(request, 'Реєстрація успішна! Ласкаво просимо до Double Bite.')
            next_url = request.POST.get('next') or request.GET.get('next')
            if next_url and url_has_allowed_host_and_scheme(
                next_url, allowed_hosts={request.get_host()}
            ):
                return redirect(next_url)
            return redirect('accounts:profile')
        return render(request, 'accounts/register.html', {'form': form})


class LoginView(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect('accounts:profile')
        form = UserLoginForm()
        return render(request, 'accounts/login.html', {'form': form})

    def post(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect('accounts:profile')
        form = UserLoginForm(request.POST)
        if form.is_valid():
            user = form.get_user()
            if user:
                if request.session.session_key:
                    request.session['_pre_login_session_key'] = request.session.session_key
                login(request, user)
                messages.success(request, 'Ви успішно увійшли в систему.')
                next_url = request.POST.get('next') or request.GET.get('next')
                if next_url and url_has_allowed_host_and_scheme(
                    next_url, allowed_hosts={request.get_host()}
                ):
                    return redirect(next_url)
                return redirect('accounts:profile')
        return render(request, 'accounts/login.html', {'form': form})


class LogoutView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        logout(request)
        messages.info(request, 'Ви вийшли з облікового запису.')
        return redirect('home')


class ProfileView(LoginRequiredMixin, View):
    login_url = '/accounts/login/'

    def get(self, request: HttpRequest) -> HttpResponse:
        from orders.models import Order

        user_form = UserProfileForm(instance=request.user)
        address_form = DeliveryAddressForm()
        addresses = list(DeliveryAddress.objects.filter(user=request.user))
        orders = list(Order.objects.filter(user=request.user).prefetch_related('items').order_by('-created_at')[:5])
        return render(
            request,
            'accounts/profile.html',
            {
                'user_form': user_form,
                'address_form': address_form,
                'addresses': addresses,
                'orders': orders,
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        action = request.POST.get('action')
        if action == 'update_profile':
            user_form = UserProfileForm(request.POST, instance=request.user)
            if user_form.is_valid():
                user_form.save()
                messages.success(request, 'Профіль успішно оновлено.')
                return redirect('accounts:profile')
            address_form = DeliveryAddressForm()
        elif action == 'add_address':
            address_form = DeliveryAddressForm(request.POST)
            if address_form.is_valid():
                address = address_form.save(commit=False)
                address.user = request.user
                address.save()
                messages.success(request, 'Адресу успішно додано.')
                return redirect('accounts:profile')
            user_form = UserProfileForm(instance=request.user)
        else:
            user_form = UserProfileForm(instance=request.user)
            address_form = DeliveryAddressForm()

        addresses = DeliveryAddress.objects.filter(user=request.user)
        return render(
            request,
            'accounts/profile.html',
            {
                'user_form': user_form,
                'address_form': address_form,
                'addresses': addresses,
            },
        )


class AddressDeleteView(LoginRequiredMixin, View):
    login_url = '/accounts/login/'

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        address = get_object_or_404(DeliveryAddress, pk=pk, user=request.user)
        address.delete()
        messages.success(request, 'Адресу видалено.')
        return redirect('accounts:profile')


class CustomPasswordResetView(PasswordResetView):
    template_name = 'accounts/password_reset.html'
    email_template_name = 'emails/password_reset_email.txt'
    html_email_template_name = 'emails/password_reset_email.html'
    subject_template_name = 'emails/password_reset_subject.txt'
    success_url = reverse_lazy('accounts:password_reset_done')
    form_class = CustomPasswordResetForm


class CustomPasswordResetDoneView(PasswordResetDoneView):
    template_name = 'accounts/password_reset_done.html'


class CustomPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = 'accounts/password_reset_confirm.html'
    success_url = reverse_lazy('accounts:password_reset_complete')
    form_class = CustomSetPasswordForm


class CustomPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = 'accounts/password_reset_complete.html'
