"""Authentication uses an ephemeral SQLite test DB; threat data stays fake."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_login_failed
from django.test import Client, TestCase, SimpleTestCase, override_settings
from django.urls import reverse

from governance import audit, django_controls
from . import dashboard as ui
from .tests import ReadDatabase, sample

ROOT = Path(__file__).resolve().parents[2]


@override_settings(DASHBOARD_REQUIRE_AUTH=True, DEBUG=True,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AccessControlTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "synthetic-only-" + "credential"
        cls.user = get_user_model().objects.create_user(username="offline-analyst", password=cls.password)

    def setUp(self):
        self.db = ReadDatabase([sample()])
        self.audit = self.enterContext(patch.object(audit, "emit", wraps=lambda *a, **k: None))
        for target in ("socket.socket.connect", "socket.getaddrinfo", "pymongo.MongoClient"):
            self.enterContext(patch(target, side_effect=AssertionError("External service forbidden")))
        @contextmanager
        def connect():
            yield self.db
        self.mongo = self.enterContext(patch.object(ui, "mongo_database", side_effect=connect))
        self.detail_url = reverse("event_detail", args=[ui.encode_id("bitlock_demo")])

    def login(self):
        return self.client.post(reverse("login"), {"username": self.user.username, "password": self.password})

    def test_unauthenticated_dashboard_redirects_before_database_access(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/login/"))
        self.mongo.assert_not_called()
        self.assertEqual(self.audit.call_args.kwargs["result"], "denied")

    def test_unauthenticated_detail_and_head_use_same_gate(self):
        for method in (self.client.get, self.client.head):
            self.assertEqual(method(self.detail_url).status_code, 302)
        self.mongo.assert_not_called()

    def test_search_and_document_id_not_reflected_in_login_redirect(self):
        response = self.client.get(self.detail_url, {"q": "PRIVATE QUERY"})
        self.assertEqual(response.url, "/login/?next=/")
        self.assertNotIn("PRIVATE", response.url)
        self.assertNotIn("bitlock_demo", response.url)

    def test_login_form_renders_without_mongo_and_never_echoes_password(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, 'type="password"')
        self.assertContains(response, '<meta name="referrer" content="same-origin">', html=True)
        self.assertNotContains(response, '<meta name="referrer" content="no-referrer">', html=True)
        self.assertNotContains(response, self.password)
        self.mongo.assert_not_called()

    def test_builtin_login_succeeds_then_dashboard_accesses_fake_mongo(self):
        response = self.login()
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.mongo.assert_called_once()
        events = [call.args[0] for call in self.audit.call_args_list]
        self.assertIn("login_success", events)
        self.assertIn("dashboard_access", events)

    def test_authenticated_detail_preserves_existing_view(self):
        self.client.force_login(self.user)
        response = self.client.get(self.detail_url)
        self.assertContains(response, "Example Corp")
        self.assertEqual(self.audit.call_args.args[0], "event_detail_access")

    def test_explicit_development_bypass_allowed(self):
        with override_settings(DEBUG=True, DASHBOARD_REQUIRE_AUTH=False):
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertEqual(self.client.get(self.detail_url).status_code, 200)

    def test_production_runtime_setting_override_still_fails_closed(self):
        with override_settings(DEBUG=False, DASHBOARD_REQUIRE_AUTH=False):
            self.assertEqual(self.client.get("/").status_code, 403)
        self.mongo.assert_not_called()

    def test_post_to_readonly_views_remains_disallowed(self):
        for path in ("/", self.detail_url):
            self.assertEqual(self.client.post(path).status_code, 405)
        self.mongo.assert_not_called()

    def test_authenticated_head_works_and_response_is_not_cacheable(self):
        self.client.force_login(self.user)
        response = self.client.head("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])

    def test_logout_is_post_and_returns_to_login(self):
        self.login()
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        response = self.client.post(reverse("logout"))
        self.assertRedirects(response, "/login/", fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertIn("logout", [call.args[0] for call in self.audit.call_args_list])

    def test_logout_and_login_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post("/logout/").status_code, 403)
        self.assertEqual(csrf_client.post("/login/", {"username": self.user.username, "password": self.password}).status_code, 403)

    def test_failed_login_does_not_log_credentials_or_request_query(self):
        response = self.client.post("/login/?q=PRIVATEQUERY", {"username": "PRIVATEUSER", "password": "PRIVATEPASS"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "아이디 또는 비밀번호를 확인하세요.")
        self.assertNotContains(response, "PRIVATEPASS")
        self.assertIn("login_failure", [call.args[0] for call in self.audit.call_args_list])
        self.assertNotIn("PRIVATE", repr(self.audit.call_args_list))

    def test_auth_signal_registration_does_not_duplicate_logs(self):
        django_controls.connect_signals()
        django_controls.connect_signals()
        user_login_failed.send(sender=type(self), credentials={"password": "PRIVATE"})
        calls = [call for call in self.audit.call_args_list if call.args[0] == "login_failure"]
        self.assertEqual(len(calls), 1)

    def test_headers_are_applied_to_login_and_protected_redirect(self):
        for path in ("/", "/login/"):
            response = self.client.get(path)
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response["X-Frame-Options"], "DENY")
            self.assertEqual(response["Referrer-Policy"], "same-origin")

    def test_cookie_http_only_and_local_http_secure_flag(self):
        response = self.login()
        cookie = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertFalse(cookie["secure"])

    def test_secure_cookies_can_be_enabled_for_https(self):
        with override_settings(SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True):
            response = self.login()
            self.assertTrue(response.cookies[settings.SESSION_COOKIE_NAME]["secure"])

    def test_login_external_next_is_rejected_by_builtin_view(self):
        response = self.client.post("/login/?next=https://outside.example/", {"username": self.user.username,
                                                                           "password": self.password})
        self.assertEqual(response.url, "/")

    def test_logout_button_uses_csrf_protected_post(self):
        self.client.force_login(self.user)
        response = self.client.get("/")
        self.assertContains(response, '<form method="post" action="/logout/">')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')

    def test_audit_access_never_receives_filters_or_raw_metadata(self):
        self.client.force_login(self.user)
        self.client.get("/", {"q": "PRIVATEQUERY"})
        fields = self.audit.call_args.kwargs
        self.assertEqual(set(fields), {"category", "authenticated", "user_id", "document_id", "result", "status"})
        self.assertNotIn("PRIVATEQUERY", repr(fields))
        self.assertNotIn("company_url", repr(fields))

    def test_existing_legacy_secret_is_redacted_on_dashboard_surface(self):
        self.client.force_login(self.user)
        self.db.collections["leaked_data"].documents[0]["description"] = 'api_key="PRIVATE WORDS"'
        response = self.client.get(self.detail_url)
        self.assertNotContains(response, "PRIVATE WORDS")
        self.assertContains(response, "[redacted]")


class StartupControlsTests(SimpleTestCase):
    def run_settings(self, values, *, legacy=False):
        env = {**os.environ, "DJANGO_DEBUG": "True", "DASHBOARD_REQUIRE_AUTH": "True",
               "DJANGO_SECURE_COOKIES": "False", **values}
        script = """
