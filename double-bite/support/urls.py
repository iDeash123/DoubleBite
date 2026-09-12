from django.urls import path
from . import views

app_name = 'support'

urlpatterns = [
    path('chat/stream/', views.chat_stream_view, name='chat_stream'),
    path('chat/reset/', views.chat_reset_view, name='chat_reset'),
    path('chat/history/', views.chat_history_view, name='chat_history'),
]
