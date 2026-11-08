import logging
import os
from pathlib import Path
import subprocess
import sys

from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase

from project.settings import AuthenticationLinkFilter


class ConfigurationTests(SimpleTestCase):
    def test_startup_never_calls_dotenv_loader(self):
        command = "import environ; environ.Env.read_env=classmethod(lambda *a,**kw: (_ for _ in ()).throw(AssertionError('dotenv loader called'))); import django; django.setup(); import project.celeryapp"
        result = subprocess.run(
            [sys.executable, "-c", command],
            cwd=settings.BASE_DIR,
            env={**os.environ, "DJANGO_SETTINGS_MODULE": "project.settings"},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_authentication_tokens_redacted_from_raw_and_encoded_paths(self):
        for message in [
            "GET /teams/invitations/private-token/accept/",
            "GET /accounts/login/?next=%2Fteams%2Finvitations%2Fprivate-token%2Faccept%2F",
            "GET /accounts/signup/?invite=private-token",
            "GET /accounts/confirm-email/private-token/",
        ]:
            with self.subTest(message=message):
                record = logging.LogRecord(
                    "test", logging.INFO, __file__, 0, "%s", (message,), None
                )
                AuthenticationLinkFilter().filter(record)
                self.assertNotIn("private-token", record.getMessage())

    def test_runtime_versions_are_not_demo_and_no_periodic_tasks(self):
        from project.celeryapp import app

        self.assertFalse(settings.DEMO_MODE)
        self.assertFalse(settings.MCP_ENABLED)
        self.assertFalse(settings.SPEEDPY_BILLING_ENABLED)
        self.assertFalse(app.conf.beat_schedule)


class SurfaceTests(TestCase):
    def test_same_origin_login_keeps_csrf_protection(self):
        from allauth.account.models import EmailAddress
        from usermodel.models import User

        user = User.objects.create_user(
            email="csrf@example.com", password="csrf-test-long-password-893"
        )
        EmailAddress.objects.create(
            user=user, email=user.email, verified=True, primary=True
        )
        client = Client(enforce_csrf_checks=True)
        response = client.get("/accounts/login/")
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        token = client.cookies["csrftoken"].value
        payload = {
            "login": user.email,
            "password": "csrf-test-long-password-893",
            "csrfmiddlewaretoken": token,
        }
        self.assertEqual(
            client.post(
                "/accounts/login/", payload, HTTP_ORIGIN="https://foreign.example"
            ).status_code,
            403,
        )
        self.assertEqual(
            client.post(
                "/accounts/login/", payload, HTTP_ORIGIN="http://testserver"
            ).status_code,
            302,
        )

    def test_non_s1_routes_are_not_executable(self):
        for path in [
            "/demo/products/",
            "/api/v1/jobs/demo/",
            "/api/v1/teams/",
            "/o/authorize/",
            "/mcp/",
            "/accounts/github/login/",
            "/teams/00000000-0000-0000-0000-000000000001/webhooks/",
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path).status_code, 404)
