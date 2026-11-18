import logging
import smtplib
from typing import Any

from allauth.account.internal.flows.email_verification import (
    send_verification_email_to_address,
)
from allauth.account.models import EmailAddress
from allauth.account.signals import user_signed_up
from allauth.core.context import request_context
from config.security import (
    consume_rate_limit,
    get_client_ip,
    get_safe_next_url,
    rate_limit_429_response,
    reset_rate_limit,
)
from config.transactions import schedule_on_commit
from config.validators import normalize_email
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import (
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.db import IntegrityError, transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View

from .forms import (
    CustomPasswordResetForm,
    CustomSetPasswordForm,
    DeliveryAddressForm,
    UserLoginForm,
    UserProfileForm,
    UserRegistrationForm,
)
from .models import DeliveryAddress
from .selectors import get_user_profile_context

logger = logging.getLogger('accounts')
User = get_user_model()


def _send_verification_email_with_context(
    request: HttpRequest,
    email_address: EmailAddress,
) -> None:
    with request_context(request):
        send_verification_email_to_address(
            request,
            email_address,
            signup=True,
        )


def _render_profile_error(
    request: HttpRequest,
    message: str,
    *,
    status: int = 400,
    user_form: UserProfileForm | None = None,
    address_form: DeliveryAddressForm | None = None,
    editing_address: DeliveryAddress | None = None,
    edit_address_form: DeliveryAddressForm | None = None,
) -> HttpResponse:
    messages.error(request, message)
    context = get_user_profile_context(
        request.user,
        user_form or UserProfileForm(instance=request.user),
        address_form or DeliveryAddressForm(),
        editing_address=editing_address,
    )
    if edit_address_form is not None:
        context['edit_address_form'] = edit_address_form
    return render(request, 'accounts/profile.html', context, status=status)


def _parse_address_pk(raw_pk: str | None) -> int | None:
    if not raw_pk:
        return None
    try:
        pk = int(raw_pk)
    except (TypeError, ValueError):
        return None
    return pk if pk > 0 else None


class RegisterView(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect('accounts:profile')
        safe_next = get_safe_next_url(
            request,
            request.GET.get('next'),
            fallback='',
        )
        return render(
            request,
            'accounts/register.html',
            {'form': UserRegistrationForm(), 'next': safe_next},
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect('accounts:profile')
        raw_next = request.POST.get('next') or request.GET.get('next')
        safe_next = get_safe_next_url(request, raw_next, fallback='')
        ip = get_client_ip(request)
        limited, retry_after = consume_rate_limit(
            'register_ip',
            ip,
            limit=10,
            period=60,
        )
        if limited:
            msg = (
                'Забагато спроб реєстрації. Будь ласка, зачекайте хвилину.'
            )
            messages.error(request, msg)
            initial_data = {
                'email': request.POST.get('email', ''),
                'first_name': request.POST.get('first_name', ''),
                'last_name': request.POST.get('last_name', ''),
                'phone': request.POST.get('phone', ''),
            }
            return rate_limit_429_response(
                request,
                retry_after,
                msg,
                template_name='accounts/register.html',
                context={
                    'form': UserRegistrationForm(initial=initial_data),
                    'next': safe_next,
                },
            )

        form = UserRegistrationForm(request.POST)
        if form.is_valid():
            verification_mode = getattr(
                settings,
                'ACCOUNT_EMAIL_VERIFICATION',
                'optional',
            )
            try:
                with transaction.atomic():
                    user = form.save()
                    email_address, _ = EmailAddress.objects.update_or_create(
                        user=user,
                        email=user.email,
                        defaults={'verified': False, 'primary': True},
                    )
                    user_signed_up.send(
                        sender=user.__class__,
                        request=request,
                        user=user,
                    )
                    if verification_mode != 'none':
                        schedule_on_commit(
                            _send_verification_email_with_context,
                            request,
                            email_address,
                        )
            except IntegrityError:
                form.add_error('email', 'Користувач з таким email вже існує.')
                return render(
                    request,
                    'accounts/register.html',
                    {'form': form, 'next': safe_next},
                    status=400,
                )

            if verification_mode == 'mandatory':
                messages.info(
                    request,
                    'Реєстрація успішна! На вашу пошту надіслано лист '
                    'для підтвердження email.',
                )
                return redirect('account_email_verification_sent')

            messages.success(
                request,
                'Реєстрація успішна! Ласкаво просимо до Double Bite.',
            )
            return redirect(safe_next or reverse('accounts:profile'))

        return render(
            request,
            'accounts/register.html',
            {'form': form, 'next': safe_next},
        )


class LoginView(View):
    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect('accounts:profile')
        safe_next = get_safe_next_url(
            request,
            request.GET.get('next'),
            fallback='',
        )
        return render(
            request,
            'accounts/login.html',
            {'form': UserLoginForm(), 'next': safe_next},
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect('accounts:profile')
        raw_next = request.POST.get('next') or request.GET.get('next')
        safe_next = get_safe_next_url(request, raw_next, fallback='')
        ip = get_client_ip(request)
        normalized_email = normalize_email(request.POST.get('email'))

        ip_limited, ip_retry_after = consume_rate_limit(
            'login_ip',
            ip,
            limit=5,
            period=60,
        )
        email_limited = False
        email_retry_after = 0
        if normalized_email:
            email_limited, email_retry_after = consume_rate_limit(
                'login_email',
                normalized_email,
                limit=5,
                period=60,
            )

        if ip_limited or email_limited:
            retry_after = max(ip_retry_after, email_retry_after)
            msg = (
                'Забагато невдалих спроб входу. Будь ласка, зачекайте хвилину.'
            )
            messages.error(request, msg)
            return rate_limit_429_response(
                request,
                retry_after,
                msg,
                template_name='accounts/login.html',
                context={
                    'form': UserLoginForm(
                        initial={'email': request.POST.get('email', '')}
                    ),
                    'next': safe_next,
                },
            )

        form = UserLoginForm(request.POST)
        if form.is_valid():
            user = form.get_user()
            if user:
                reset_rate_limit('login_ip', ip)
                if normalized_email:
                    reset_rate_limit('login_email', normalized_email)
                login(
                    request,
                    user,
                    backend='django.contrib.auth.backends.ModelBackend',
                )
                messages.success(request, 'Ви успішно увійшли в систему.')
                return redirect(safe_next or reverse('accounts:profile'))

        return render(
            request,
            'accounts/login.html',
            {'form': form, 'next': safe_next},
        )


class LogoutView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        logout(request)
        messages.info(request, 'Ви вийшли з облікового запису.')
        return redirect('home')


class ProfileView(LoginRequiredMixin, View):
    login_url = reverse_lazy('accounts:login')

    def get(self, request: HttpRequest) -> HttpResponse:
        user_form = UserProfileForm(instance=request.user)
        address_form = DeliveryAddressForm()
        editing_address = None
        edit_pk = _parse_address_pk(request.GET.get('edit'))
        if edit_pk is not None:
            editing_address = DeliveryAddress.objects.filter(
                pk=edit_pk,
                user=request.user,
            ).first()
        context = get_user_profile_context(
            request.user,
            user_form,
            address_form,
            editing_address=editing_address,
        )
        return render(request, 'accounts/profile.html', context)

    def post(self, request: HttpRequest) -> HttpResponse:
        action = request.POST.get('action')
        if action == 'update_profile':
            user_form = UserProfileForm(request.POST, instance=request.user)
            if user_form.is_valid():
                user_form.save()
                messages.success(request, 'Профіль успішно оновлено.')
                return redirect('accounts:profile')
            address_form = DeliveryAddressForm()
            return render(
                request,
                'accounts/profile.html',
                get_user_profile_context(request.user, user_form, address_form),
            )

        if action == 'add_address':
            address_form = DeliveryAddressForm(request.POST)
            if address_form.is_valid():
                address = address_form.save(commit=False)
                address.user = request.user
                address.save()
                messages.success(request, 'Адресу успішно додано.')
                return redirect('accounts:profile')
            user_form = UserProfileForm(instance=request.user)
            return render(
                request,
                'accounts/profile.html',
                get_user_profile_context(request.user, user_form, address_form),
            )

        if action in {'edit_address', 'address_edit'}:
            pk = _parse_address_pk(
                request.POST.get('address_id') or request.POST.get('pk')
            )
            if pk is None:
                return _render_profile_error(
                    request,
                    'Некоректний ідентифікатор адреси.',
                )
            address = get_object_or_404(
                DeliveryAddress,
                pk=pk,
                user=request.user,
            )
            edit_form = DeliveryAddressForm(request.POST, instance=address)
            if edit_form.is_valid():
                edit_form.save()
                messages.success(request, 'Адресу успішно оновлено.')
                return redirect('accounts:profile')
            return _render_profile_error(
                request,
                'Перевірте коректність даних адреси.',
                status=200,
                editing_address=address,
                edit_address_form=edit_form,
            )

        if action in {'set_default_address', 'address_set_default'}:
            pk = _parse_address_pk(
                request.POST.get('address_id') or request.POST.get('pk')
            )
            if pk is None:
                return _render_profile_error(
                    request,
                    'Некоректний ідентифікатор адреси.',
                )
            address = get_object_or_404(
                DeliveryAddress,
                pk=pk,
                user=request.user,
            )
            address.is_default = True
            address.save()
            messages.success(request, 'Основну адресу оновлено.')
            return redirect('accounts:profile')

        return _render_profile_error(request, 'Невідома дія.')


class AddressEditView(LoginRequiredMixin, View):
    login_url = reverse_lazy('accounts:login')

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        address = get_object_or_404(
            DeliveryAddress,
            pk=pk,
            user=request.user,
        )
        context = get_user_profile_context(
            request.user,
            UserProfileForm(instance=request.user),
            DeliveryAddressForm(),
            editing_address=address,
        )
        return render(request, 'accounts/profile.html', context)

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        address = get_object_or_404(
            DeliveryAddress,
            pk=pk,
            user=request.user,
        )
        action = request.POST.get('action')
        if action and action not in {'edit_address', 'address_edit'}:
            return _render_profile_error(request, 'Невідома дія.')

        edit_form = DeliveryAddressForm(request.POST, instance=address)
        if edit_form.is_valid():
            edit_form.save()
            messages.success(request, 'Адресу успішно оновлено.')
            return redirect('accounts:profile')

        return _render_profile_error(
            request,
            'Перевірте коректність даних адреси.',
            status=200,
            editing_address=address,
            edit_address_form=edit_form,
        )


class AddressSetDefaultView(LoginRequiredMixin, View):
    login_url = reverse_lazy('accounts:login')

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        address = get_object_or_404(
            DeliveryAddress,
            pk=pk,
            user=request.user,
        )
        action = request.POST.get('action')
        if action and action not in {
            'set_default_address',
            'address_set_default',
        }:
            return _render_profile_error(request, 'Невідома дія.')

        address.is_default = True
        address.save()
        messages.success(request, 'Основну адресу оновлено.')
        return redirect('accounts:profile')


class AddressDeleteView(LoginRequiredMixin, View):
    login_url = reverse_lazy('accounts:login')

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        with transaction.atomic():
            User.objects.select_for_update().filter(
                pk=request.user.pk
            ).exists()
            address = (
                DeliveryAddress.objects.select_for_update()
                .filter(pk=pk, user=request.user)
                .first()
            )
            if address is None:
                raise Http404('Адресу доставки не знайдено.')
            was_default = address.is_default
            address.delete()
            if was_default:
                next_address = (
                    DeliveryAddress.objects.select_for_update()
                    .filter(user=request.user)
                    .order_by('-created_at', '-pk')
                    .first()
                )
                if next_address is not None:
                    next_address.is_default = True
                    next_address.save()
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
        ip = get_client_ip(request)
        limited, retry_after = consume_rate_limit(
            'pw_reset_ip',
            ip,
            limit=5,
            period=60,
        )
        if limited:
            msg = (
                'Забагато запитів на скидання пароля. Будь ласка, зачекайте хвилину.'
            )
            messages.error(request, msg)
            form = self.form_class()
            return rate_limit_429_response(
                request,
                retry_after,
                msg,
                template_name=self.template_name,
                context=self.get_context_data(form=form),
            )
        try:
            return super().post(request, *args, **kwargs)
        except (smtplib.SMTPException, OSError):
            logger.exception('Помилка надсилання листа для відновлення пароля')
            messages.error(
                request,
                'Не вдалося надіслати лист для відновлення пароля. '
                'Будь ласка, спробуйте пізніше.',
            )
            form = self.get_form()
            return self.render_to_response(self.get_context_data(form=form))


class CustomPasswordResetDoneView(PasswordResetDoneView):
    template_name = 'accounts/password_reset_done.html'


class CustomPasswordResetConfirmView(PasswordResetConfirmView):
    template_name = 'accounts/password_reset_confirm.html'
    success_url = reverse_lazy('accounts:password_reset_complete')
    form_class = CustomSetPasswordForm


class CustomPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = 'accounts/password_reset_complete.html'
