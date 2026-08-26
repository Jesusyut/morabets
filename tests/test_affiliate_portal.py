import unittest
from unittest.mock import patch

try:
    import app as app_module
except ModuleNotFoundError as exc:
    app_module = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(app_module is None, f"app dependencies unavailable: {IMPORT_ERROR}")
class AffiliatePortalTest(unittest.TestCase):
    def setUp(self):
        self.app = app_module.app
        self.app.config["TESTING"] = True
        self.app.config["SECRET_KEY"] = "test-secret"
        self.client = self.app.test_client()

    def test_logged_out_user_redirects_to_landing_page(self):
        response = self.client.get("/affiliate")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/?affiliate=required", response.headers["Location"])

    def test_non_affiliate_verified_user_gets_forbidden(self):
        with self.client.session_transaction() as session:
            session[app_module.CONTEXT_EDGE_SESSION_EMAIL_KEY] = "customer@example.com"

        with patch("supabase_backend.user_has_affiliate_access", return_value=False):
            response = self.client.get("/affiliate")

        self.assertEqual(response.status_code, 403)
        self.assertIn(b"not approved for partner portal access", response.data)

    def test_affiliate_verified_user_can_access_portal(self):
        with self.client.session_transaction() as session:
            session[app_module.CONTEXT_EDGE_SESSION_EMAIL_KEY] = "partner@example.com"

        with patch("supabase_backend.user_has_affiliate_access", return_value=True):
            response = self.client.get("/affiliate")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"FadeTheBooks Partner Portal", response.data)


if __name__ == "__main__":
    unittest.main()
