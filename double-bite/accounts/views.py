
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
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
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


class HomeView(TemplateView):
    template_name = 'base.html'


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
            login(request, user)
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
            return redirect(request.GET.get('next', 'accounts:profile'))
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
                return redirect(request.GET.get('next', 'accounts:profile'))
        return render(request, 'accounts/login.html', {'form': form})


class LogoutView(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        logout(request)
        messages.info(request, 'Ви вийшли з облікового запису.')
        return redirect('home')

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
