import hashlib
import hmac
import json
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import PaymentTransaction, UserSettings
from openrouter_handler import client

User = get_user_model()


def _fake_payload(model, content=None):
    return {
        "model": model,
        "choices": [
            {
                "message": {
                    "content": content or f"answer from {model}",
                }
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 32,
            "total_tokens": 42,
        },
    }


class PaymentTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="paddleuser", email="paddle@example.com", password="password123"
        )
        self.settings = UserSettings.objects.create(user=self.user)

    def test_payment_config_public(self):
        """Public config endpoint returns price amount and max free requests."""
        response = self.client.get("/api/v1/payments/config/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertEqual(data["price_amount"], "0.99")
        self.assertEqual(data["max_free_requests"], 50)
        self.assertEqual(data["currency"], "USD")

    def test_payment_status_requires_auth(self):
        """Status endpoint requires authentication."""
        response = self.client.get("/api/v1/payments/status/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_payment_status_authenticated(self):
        """Status endpoint returns user quota and plan."""
        self.client.force_authenticate(user=self.user)
        response = self.client.get("/api/v1/payments/status/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertFalse(data["is_unlimited"])
        self.assertEqual(data["paid_requests_count"], 0)
        self.assertEqual(data["remaining_paid_requests"], 50)
        self.assertEqual(data["plan"], "free")

    def test_verify_payment_unlocks_user(self):
        """Verifying transaction unlocks unlimited plan."""
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            "/api/v1/payments/verify/",
            {"transaction_id": "txn_test_12345"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertTrue(data["is_unlimited"])
        self.assertIsNone(data["remaining_paid_requests"])
        self.assertEqual(data["plan"], "unlimited")

        self.settings.refresh_from_db()
        self.assertTrue(self.settings.is_unlimited)
        self.assertEqual(self.settings.paddle_transaction_id, "txn_test_12345")
        self.assertTrue(PaymentTransaction.objects.filter(paddle_transaction_id="txn_test_12345").exists())

    def test_paddle_webhook_with_valid_signature(self):
        """Webhook with valid signature unlocks user account."""
        secret = "test_webhook_secret_123"
        payload_dict = {
            "event_type": "transaction.completed",
            "data": {
                "id": "txn_webhook_999",
                "customer_id": "ctm_123",
                "custom_data": {"user_id": self.user.id},
                "details": {"totals": {"total": "0.99"}},
                "currency_code": "USD",
            },
        }
        body_bytes = json.dumps(payload_dict).encode("utf-8")
        ts = "1700000000"
        signed = f"{ts}:".encode("utf-8") + body_bytes
        h1 = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        sig_header = f"ts={ts};h1={h1}"

        with patch.dict(os.environ, {"PADDLE_WEBHOOK_SECRET": secret}):
            response = self.client.post(
                "/api/v1/payments/webhook/",
                data=body_bytes,
                content_type="application/json",
                HTTP_PADDLE_SIGNATURE=sig_header,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.settings.refresh_from_db()
        self.assertTrue(self.settings.is_unlimited)
        self.assertEqual(self.settings.paddle_transaction_id, "txn_webhook_999")


class QuotaLimitTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="quotauser", email="quota@example.com", password="password123"
        )
        self.settings = UserSettings.objects.create(user=self.user)
        self.client.force_authenticate(user=self.user)

    @patch("openrouter_handler.views.client.chat_completion")
    def test_free_model_always_allowed(self, mock_chat):
        """Free models (ending in :free) are allowed even if 50 paid requests reached."""
        self.settings.paid_requests_count = 50
        self.settings.save()

        mock_chat.return_value = _fake_payload("meta-llama/llama-3.2-1b-instruct:free")

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-server-key"}):
            response = self.client.post(
                "/api/v1/openrouter/chat/",
                {"message": "Hello", "model": "meta-llama/llama-3.2-1b-instruct:free"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Paid requests count should not increase on free models
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.paid_requests_count, 50)

    @patch("openrouter_handler.views.client.chat_completion")
    def test_paid_model_under_limit_increments_count(self, mock_chat):
        """Paid model request under 50 limit succeeds and increments counter."""
        self.settings.paid_requests_count = 10
        self.settings.save()

        mock_chat.return_value = _fake_payload("openai/gpt-4o-mini")

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-server-key"}):
            response = self.client.post(
                "/api/v1/openrouter/chat/",
                {"message": "Hello", "model": "openai/gpt-4o-mini"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.settings.refresh_from_db()
        self.assertEqual(self.settings.paid_requests_count, 11)
        quota = response.json()["data"]["quota"]
        self.assertEqual(quota["paid_requests_count"], 11)
        self.assertEqual(quota["remaining_paid_requests"], 39)

    @patch("openrouter_handler.views.client.chat_completion")
    def test_paid_model_at_limit_blocked_with_402(self, mock_chat):
        """Paid model request at 50 limit is blocked with 402."""
        self.settings.paid_requests_count = 50
        self.settings.save()

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-server-key"}):
            response = self.client.post(
                "/api/v1/openrouter/chat/",
                {"message": "Hello", "model": "openai/gpt-4o-mini"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertIn("Free limit reached", response.json()["message"])
        mock_chat.assert_not_called()

    @patch("openrouter_handler.views.client.chat_completion")
    def test_unlimited_user_bypasses_limit(self, mock_chat):
        """User with is_unlimited=True can make unlimited paid model requests."""
        self.settings.paid_requests_count = 50
        self.settings.is_unlimited = True
        self.settings.save()

        mock_chat.return_value = _fake_payload("openai/gpt-4o-mini")

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-server-key"}):
            response = self.client.post(
                "/api/v1/openrouter/chat/",
                {"message": "Hello", "model": "openai/gpt-4o-mini"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        quota = response.json()["data"]["quota"]
        self.assertTrue(quota["is_unlimited"])
        self.assertIsNone(quota["remaining_paid_requests"])
