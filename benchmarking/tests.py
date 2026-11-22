import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from benchmarking.models import (
    AIModel,
    BenchmarkDataset,
    BenchmarkDatasetVersion,
    BenchmarkExecution,
    BenchmarkExperiment,
    BenchmarkTestCase,
    ComparisonSession,
    EvaluationResult,
    EvaluationRun,
    ExperimentSummary,
    LeaderboardSnapshot,
)
from benchmarking.providers.base import ProviderRequest
from benchmarking.providers.mock import MockProvider
from benchmarking.providers.registry import get_provider
from benchmarking.services.analytics import (
    AnalyticsService,
    calculate_percentile,
    calculate_statistics,
)
from benchmarking.services.cost import CostIntelligenceService
from benchmarking.services.dataset_importer import (
    export_dataset_to_jsonl,
    import_dataset_from_jsonl,
    seed_builtin_datasets,
)
from benchmarking.services.evaluator import EvaluationEngine
from benchmarking.services.leaderboard import LeaderboardService
from benchmarking.services.orchestrator import BenchmarkOrchestrator

User = get_user_model()


class BenchmarkingTestSuite(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testevaluator", email="eval@chattydesk.com", password="password123"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    # 1. Dataset Seeding & Versioning
    def test_seed_builtin_datasets(self):
        count = seed_builtin_datasets()
        self.assertGreaterEqual(count, 5)
        self.assertTrue(BenchmarkDataset.objects.filter(is_builtin=True).exists())

        # Check version and test cases
        math_ds = BenchmarkDataset.objects.filter(category="math", is_builtin=True).first()
        self.assertIsNotNone(math_ds)
        self.assertIsNotNone(math_ds.latest_version)
        self.assertGreater(math_ds.latest_version.test_cases.count(), 0)

    def test_dataset_jsonl_import_and_export(self):
        jsonl_content = (
            '{"id": "test-1", "prompt": "What is 2+2?", "answer": "4", "evaluation_method": "numeric_exact_match"}\n'
            '{"id": "test-2", "prompt": "What is 3*3?", "answer": "9", "evaluation_method": "numeric_exact_match"}\n'
        )
        dataset, version = import_dataset_from_jsonl(
            self.user, "Custom Math Suite", "Testing math", "math", jsonl_content
        )
        self.assertEqual(dataset.name, "Custom Math Suite")
        self.assertEqual(version.test_case_count, 2)
        self.assertEqual(version.test_cases.count(), 2)

        exported = export_dataset_to_jsonl(version)
        self.assertIn("What is 2+2?", exported)
        self.assertIn("What is 3*3?", exported)

    # 2. Evaluation Engine
    def test_deterministic_exact_match(self):
        res = EvaluationEngine.exact_match(
            "Paris", {"answer": "Paris"}, {"case_sensitive": False}
        )
        self.assertEqual(res.score, 1.0)
        self.assertTrue(res.passed)

        res_fail = EvaluationEngine.exact_match(
            "London", {"answer": "Paris"}, {"case_sensitive": False}
        )
        self.assertEqual(res_fail.score, 0.0)
        self.assertFalse(res_fail.passed)

    def test_numeric_exact_match(self):
        # Handles currency, symbols, percentages, conversational text
        res = EvaluationEngine.numeric_exact_match(
            "The final price after discount is $60.00.",
            {"answer": "60"},
            {"tolerance": 1e-4},
        )
        self.assertEqual(res.score, 1.0)
        self.assertTrue(res.passed)

        res_fail = EvaluationEngine.numeric_exact_match(
            "The price is $55.00.", {"answer": "60"}, {"tolerance": 1e-4}
        )
        self.assertEqual(res_fail.score, 0.0)
        self.assertFalse(res_fail.passed)

    def test_json_schema_validation(self):
        valid_json_text = '```json\n{"name": "Alice", "age": 30, "city": "Seattle", "occupation": "Dev"}\n```'
        res = EvaluationEngine.json_schema_validation(
            valid_json_text,
            {"keys": ["name", "age", "city", "occupation"]},
            {},
        )
        self.assertEqual(res.score, 1.0)
        self.assertTrue(res.passed)

        invalid_json = "Hello, this is not JSON at all."
        res_bad = EvaluationEngine.json_schema_validation(invalid_json, {"keys": ["name"]}, {})
        self.assertEqual(res_bad.score, 0.0)
        self.assertFalse(res_bad.passed)

    def test_token_f1_and_rouge(self):
        ref = "Quicksort divides an array around a pivot element."
        cand = "Quicksort algorithm partitions an array using a pivot element."
        res = EvaluationEngine.token_f1(cand, {"answer": ref}, {"threshold": 0.4})
        self.assertGreater(res.score, 0.5)
        self.assertTrue(res.passed)

        rouge_res = EvaluationEngine.rouge_scores(cand, {"answer": ref}, {})
        self.assertEqual(len(rouge_res), 2)
        self.assertGreater(rouge_res[0].score, 0.4)

    def test_llm_judge_rubric(self):
        outputs = EvaluationEngine.llm_judge(
            "Because quicksort partitions the elements, it runs in average O(n log n).",
            {"answer": "Quicksort O(n log n)"},
            {},
            "Explain quicksort complexity.",
        )
        self.assertTrue(any(o.metric_name == "judge_composite" for o in outputs))
        self.assertTrue(any(o.metric_name == "judge_correctness" for o in outputs))

    # 3. Provider Abstraction
    def test_mock_provider_execution(self):
        mock_p = get_provider("mock/gpt-4o-mini")
        resp = mock_p.generate(
            ProviderRequest(prompt="What is 2+2?", model="mock/gpt-4o-mini")
        )
        self.assertEqual(resp.text, "4")
        self.assertEqual(resp.model, "mock/gpt-4o-mini")
        self.assertIsNotNone(resp.estimated_cost)

    # 4. Analytics & Statistical Calculations
    def test_percentile_and_statistics(self):
        vals = [100.0, 200.0, 300.0, 400.0, 500.0]
        stats = calculate_statistics(vals)
        self.assertEqual(stats["count"], 5)
        self.assertEqual(stats["mean"], 300.0)
        self.assertEqual(stats["median"], 300.0)
        self.assertEqual(stats["min"], 100.0)
        self.assertEqual(stats["max"], 500.0)

    # 5. Benchmark Orchestration & End-to-End Execution
    def test_benchmark_experiment_lifecycle(self):
        seed_builtin_datasets()
        ds = BenchmarkDataset.objects.filter(category="math", is_builtin=True).first()
        version = ds.latest_version

        # Create experiment
        create_res = self.client.post(
            "/api/v1/benchmarks/",
            {
                "name": "Math Evaluation Test",
                "dataset_version_id": str(version.id),
                "models": ["mock/fast-model"],
                "configuration": {"repetitions": 1, "concurrency": 2},
                "auto_start": False,
            },
            format="json",
        )
        self.assertEqual(create_res.status_code, status.HTTP_202_ACCEPTED)
        exp_id = create_res.data["data"]["experiment_id"]

        # Run synchronously via orchestrator
        BenchmarkOrchestrator._run_experiment_lifecycle(exp_id)

        exp = BenchmarkExperiment.objects.get(id=exp_id)
        self.assertIn(exp.status, ("completed", "partially_completed"))
        self.assertGreater(exp.completed_cases, 0)
        self.assertTrue(exp.executions.filter(status="success").exists())
        self.assertTrue(EvaluationResult.objects.filter(execution__experiment=exp).exists())
        self.assertTrue(ExperimentSummary.objects.filter(experiment=exp).exists())

        # Test Results API
        results_res = self.client.get(f"/api/v1/benchmarks/{exp_id}/results/")
        self.assertEqual(results_res.status_code, status.HTTP_200_OK)
        self.assertGreater(len(results_res.data["data"]["executions"]), 0)

        # Test Export API (JSON and CSV)
        export_json = self.client.get(f"/api/v1/benchmarks/{exp_id}/export/?export_format=json")
        self.assertEqual(export_json.status_code, status.HTTP_200_OK)

        export_csv = self.client.get(f"/api/v1/benchmarks/{exp_id}/export/?export_format=csv")
        self.assertEqual(export_csv.status_code, status.HTTP_200_OK)
        self.assertIn("case_id", export_csv.content.decode("utf-8"))

    # 6. Leaderboards & Composite Scoring
    def test_leaderboard_and_recommendation(self):
        score = LeaderboardService.compute_composite_score(
            quality_score=90.0,
            latency_p50_ms=800.0,
            tokens_per_second=65.0,
            cost_per_1m=0.50,
            success_rate=100.0,
        )
        self.assertGreater(score, 75.0)

        # API check
        res = self.client.get("/api/v1/leaderboards/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        rec_res = self.client.get("/api/v1/leaderboards/recommend/")
        self.assertEqual(rec_res.status_code, status.HTTP_200_OK)

    # 7. Cost Intelligence Workload Projection
    def test_cost_intelligence_projection(self):
        projections = CostIntelligenceService.project_workload_costs(
            daily_requests=5000,
            avg_input_tokens=1000,
            avg_output_tokens=500,
            model_ids=["mock/gpt-4o-mini"],
        )
        self.assertEqual(len(projections), 1)
        self.assertIn("monthly_cost", projections[0])
        self.assertIn("cost_per_1k_requests", projections[0])

        # API check
        api_res = self.client.get(
            "/api/v1/analytics/cost/?daily_requests=5000&avg_input_tokens=1000&avg_output_tokens=500"
        )
        self.assertEqual(api_res.status_code, status.HTTP_200_OK)
        self.assertIn("projections", api_res.data["data"])

    # 8. Persistent Comparisons (Module A)
    def test_persistent_comparison_save_and_retrieve(self):
        save_res = self.client.post(
            "/api/v1/comparisons/",
            {
                "prompt": "Compare quicksort vs mergesort",
                "models": ["mock/model-a", "mock/model-b"],
                "results": [
                    {"model": "mock/model-a", "response": "Quicksort in place", "duration_ms": 120},
                    {"model": "mock/model-b", "response": "Mergesort stable", "duration_ms": 150},
                ],
            },
            format="json",
        )
        self.assertEqual(save_res.status_code, status.HTTP_201_CREATED)
        session_id = save_res.data["data"]["id"]

        list_res = self.client.get("/api/v1/comparisons/")
        self.assertEqual(list_res.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(list_res.data["data"]["count"], 1)

        detail_res = self.client.get(f"/api/v1/comparisons/{session_id}/")
        self.assertEqual(detail_res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(detail_res.data["data"]["results"]), 2)
