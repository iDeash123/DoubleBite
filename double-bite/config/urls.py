from django.contrib import admin
from django.urls import include, path

from accounts.views import HomeView

from orders import views as orders_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', HomeView.as_view(), name='home'),
    path('accounts/', include('accounts.urls')),
    path('menu/', include('menu.urls')),
    path('orders/', include('orders.urls')),
    path('cart/', orders_views.cart_view, name='cart_direct'),
    path('cart/add/<int:dish_id>/', orders_views.cart_add_view, name='cart_add_direct'),
    path('cart/update/<int:item_id>/', orders_views.cart_update_view, name='cart_update_direct'),
    path('cart/remove/<int:item_id>/', orders_views.cart_remove_view, name='cart_remove_direct'),
]
