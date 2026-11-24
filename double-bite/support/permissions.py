from collections.abc import Callable
from functools import wraps
from typing import Any

from accounts.models import Role
from asgiref.sync import iscoroutinefunction, sync_to_async
from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import resolve_url


def is_support_operator(user: Any) -> bool:
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if not getattr(user, 'is_active', True):
        return False
    return bool(
        getattr(user, 'is_superuser', False)
        or getattr(user, 'role', None) == Role.RESTAURANT_ADMIN
    )


def _check_operator_access(
    request: HttpRequest,
    user: Any,
    login_url: str,
) -> HttpResponse | None:
    if user is None or not getattr(user, 'is_authenticated', False):
        return redirect_to_login(
            request.get_full_path(),
            resolve_url(login_url),
        )
    if not is_support_operator(user):
        return HttpResponseForbidden('Доступ дозволено лише менеджерам підтримки.')
    return None


def support_operator_required(
    view_func: Callable[..., Any] | None = None,
    *,
    login_url: str = 'accounts:login',
) -> Callable[..., Any]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapped(
                request: HttpRequest,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                auser = getattr(request, 'auser', None)
                if callable(auser):
                    user = await auser()
                    denied = _check_operator_access(request, user, login_url)
                else:
                    denied = await sync_to_async(
                        lambda: _check_operator_access(
                            request,
                            getattr(request, 'user', None),
                            login_url,
                        ),
                        thread_sensitive=True,
                    )()
                if denied is not None:
                    return denied
                return await func(request, *args, **kwargs)

            return async_wrapped

        @wraps(func)
        def sync_wrapped(
            request: HttpRequest,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            user = getattr(request, 'user', None)
            denied = _check_operator_access(request, user, login_url)
            if denied is not None:
                return denied
            return func(request, *args, **kwargs)

        return sync_wrapped

    if view_func is not None:
        return decorator(view_func)
    return decorator
