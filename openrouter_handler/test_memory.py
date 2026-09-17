import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from openrouter_handler.models import ConversationMemory, HistoryPrompt
from openrouter_handler.test_inference import completion


class ConversationMemoryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="memory-user")
        self.other_user = get_user_model().objects.create_user(username="another-user")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.chat_url = "/api/v1/openrouter/chat/"
        self.memory_url = "/api/v1/openrouter/conversations/thread-one/memory/"
        self.environment = patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.completion = patch("openrouter_handler.views.client.chat_completion", return_value=completion("Answer"))
        self.mock_completion = self.completion.start()
        self.addCleanup(self.completion.stop)

    def turn(self, prompt, response="Previous answer", user=None, conversation_id="thread-one"):
        return HistoryPrompt.objects.create(
            user=user or self.user, prompt=prompt, response=response,
            conversation_id=conversation_id, model_name="first/model",
        )

    def send(self, **extra):
        return self.client.post(self.chat_url, {
            "message": "What did I say?", "conversation_id": "thread-one", **extra,
        }, format="json")

    def test_memory_replays_ordered_turns_when_models_change(self):
        self.turn("My name is Ada", "Hello Ada")
        self.turn("I prefer short answers", "Understood")
        response = self.send(model="different/model")
        self.assertEqual(response.status_code, 200)
        sent = self.mock_completion.call_args.args[0]
        self.assertEqual(sent[1:], [
            {"role": "user", "content": "My name is Ada"},
            {"role": "assistant", "content": "Hello Ada"},
            {"role": "user", "content": "I prefer short answers"},
            {"role": "assistant", "content": "Understood"},
            {"role": "user", "content": "What did I say?"},
        ])
        self.assertEqual(response.json()["data"]["memory"]["used_turns"], 2)

    def test_memory_is_scoped_to_account_and_conversation(self):
        self.turn("Other person's private message", user=self.other_user)
        self.turn("Another thread", conversation_id="thread-two")
        self.turn("Current thread")
        self.send()
        sent = str(self.mock_completion.call_args.args[0])
        self.assertIn("Current thread", sent)
        self.assertNotIn("private message", sent)
        self.assertNotIn("Another thread", sent)

    def test_limit_retains_the_most_recent_complete_turns(self):
        for i in range(4):
            self.turn(f"Turn {i}")
        response = self.send(history_turns=2)
        sent = self.mock_completion.call_args.args[0]
        self.assertEqual([message["content"] for message in sent if message["role"] == "user"], ["Turn 2", "Turn 3", "What did I say?"])
        self.assertTrue(response.json()["data"]["memory"]["trimmed"])

    @patch("openrouter_handler.memory.HISTORY_CHARACTER_LIMIT", 100)
    def test_large_history_is_bounded_without_splitting_turns(self):
        self.turn("A" * 80, "B" * 80)
        self.turn("Recent", "Reply")
        response = self.send(system_prompt="Be concise.")
        sent = self.mock_completion.call_args.args[0]
        self.assertLessEqual(sum(len(message["content"]) for message in sent), 100)
        self.assertEqual(response.json()["data"]["memory"]["used_turns"], 1)
        self.assertTrue(response.json()["data"]["memory"]["trimmed"])
        self.assertNotIn("A" * 80, str(sent))

    def test_disabled_memory_uses_only_the_current_prompt_and_is_saved(self):
        self.turn("Do not recall this")
        response = self.send(use_history=False, history_turns=5)
        self.assertEqual(len(self.mock_completion.call_args.args[0]), 2)
        self.assertEqual(response.json()["data"]["memory"]["used_turns"], 0)
        saved = ConversationMemory.objects.get(user=self.user, conversation_id="thread-one")
        self.assertFalse(saved.enabled)
        self.assertEqual(saved.history_turns, 5)
        self.send()
        self.assertEqual(len(self.mock_completion.call_args.args[0]), 2)

    def test_preferences_survive_another_request(self):
        self.turn("A previous message")
        response = self.client.patch(self.memory_url, {"enabled": False, "history_turns": 20}, format="json")
        self.assertEqual(response.status_code, 200)
        loaded = self.client.get(self.memory_url).json()["data"]
        self.assertFalse(loaded["enabled"])
        self.assertEqual(loaded["history_turns"], 20)
        self.send()
        self.assertEqual(len(self.mock_completion.call_args.args[0]), 2)

    def test_reset_excludes_old_context_without_deleting_history(self):
        self.turn("Forget this fact")
        response = self.client.patch(self.memory_url, {"reset": True}, format="json")
        self.assertEqual(response.json()["data"]["available_turns"], 0)
        self.assertEqual(HistoryPrompt.objects.count(), 1)
        self.send()
        self.assertNotIn("Forget this fact", str(self.mock_completion.call_args.args[0]))
        self.assertEqual(HistoryPrompt.objects.count(), 2)
        self.send(message="Continue after reset")
        self.assertIn("What did I say?", str(self.mock_completion.call_args.args[0]))
        self.assertNotIn("Forget this fact", str(self.mock_completion.call_args.args[0]))

    def test_another_account_cannot_view_or_reset_memory(self):
        self.turn("Other user's message", user=self.other_user)
        self.assertEqual(self.client.get(self.memory_url).status_code, 404)
        self.assertEqual(self.client.patch(self.memory_url, {"reset": True}, format="json").status_code, 404)
        self.assertFalse(ConversationMemory.objects.exists())

    def test_invalid_limits_are_rejected_before_inference(self):
        self.turn("Earlier message")
        for limit in (-1, 0, 51, "invalid", 1.5, True):
            with self.subTest(limit=limit):
                self.assertEqual(self.send(history_turns=limit).status_code, 400)
                response = self.client.patch(self.memory_url, {"history_turns": limit}, format="json")
                self.assertEqual(response.status_code, 400)
        self.mock_completion.assert_not_called()

    def test_new_thread_starts_without_any_previous_context(self):
        self.turn("Existing thread")
        response = self.client.post(self.chat_url, {"message": "A fresh start"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["data"]["conversation_id"], "thread-one")
        self.assertEqual(response.json()["data"]["memory"]["used_turns"], 0)
        self.assertEqual(len(self.mock_completion.call_args.args[0]), 2)
