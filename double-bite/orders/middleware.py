import secrets

from django.http import HttpRequest, HttpResponse

GUEST_TOKEN_SESSION_KEY = 'guest_token'  # noqa: S105 - Django session key name, not a secret
PRE_LOGIN_GUEST_TOKEN_SESSION_KEY = '_pre_login_guest_token'  # noqa: S105 - Django session key name, not a secret


def get_guest_token(request: HttpRequest, *, create: bool = False) -> str:
    session = getattr(request, 'session', None)
    if session is None or not hasattr(session, 'get'):
        return ''

    token = session.get(GUEST_TOKEN_SESSION_KEY)
    if isinstance(token, str) and token:
        return token

    if not create:
        return ''

    if hasattr(session, 'session_key') and not session.session_key and hasattr(session, 'create'):
        session.create()

    token = secrets.token_urlsafe(32)
    session[GUEST_TOKEN_SESSION_KEY] = token
    if hasattr(session, 'modified'):
        session.modified = True
    return token


def get_pre_login_guest_token(request: HttpRequest) -> str:
    session = getattr(request, 'session', None)
    if session is not None and hasattr(session, 'get'):
        token = session.get(PRE_LOGIN_GUEST_TOKEN_SESSION_KEY)
        if isinstance(token, str) and token:
            return token

    attr_token = getattr(request, '_pre_login_guest_token', '')
    return attr_token if isinstance(attr_token, str) and attr_token else ''


class PreserveSessionKeyMiddleware:
    """
    Saves the current session_key and guest_token before each request completes,
    so that they are available in `_pre_login_*` attributes after Django's
    `login()` rotates the session (session fixation protection).

    This is critical for merging guest carts after social login (allauth),
    where the session key changes but our custom LoginView code
    is not called.

    Must be placed after SessionMiddleware in MIDDLEWARE.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        session = getattr(request, 'session', None)
        if session is not None:
            session_key = getattr(session, 'session_key', None)
            if session_key:
                request._pre_login_session_key = session_key

            if hasattr(session, 'get'):
                guest_token = session.get(GUEST_TOKEN_SESSION_KEY)
                if guest_token:
                    request._pre_login_guest_token = guest_token

                guest_cart_id = session.get('guest_cart_id')
                if guest_cart_id:
                    request._pre_login_guest_cart_id = guest_cart_id

        return self.get_response(request)
