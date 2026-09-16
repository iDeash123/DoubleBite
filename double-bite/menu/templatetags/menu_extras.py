from urllib.parse import urlencode

from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def query_transform(context, **kwargs):
    """Return the current GET querystring with the given kwargs overridden.

    Usage in template:
        href="?{% query_transform page=3 %}"
    """
    request = context.get("request")
    if request is None:
        return urlencode(kwargs)
    params = request.GET.copy()
    for key, value in kwargs.items():
        params[key] = value
    return params.urlencode()
