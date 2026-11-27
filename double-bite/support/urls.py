from django.urls import path

from . import desk_views, telegram_views, views

app_name = 'support'

urlpatterns = [
    path('chat/stream/', views.chat_stream_view, name='chat_stream'),
    path('chat/escalate/', views.chat_escalate_view, name='chat_escalate'),
    path('chat/message/', views.chat_message_view, name='chat_message'),
    path('chat/reset/', views.chat_reset_view, name='chat_reset'),
    path('chat/history/', views.chat_history_view, name='chat_history'),
    path('chat/events/', views.chat_events_view, name='chat_events'),
    path('telegram/webhook/', telegram_views.telegram_webhook_view, name='telegram_webhook'),
    path('desk/', desk_views.desk_index_view, name='desk_index'),
    path('desk/events/', desk_views.desk_events_view, name='desk_events'),
    path(
        'desk/partials/queue/',
        desk_views.desk_queue_partial_view,
        name='desk_queue_partial',
    ),
    path(
        'desk/partials/thread/<uuid:session_uuid>/',
        desk_views.desk_thread_partial_view,
        name='desk_thread_partial',
    ),
    path(
        'desk/partials/history/<uuid:past_session_uuid>/',
        desk_views.desk_past_history_partial_view,
        name='desk_past_history_partial',
    ),
    path(
        'desk/api/sessions/<uuid:session_uuid>/messages/',
        desk_views.desk_messages_api_view,
        name='desk_messages_api',
    ),
    path(
        'desk/api/sessions/<uuid:session_uuid>/claim/',
        desk_views.desk_claim_view,
        name='desk_claim',
    ),
    path(
        'desk/api/sessions/<uuid:session_uuid>/takeover/',
        desk_views.desk_takeover_view,
        name='desk_takeover',
    ),
    path(
        'desk/api/sessions/<uuid:session_uuid>/reply/',
        desk_views.desk_reply_view,
        name='desk_reply',
    ),
    path(
        'desk/api/sessions/<uuid:session_uuid>/return-ai/',
        desk_views.desk_return_ai_view,
        name='desk_return_ai',
    ),
    path(
        'desk/api/sessions/<uuid:session_uuid>/resolve/',
        desk_views.desk_resolve_view,
        name='desk_resolve',
    ),
    path(
        'desk/<uuid:session_uuid>/',
        desk_views.desk_session_view,
        name='desk_session',
    ),
]
