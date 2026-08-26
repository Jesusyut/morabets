import unittest
from unittest.mock import patch

import supabase_backend


class ProductAccessRolesTest(unittest.TestCase):
    def test_affiliate_role_gets_product_access_without_subscription(self):
        with patch("supabase_backend.read_user_role_by_email", return_value="affiliate"), \
             patch("supabase_backend.read_active_subscription_status_by_email") as read_subscription:
            self.assertTrue(supabase_backend.user_has_product_access("partner@example.com"))
            read_subscription.assert_not_called()

    def test_admin_role_gets_product_access_without_subscription(self):
        with patch("supabase_backend.read_user_role_by_email", return_value="admin"), \
             patch("supabase_backend.read_active_subscription_status_by_email") as read_subscription:
            self.assertTrue(supabase_backend.user_has_product_access("admin@example.com"))
            read_subscription.assert_not_called()

    def test_customer_role_still_requires_active_subscription(self):
        with patch("supabase_backend.read_user_role_by_email", return_value="customer"), \
             patch("supabase_backend.read_active_subscription_status_by_email", return_value={"active": False}):
            self.assertFalse(supabase_backend.user_has_product_access("customer@example.com"))

    def test_active_customer_subscription_still_gets_product_access(self):
        with patch("supabase_backend.read_user_role_by_email", return_value="customer"), \
             patch("supabase_backend.read_active_subscription_status_by_email", return_value={"active": True}):
            self.assertTrue(supabase_backend.user_has_product_access("customer@example.com"))


if __name__ == "__main__":
    unittest.main()
