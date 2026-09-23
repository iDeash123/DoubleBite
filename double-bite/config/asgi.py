import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')

from django.conf import settings
from django.core.asgi import get_asgi_application

if settings.DEBUG:
    from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler

    class CachedASGIStaticFilesHandler(ASGIStaticFilesHandler):
        async def get_response_async(self, request):
            response = await super().get_response_async(request)
            if getattr(response, 'status_code', None) == 200:
                response.headers['Cache-Control'] = 'public, max-age=86400'
            return response

        def get_response(self, request):
            response = super().get_response(request)
            if getattr(response, 'status_code', None) == 200:
                response.headers['Cache-Control'] = 'public, max-age=86400'
            return response

    application = CachedASGIStaticFilesHandler(get_asgi_application())
else:
    application = get_asgi_application()
