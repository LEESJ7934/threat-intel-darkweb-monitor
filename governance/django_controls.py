"""Django access gates and auth audit receivers; no external services."""
from functools import wraps

from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseForbidden
from django.utils.cache import add_never_cache_headers

from . import audit


def protected_view(category):
    event = "dashboard_access" if category == "dashboard" else "event_detail_access"

    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            user = request.user
            authenticated = bool(user.is_authenticated)
            fields = {"category": category, "authenticated": authenticated,
                      "user_id": user.pk if authenticated else None,
                      "document_id": kwargs.get("document_id")}
            try:
                if not settings.DEBUG and not settings.DASHBOARD_REQUIRE_AUTH:
                    response = HttpResponseForbidden("Dashboard authentication is required.")
                elif (request.method in {"GET", "HEAD"} and settings.DASHBOARD_REQUIRE_AUTH
                      and not authenticated):
                    # Do not copy search text or document IDs into the login URL.
                    response = redirect_to_login("/", settings.LOGIN_URL)
                else:
                    response = view(request, *args, **kwargs)
            except Exception:
                audit.emit(event, result="error", status=500, **fields)
                raise
            add_never_cache_headers(response)
            status = response.status_code
            result = ("denied" if status in {302, 403, 405} else
                      "not_found" if status == 404 else "error" if status >= 500 else "success")
            audit.emit(event, result=result, status=status, **fields)
            return response
        return wrapped
    return decorate


def login_success(sender, request, user, **kwargs):
    audit.emit("login_success", category="authentication", result="success",
               authenticated=True, user_id=user.pk)


def login_failure(sender, credentials, request=None, **kwargs):
    # Intentionally ignore credentials, username, request body, IP and query.
    audit.emit("login_failure", category="authentication", result="denied")


def logout(sender, request, user, **kwargs):
    audit.emit("logout", category="authentication", result="success",
               authenticated=bool(user and user.is_authenticated), user_id=user.pk if user else None)


def connect_signals():
    user_logged_in.connect(login_success, dispatch_uid="day13_login_success", weak=False)
    user_login_failed.connect(login_failure, dispatch_uid="day13_login_failure", weak=False)
    user_logged_out.connect(logout, dispatch_uid="day13_logout", weak=False)
