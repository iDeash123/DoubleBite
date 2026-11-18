from accounts.views import CustomPasswordResetView, RegisterView
from django.conf import settings
from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path
from django.views.static import serve
from menu.sitemaps import CategorySitemap, DishSitemap, StaticViewSitemap
from menu.views import HomeView, robots_txt_view

sitemaps = {
    'static': StaticViewSitemap,
    'categories': CategorySitemap,
    'dishes': DishSitemap,
}

urlpatterns = [
    path('favicon.ico', serve, {'document_root': settings.BASE_DIR / 'static' / 'favicon', 'path': 'favicon.ico'}),
    path('robots.txt', robots_txt_view, name='robots_txt'),
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='django.contrib.sitemaps.views.sitemap'),
    path(settings.DJANGO_ADMIN_URL, admin.site.urls),
    path('', HomeView.as_view(), name='home'),
    path('accounts/signup/', RegisterView.as_view()),
    path('accounts/password/reset/', CustomPasswordResetView.as_view()),
    path('accounts/', include('accounts.urls')),
    path('accounts/', include('allauth.urls')),
    path('menu/', include('menu.urls')),
    path('orders/', include('orders.urls')),
    path('support/', include('support.urls')),
]

if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()

    def cached_media_serve(request, path, document_root=None, show_indexes=False):
        response = serve(request, path, document_root=document_root, show_indexes=show_indexes)
        if getattr(response, 'status_code', None) == 200:
            response['Cache-Control'] = 'no-cache, must-revalidate'
        return response

    media_prefix = settings.MEDIA_URL.strip('/')
    urlpatterns += [
        path(f"{media_prefix}/<path:path>", cached_media_serve, {'document_root': settings.MEDIA_ROOT}),
    ]
    if 'debug_toolbar' in settings.INSTALLED_APPS:
        try:
            import debug_toolbar

            urlpatterns = [
                path('__debug__/', include(debug_toolbar.urls)),
            ] + urlpatterns
        except ImportError:
            pass

