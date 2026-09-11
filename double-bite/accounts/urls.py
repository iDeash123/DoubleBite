from django.urls import path

from .views import AddressDeleteView, LoginView, LogoutView, ProfileView, RegisterView

app_name = 'accounts'

urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('profile/', ProfileView.as_view(), name='profile'),
    path('address/<int:pk>/delete/', AddressDeleteView.as_view(), name='delete_address'),
]
