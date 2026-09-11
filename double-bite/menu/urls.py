from django.urls import path

from . import views

app_name = 'menu'

urlpatterns = [
    path('', views.catalog_view, name='catalog'),
    path('<slug:dish_slug>/', views.menu_slug_dispatch_view, name='dish_detail'),
    path('<slug:category_slug>/<slug:dish_slug>/', views.dish_detail_view, name='category_dish_detail'),
]
