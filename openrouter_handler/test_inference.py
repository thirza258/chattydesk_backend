import os
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from accounts.models import UserSettings
from openrouter_handler import client
from openrouter_handler.models import HistoryPrompt


def upstream_response(payload, status=200):
    return Mock(status_code=status, json=Mock(return_value=payload))


def completion(content="Hello", **choice_fields):
    return {
        "model": "test/model",
        "choices": [{"message": {"content": content}, **choice_fields}],
        "usage": {"total_tokens": 12},
    }


class InferenceClientTests(SimpleTestCase):
    @patch("openrouter_handler.client.requests.request")
    def test_errors_in_successful_http_responses_keep_their_status(self, request):
        for code in (401, 402, 429, 502):
            with self.subTest(code=code):
                request.return_value = upstream_response(
                    {"error": {"code": code, "message": "Provider could not complete"}}
                )
                with self.assertRaises(client.OpenRouterError) as raised:
                    client.chat_completion([], api_key="test-key")
                self.assertEqual(raised.exception.status_code, code)
                self.assertEqual(raised.exception.message, "Provider could not complete")

    @patch("openrouter_handler.client.requests.request")
    def test_missing_or_invalid_error_codes_become_bad_gateway(self, request):
        for code in (None, "provider_error", 200, 999):
            with self.subTest(code=code):
                request.return_value = upstream_response(
                    {"error": {"code": code, "message": "Provider unavailable"}}
                )
                with self.assertRaises(client.OpenRouterError) as raised:
                    client.chat_completion([], api_key="test-key")
                self.assertEqual(raised.exception.status_code, 502)
                self.assertEqual(raised.exception.message, "Provider unavailable")

    @patch("openrouter_handler.client.requests.request")
    def test_malformed_completions_are_reported_as_upstream_errors(self, request):
        for payload in ([], "invalid", {"choices": [None]}, {"choices": "invalid"}):
            with self.subTest(payload=payload):
                request.return_value = upstream_response(payload)
                with self.assertRaises(client.OpenRouterError) as raised:
                    client.chat_completion([], api_key="test-key")
                self.assertEqual(raised.exception.status_code, 502)

    @patch("openrouter_handler.client.requests.request")
    def test_empty_content_is_not_a_successful_completion(self, request):
        for content in (None, "", "  ", [], [{"type": "text", "text": None}]):
            with self.subTest(content=content):
                request.return_value = upstream_response(completion(content))
                with self.assertRaises(client.OpenRouterError) as raised:
                    client.chat_completion([], api_key="test-key")
                self.assertEqual(raised.exception.status_code, 502)
                self.assertIn("no text", raised.exception.message)

    def test_token_exhaustion_provides_an_actionable_error(self):
        with self.assertRaises(client.OpenRouterError) as raised:
            client.extract_text(completion(None, finish_reason="length"))
        self.assertIn("token limit", raised.exception.message)

    def test_text_parts_and_refusals_remain_visible(self):
        payload = completion([
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ])
        self.assertEqual(client.extract_text(payload), "Hello world")

        payload = completion(None)
        payload["choices"][0]["message"]["refusal"] = "I cannot help with that."
        self.assertEqual(client.extract_text(payload), "I cannot help with that.")


class InferenceEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(username="inference-test")
        self.client.force_authenticate(user=self.user)
        self.environment = patch.dict(os.environ, {"OPENROUTER_API_KEY": "server-key"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    @patch("openrouter_handler.client.requests.request")
    def test_empty_answer_is_not_saved_to_history(self, request):
        request.return_value = upstream_response(completion(None))
        response = self.client.post(
            "/api/v1/openrouter/chat/", {"message": "Hello"}, format="json"
        )
        self.assertEqual(response.status_code, 502)
        self.assertIn("no text", response.json()["message"])
        self.assertFalse(HistoryPrompt.objects.exists())

    @patch("openrouter_handler.client.requests.request")
    def test_embedded_key_error_uses_the_configured_fallback(self, request):
        settings = UserSettings.objects.create(user=self.user)
        settings.set_openrouter_key("own-key")
        settings.save()
        request.side_effect = [
            upstream_response({"error": {"code": 402, "message": "Out of credits"}}),
            upstream_response(completion("A valid answer")),
        ]

        response = self.client.post(
            "/api/v1/openrouter/chat/", {"message": "Hello"}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["response"], "A valid answer")
        self.assertTrue(response.json()["data"]["used_own_key"])
        self.assertEqual(HistoryPrompt.objects.get().response, "A valid answer")
        self.assertEqual(
            [call.kwargs["headers"]["Authorization"] for call in request.call_args_list],
            ["Bearer server-key", "Bearer own-key"],
        )

    @patch("openrouter_handler.client.requests.request")
    def test_compare_keeps_valid_answers_when_another_model_returns_no_text(self, request):
        def respond(method, url, **kwargs):
            if kwargs["json"]["model"] == "test/empty":
                return upstream_response(completion(None))
            return upstream_response(completion("A valid answer"))

        request.side_effect = respond
        response = self.client.post(
            "/api/v1/openrouter/compare/",
            {"message": "Hello", "models": ["test/empty", "test/valid"]},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        results = response.json()["data"]["results"]
        self.assertIn("no text", results[0]["error"])
        self.assertNotIn("response", results[0])
        self.assertIsNone(results[1]["error"])
        self.assertEqual(results[1]["response"], "A valid answer")
        self.assertFalse(HistoryPrompt.objects.exists())