import sys
from unittest.mock import patch
sys.path.insert(0, %r)
with patch('dotenv.load_dotenv'), patch('socket.socket.connect', side_effect=AssertionError('network')):
    try:
        from DjangoProject import settings
    except Exception as error:
        print(type(error).__name__ + ': ' + str(error))
        sys.exit(1)
    print(settings.DASHBOARD_REQUIRE_AUTH, settings.SESSION_COOKIE_SECURE)
""" % ("webapp" if legacy else "DjangoProject")
        return subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=30)

    def test_default_auth_true(self):
        result = self.run_settings({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True False")

    def test_production_weak_key_auth_bypass_and_wildcard_rejected_at_startup(self):
        base = {"DJANGO_DEBUG": "False", "DJANGO_SECRET_KEY": "s" * 60, "DJANGO_ALLOWED_HOSTS": "localhost"}
        for values in ({"DJANGO_SECRET_KEY": "weak"}, {"DASHBOARD_REQUIRE_AUTH": "False"}, {"DJANGO_ALLOWED_HOSTS": "*"}):
            result = self.run_settings({**base, **values})
            self.assertEqual(result.returncode, 1)
            self.assertIn("ImproperlyConfigured", result.stdout)
            self.assertNotIn("s" * 60, result.stdout)

    def test_production_valid_settings_and_https_cookie_opt_in(self):
        result = self.run_settings({"DJANGO_DEBUG": "False", "DJANGO_SECRET_KEY": "s" * 60,
                                   "DJANGO_ALLOWED_HOSTS": "localhost", "DJANGO_SECURE_COOKIES": "True"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True True")

    def test_malformed_auth_setting_rejected_at_startup(self):
        self.assertEqual(self.run_settings({"DASHBOARD_REQUIRE_AUTH": "invalid"}).returncode, 1)

    def test_legacy_webapp_cannot_bypass_canonical_controls(self):
        result = self.run_settings({}, legacy=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Legacy webapp entry point is disabled", result.stdout)
