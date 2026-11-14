from django.http import HttpResponse
from django.template.loader import render_to_string


def render_partial_or_full(
    request,
    template_name: str,
    context: dict | None = None,
    status: int | None = None,
    content_type: str | None = None,
) -> HttpResponse:
    content = render_to_string(template_name, context or {}, request=request)
    return HttpResponse(
        content,
        status=status,
        content_type=content_type,
    )
