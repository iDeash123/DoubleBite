from django.http import HttpRequest, HttpResponse


class PreserveSessionKeyMiddleware:
    """
    Saves the current session_key before each request completes,
    so that it's available in `_pre_login_session_key` after Django's
    `login()` rotates the session (session fixation protection).

    This is critical for merging guest carts after social login (allauth),
    where the session key changes but our custom LoginView code
    (which saves _pre_login_session_key) is not called.

    Must be placed after SessionMiddleware in MIDDLEWARE.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        session_key = getattr(request.session, 'session_key', None)
        if session_key:
            request._pre_login_session_key = session_key

        guest_cart_id = request.session.get('guest_cart_id')
        if guest_cart_id:
            request._pre_login_guest_cart_id = guest_cart_id

        response = self.get_response(request)

        return response
