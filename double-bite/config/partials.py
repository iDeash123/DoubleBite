from django.http import HttpResponse
from django.shortcuts import render
from django.template.context import RequestContext
from django.template.loader import get_template


def render_partial_or_full(
    request,
    template_name: str,
    context: dict | None = None,
    status: int | None = None,
    content_type: str | None = None,
) -> HttpResponse:
    if '#' in template_name:
        base_name, partial_name = template_name.split('#', 1)
        tmpl = get_template(base_name)
        django_template = getattr(tmpl, 'template', tmpl)
        partials = getattr(django_template, 'extra_data', {}).get('partials', {})
        if partial_name in partials:
            partial_obj = partials[partial_name]
            if not getattr(partial_obj, 'engine', None):
                partial_obj.engine = getattr(django_template, 'engine', None)
            ctx = RequestContext(request, context or {})
            return HttpResponse(
                partial_obj.render(ctx),
                status=status,
                content_type=content_type or 'text/html; charset=utf-8',
            )
        template_name = base_name

    return render(
        request,
        template_name,
        context=context,
        status=status,
        content_type=content_type,
    )
