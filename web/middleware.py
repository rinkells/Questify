"""Middleware that applies to the web UI only; the API is left as it is."""
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from django.conf import settings
from django.http import HttpResponse
from django.urls import Resolver404, resolve
from django.utils import timezone, translation

from core.models import Character

WEB_APP_NAME = 'web'
WEB_LANGUAGE = 'uk'


def is_web_request(request):
    """Whether the URL belongs to the ``web`` namespace (cached per request)."""
    cached = getattr(request, '_is_web_request', None)
    if cached is None:
        try:
            cached = WEB_APP_NAME in resolve(request.path_info).app_names
        except Resolver404:
            cached = False
        request._is_web_request = cached
    return cached


class RejectMultipartMiddleware:
    """Refuse multipart bodies on web URLs with 415.

    No form takes a file. Must sit above CsrfViewMiddleware: that reads the
    token from ``request.POST``, which would stream a multipart body to
    temporary storage before any view could say no.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.method not in ('GET', 'HEAD', 'OPTIONS', 'TRACE')
            and request.content_type == 'multipart/form-data'
            and is_web_request(request)
        ):
            return HttpResponse('Unsupported media type.', status=415)
        return self.get_response(request)


class WebContextMiddleware:
    """Activate the user's timezone and the Ukrainian language on web URLs.

    Only the web: API responses keep UTC and the project's language. Sits
    after AuthenticationMiddleware.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not is_web_request(request):
            return self.get_response(request)
        name = None
        if request.user.is_authenticated:
            name = Character.objects.filter(user=request.user).values_list(
                'timezone',
                flat=True,
            ).first()
        try:
            zone = ZoneInfo(name) if name else None
        except (KeyError, ValueError, OSError):
            zone = None
        if zone is not None:
            timezone.activate(zone)
        try:
            with translation.override(WEB_LANGUAGE):
                return self.get_response(request)
        finally:
            timezone.deactivate()


class HtmxSessionExpiryMiddleware:
    """Answer an htmx request that was redirected to the login page with HX-Redirect.

    Without it htmx follows the redirect and swaps the whole login page into
    a fragment of the screen.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            request.headers.get('HX-Request') == 'true'
            and response.status_code in (301, 302, 303)
            and response.headers.get('Location', '').startswith(
                settings.LOGIN_URL,
            )
            and is_web_request(request)
        ):
            redirect = HttpResponse(status=200)
            # The page the user is on, not the fragment URL htmx asked for.
            current = request.headers.get('HX-Current-URL', '')
            target = settings.LOGIN_URL
            if current:
                parts = urlsplit(current)
                if parts.netloc == request.get_host():
                    path = parts.path + (f'?{parts.query}' if parts.query else '')
                    target = f'{settings.LOGIN_URL}?next={quote(path)}'
            redirect.headers['HX-Redirect'] = target
            return redirect
        return response
