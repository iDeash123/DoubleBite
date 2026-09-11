from django.urls import path

from . import views

app_name = 'orders'

urlpatterns = [
    path('cart/', views.cart_view, name='cart'),
    path('cart/drawer/', views.cart_drawer_view, name='cart_drawer'),
    path('cart/add/<int:dish_id>/', views.cart_add_view, name='cart_add'),
    path('cart/update/<int:item_id>/', views.cart_update_view, name='cart_update'),
    path('cart/remove/<int:item_id>/', views.cart_remove_view, name='cart_remove'),
    path('cart/clear/', views.cart_clear_view, name='cart_clear'),
    path('checkout/', views.checkout_view, name='checkout'),
    path('tracking/<str:order_number>/', views.tracking_view, name='tracking'),
    path('tracking/<str:order_number>/status/', views.tracking_status_view, name='tracking_status'),
    path('cancel/<str:order_number>/', views.order_cancel_view, name='order_cancel'),
    path('history/', views.order_list_view, name='order_list'),
    path('webhook/stripe/', views.stripe_webhook_view, name='stripe_webhook'),
    path('checkout/stripe/<str:order_number>/', views.stripe_checkout_view, name='stripe_checkout'),
]

