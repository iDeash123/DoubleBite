from decimal import Decimal
from typing import Any

from allauth.account.models import EmailAddress
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
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import TemplateView


def _get_client_ip(request: HttpRequest) -> str:
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '127.0.0.1')


def _is_rate_limited(key: str, limit: int, period: int = 60) -> bool:
    if cache.add(key, 0, timeout=period) is False:
        pass
    try:
        val = cache.incr(key)
    except Exception:
        val = 1
        cache.set(key, val, timeout=period)
    return val > limit

from .forms import (
    CustomPasswordResetForm,
    CustomSetPasswordForm,
    DeliveryAddressForm,
    UserLoginForm,
    UserProfileForm,
    UserRegistrationForm,
)
from .models import DeliveryAddress
from .selectors import get_home_page_context


class HomeView(TemplateView):
    template_name = 'home/index.html'

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        total_amount = context.get('total_amount', Decimal('0.00')) or Decimal('0.00')
        context.update(get_home_page_context(total_amount=total_amount))
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
        ip = _get_client_ip(request)
        if _is_rate_limited(f'rl:register:ip:{ip}', limit=10, period=60):
            messages.error(request, 'Забагато спроб реєстрації. Будь ласка, зачекайте хвилину.')
            return render(request, 'accounts/register.html', {'form': UserRegistrationForm(request.POST)})
        form = UserRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            EmailAddress.objects.get_or_create(user=user, email=user.email, defaults={'verified': False, 'primary': True})
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
        ip = _get_client_ip(request)
        if cache.get(f'rl:login:blocked:ip:{ip}'):
            messages.error(request, 'Забагато невдалих спроб входу. Будь ласка, зачекайте хвилину.')
            return render(request, 'accounts/login.html', {'form': UserLoginForm(request.POST)})
        form = UserLoginForm(request.POST)
        if form.is_valid():
            user = form.get_user()
            if user:
                cache.delete(f'rl:login:failed:ip:{ip}')
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
        else:
            if _is_rate_limited(f'rl:login:failed:ip:{ip}', limit=5, period=60):
                cache.set(f'rl:login:blocked:ip:{ip}', True, timeout=60)
                messages.error(request, 'Забагато невдалих спроб входу. Будь ласка, зачекайте хвилину.')
        return render(request, 'accounts/login.html', {'form': form})


class LogoutView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        logout(request)
        messages.info(request, 'Ви вийшли з облікового запису.')
        return redirect('home')


class ProfileView(LoginRequiredMixin, View):
    login_url = '/accounts/login/'

    def _get_context(self, request: HttpRequest, user_form: Any, address_form: Any) -> dict[str, Any]:
        from orders.models import Order

        addresses = list(DeliveryAddress.objects.filter(user=request.user))
        orders = list(Order.objects.filter(user=request.user).prefetch_related('items').order_by('-created_at')[:5])
        return {
            'user_form': user_form,
            'address_form': address_form,
            'addresses': addresses,
            'orders': orders,
        }

    def get(self, request: HttpRequest) -> HttpResponse:
        user_form = UserProfileForm(instance=request.user)
        address_form = DeliveryAddressForm()
        return render(request, 'accounts/profile.html', self._get_context(request, user_form, address_form))

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

        return render(request, 'accounts/profile.html', self._get_context(request, user_form, address_form))


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

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        ip = _get_client_ip(request)
        if _is_rate_limited(f'rl:pw_reset:ip:{ip}', limit=5, period=60):
            messages.error(request, 'Забагато запитів на скидання пароля. Будь ласка, зачекайте хвилину.')
            form = self.get_form()
            return self.form_invalid(form)
        return super().post(request, *args, **kwargs)


class CustomPasswordResetDoneView(PasswordResetDoneView):
    template_name = 'accounts/password_reset_done.html'


class CustomPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = 'accounts/password_reset_confirm.html'
    success_url = reverse_lazy('accounts:password_reset_complete')
    form_class = CustomSetPasswordForm


class CustomPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = 'accounts/password_reset_complete.html'
