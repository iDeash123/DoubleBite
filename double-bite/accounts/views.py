from typing import Any

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import TemplateView

from .forms import DeliveryAddressForm, UserLoginForm, UserProfileForm, UserRegistrationForm
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
        addresses = DeliveryAddress.objects.filter(user=request.user)
        orders = Order.objects.filter(user=request.user).prefetch_related('items').order_by('-created_at')[:5]
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
