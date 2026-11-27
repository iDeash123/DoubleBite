from typing import Any

from django.core.exceptions import SynchronousOnlyOperation
from django.db import DatabaseError
from django.http import HttpRequest
from orders.context_processors import LazyValue

from support.models import ChatSession, ChatSessionStatus
from support.permissions import is_support_operator


def support_context(request: HttpRequest) -> dict[str, Any]:
    def _is_operator() -> bool:
        if hasattr(request, '_cached_is_support_operator'):
            return bool(request._cached_is_support_operator)
        user = getattr(request, 'user', None)
        try:
            allowed = is_support_operator(user)
        except (SynchronousOnlyOperation, DatabaseError, AttributeError):
            allowed = False
        request._cached_is_support_operator = allowed
        return allowed

    def _waiting_count() -> int:
        if hasattr(request, '_cached_support_waiting_count'):
            return int(request._cached_support_waiting_count)
        if not _is_operator():
            request._cached_support_waiting_count = 0
            return 0
        try:
            count = ChatSession.objects.filter(
                status=ChatSessionStatus.ESCALATED,
                assigned_operator__isnull=True,
            ).count()
        except (SynchronousOnlyOperation, DatabaseError):
            count = 0
        request._cached_support_waiting_count = count
        return count

    return {
        'is_support_operator': LazyValue(_is_operator),
        'support_waiting_count': LazyValue(_waiting_count),
    }
