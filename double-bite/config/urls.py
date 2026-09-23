from accounts.views import HomeView
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path
from django.views.static import serve
from orders import views as orders_views

urlpatterns = [
    path('favicon.ico', serve, {'document_root': settings.BASE_DIR / 'static' / 'favicon', 'path': 'favicon.ico'}),
    path('admin/', admin.site.urls),
    path('', HomeView.as_view(), name='home'),
    path('accounts/', include('accounts.urls')),
    path('accounts/', include('allauth.urls')),
    path('menu/', include('menu.urls')),
    path('orders/', include('orders.urls')),
    path('support/', include('support.urls')),
    path('cart/', orders_views.cart_view, name='cart_direct'),
    path('cart/drawer/', orders_views.cart_drawer_view, name='cart_drawer_direct'),
    path('cart/add/<int:dish_id>/', orders_views.cart_add_view, name='cart_add_direct'),
    path('cart/update/<int:item_id>/', orders_views.cart_update_view, name='cart_update_direct'),
    path('cart/remove/<int:item_id>/', orders_views.cart_remove_view, name='cart_remove_direct'),
    path('cart/clear/', orders_views.cart_clear_view, name='cart_clear_direct'),
]

if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()

    def cached_media_serve(request, path, document_root=None, show_indexes=False):
        response = serve(request, path, document_root=document_root, show_indexes=show_indexes)
        if getattr(response, 'status_code', None) == 200:
            response['Cache-Control'] = 'public, max-age=86400'
        return response

    media_prefix = settings.MEDIA_URL.strip('/')
    urlpatterns += [
        path(f"{media_prefix}/<path:path>", cached_media_serve, {'document_root': settings.MEDIA_ROOT}),
    ]
    try:
        import debug_toolbar
        urlpatterns = [
            path('__debug__/', include(debug_toolbar.urls)),
        ] + urlpatterns
    except ImportError:
        pass

