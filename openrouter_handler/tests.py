import os
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import UserSettings
from openrouter_handler import client
from openrouter_handler.models import HistoryPrompt

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


class CompareModelsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com", password="password123"
        )
        self.url = "/api/v1/openrouter/compare/"

    def test_auth_required(self):
        """1. auth — anonymous POST -> 401."""
        response = self.client.post(
            self.url,
            {"message": "Hello", "models": ["model/a", "model/b"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_message_required(self):
        """2. message required — missing/blank message -> 400."""
        self.client.force_authenticate(user=self.user)
        for blank_message in [None, "", "   "]:
            data = {"models": ["model/a", "model/b"]}
            if blank_message is not None:
                data["message"] = blank_message
            response = self.client.post(self.url, data, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.json()["status"], status.HTTP_400_BAD_REQUEST)
            self.assertIn("'message' field is required", response.json()["message"])

    def test_models_arity(self):
        """3. models arity — missing, one id, six ids, non-list, blank entry -> 400."""
        self.client.force_authenticate(user=self.user)
        invalid_models = [
            None,
            "not-a-list",
            [],
            ["only-one"],
            ["m1", "m2", "m3", "m4", "m5", "m6"],
            ["m1", ""],
            ["m1", "   "],
            ["m1", 123],
        ]
        for models_val in invalid_models:
            data = {"message": "Test prompt"}
            if models_val is not None:
                data["models"] = models_val
            response = self.client.post(self.url, data, format="json")
            self.assertEqual(
                response.status_code,
                status.HTTP_400_BAD_REQUEST,
                f"Expected 400 for models={models_val!r}",
            )
            self.assertEqual(response.json()["status"], status.HTTP_400_BAD_REQUEST)

    @patch("openrouter_handler.views.client.chat_completion")
    def test_happy_path(self, mock_chat):
        """4. happy path — 3 models -> 200; results in request order; payload valid."""
        self.client.force_authenticate(user=self.user)
        models = ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4.5", "meta/llama-3"]

        def fake_completion(messages, model=None, **kwargs):
            return _fake_payload(model)

        mock_chat.side_effect = fake_completion

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-server-key"}):
            response = self.client.post(
                self.url,
                {
                    "message": "Explain quicksort",
                    "models": models,
                    "temperature": 0.7,
                    "max_tokens": 100,
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["status"], status.HTTP_200_OK)
        data = body["data"]
        self.assertEqual(data["prompt"], "Explain quicksort")
        results = data["results"]
        self.assertEqual(len(results), 3)

        for i, m in enumerate(models):
            self.assertEqual(results[i]["model"], m)
            self.assertEqual(results[i]["response"], f"answer from {m}")
            self.assertEqual(results[i]["usage"]["total_tokens"], 42)
            self.assertFalse(results[i]["used_own_key"])
            self.assertIsNone(results[i]["error"])
            self.assertIsInstance(results[i]["duration_ms"], int)

    @patch("openrouter_handler.views.client.chat_completion")
    def test_slot_isolation(self, mock_chat):
        """5. slot isolation — one model fails, others succeed -> 200 with mixed results."""
        self.client.force_authenticate(user=self.user)
        models = ["openai/gpt-4o-mini", "bad/broken-model", "anthropic/claude-sonnet-4.5"]

        def fake_completion(messages, model=None, **kwargs):
            if model == "bad/broken-model":
                raise client.OpenRouterError("Payment Required", status_code=402)
            return _fake_payload(model)

        mock_chat.side_effect = fake_completion

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-server-key"}):
            response = self.client.post(
                self.url,
                {"message": "Hello", "models": models},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()["data"]["results"]
        self.assertEqual(len(results), 3)

        # Slot 0 succeeded
        self.assertEqual(results[0]["model"], "openai/gpt-4o-mini")
        self.assertEqual(results[0]["response"], "answer from openai/gpt-4o-mini")
        self.assertIsNone(results[0]["error"])

        # Slot 1 failed
        self.assertEqual(results[1]["model"], "bad/broken-model")
        self.assertEqual(results[1]["error"], "OpenRouter error: Payment Required")
        self.assertNotIn("response", results[1])
        self.assertIsInstance(results[1]["duration_ms"], int)

        # Slot 2 succeeded
        self.assertEqual(results[2]["model"], "anthropic/claude-sonnet-4.5")
        self.assertEqual(results[2]["response"], "answer from anthropic/claude-sonnet-4.5")
        self.assertIsNone(results[2]["error"])

    @patch("openrouter_handler.views.client.chat_completion")
    def test_all_failed(self, mock_chat):
        """6. all failed — every id raises -> status maps from the first failure."""
        self.client.force_authenticate(user=self.user)
        models = ["model/a", "model/b"]

        # Case A: first error is 402
        mock_chat.side_effect = client.OpenRouterError("Out of credit", status_code=402)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-server-key"}):
            response = self.client.post(
                self.url,
                {"message": "Hello", "models": models},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(response.json()["status"], status.HTTP_402_PAYMENT_REQUIRED)
        self.assertEqual(response.json()["message"], "OpenRouter error: Out of credit")

        # Case B: first error is 401 -> maps to 502 Bad Gateway
        mock_chat.side_effect = client.OpenRouterError("Invalid key", status_code=401)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-server-key"}):
            response = self.client.post(
                self.url,
                {"message": "Hello", "models": models},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(response.json()["status"], status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(response.json()["message"], "OpenRouter error: Invalid key")

    @patch("openrouter_handler.views.client.chat_completion")
    def test_no_history(self, mock_chat):
        """7. no history — after a successful compare, HistoryPrompt has 0 records."""
        self.client.force_authenticate(user=self.user)
        mock_chat.side_effect = lambda messages, model=None, **kwargs: _fake_payload(model)

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-server-key"}):
            response = self.client.post(
                self.url,
                {"message": "Hello", "models": ["model/a", "model/b"]},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(HistoryPrompt.objects.count(), 0)

    @patch("openrouter_handler.views.client.chat_completion")
    def test_key_fallback_per_slot(self, mock_chat):
        """8. key fallback per slot — server key fails, user key succeeds -> used_own_key: true."""
        self.client.force_authenticate(user=self.user)
        settings = UserSettings.objects.create(user=self.user)
        settings.set_openrouter_key("sk-user-key")
        settings.save()

        def fake_completion(messages, api_key=None, model=None, **kwargs):
            if api_key == "sk-user-key":
                return _fake_payload(model)
            if api_key == "sk-server-key":
                raise client.OpenRouterError("Out of credit", status_code=402)
            raise client.OpenRouterError("Unknown key", status_code=401)

        mock_chat.side_effect = fake_completion

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-server-key"}):
            response = self.client.post(
                self.url,
                {"message": "Hello", "models": ["model/a", "model/b"]},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()["data"]["results"]
        self.assertTrue(results[0]["used_own_key"])
        self.assertTrue(results[1]["used_own_key"])

    @patch("openrouter_handler.views.client.chat_completion")
    def test_parallelism(self, mock_chat):
        """9. parallelism — 3 models sleeping 0.2s finish faster than sequential sum."""
        self.client.force_authenticate(user=self.user)

        def slow_completion(messages, model=None, **kwargs):
            time.sleep(0.2)
            return _fake_payload(model)

        mock_chat.side_effect = slow_completion

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-server-key"}):
            start = time.perf_counter()
            response = self.client.post(
                self.url,
                {"message": "Hello", "models": ["m1", "m2", "m3"]},
                format="json",
            )
            elapsed = time.perf_counter() - start

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Sequential would take >= 0.6s; parallel should comfortably finish under 0.55s
        self.assertLess(elapsed, 0.55)
