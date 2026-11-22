import csv
import io
import json
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from benchmarking.models import (
    AIModel,
    BenchmarkDataset,
    BenchmarkDatasetVersion,
    BenchmarkExecution,
    BenchmarkExperiment,
    BenchmarkTestCase,
    ComparisonResultRecord,
    ComparisonSession,
    EvaluationResult,
    EvaluationRun,
    ExperimentModel,
    HumanEvaluation,
    LeaderboardSnapshot,
    ModelMetadataSnapshot,
)
from benchmarking.serializers import (
    AIModelSerializer,
    BenchmarkDatasetSerializer,
    BenchmarkDatasetVersionSerializer,
    BenchmarkExecutionSerializer,
    BenchmarkExperimentDetailSerializer,
    BenchmarkExperimentListSerializer,
    ComparisonSessionSerializer,
    EvaluationResultSerializer,
    LeaderboardSnapshotSerializer,
    ModelMetadataSnapshotSerializer,
)
from benchmarking.services.analytics import AnalyticsService
from benchmarking.services.cost import CostIntelligenceService
from benchmarking.services.dataset_importer import (
    compute_dataset_checksum,
    export_dataset_to_jsonl,
    import_dataset_from_jsonl,
    seed_builtin_datasets,
)
from benchmarking.services.leaderboard import LeaderboardService
from benchmarking.services.orchestrator import BenchmarkOrchestrator
from chattydesk.envelope import envelope as _envelope, error as _error
from openrouter_handler import client

# ---------------------------------------------------------------------------
# Model Registry Views
# ---------------------------------------------------------------------------


class ModelListView(APIView):
    """GET /api/v1/models/ — Normalized registry of models."""

    permission_classes = [AllowAny]

    def get(self, request):
        provider = request.query_params.get("provider")
        search = (request.query_params.get("search") or "").strip().lower()
        free_only = request.query_params.get("free") in ("true", "1")
        category = request.query_params.get("category")

        # Sync or ensure AIModel records from OpenRouter catalogue
        try:
            catalogue_models = client.list_models()
        except Exception:
            catalogue_models = []

        # Update or create AIModel cache
        if catalogue_models and AIModel.objects.count() == 0:
            models_to_create = []
            for m in catalogue_models:
                pricing = m.get("pricing") or {}
                models_to_create.append(
                    AIModel(
                        model_identifier=m["id"],
                        display_name=m.get("name") or m["id"],
                        provider_id=m.get("provider") or "openrouter",
                        description=m.get("description") or "",
                        context_length=m.get("context_length"),
                        max_output_tokens=m.get("max_completion_tokens"),
                        capabilities=m.get("supported_parameters") or [],
                        is_free=client.is_free_model(m),
                        input_price_per_token=Decimal(str(pricing.get("prompt") or 0)) if pricing.get("prompt") else None,
                        output_price_per_token=Decimal(str(pricing.get("completion") or 0)) if pricing.get("completion") else None,
                    )
                )
            AIModel.objects.bulk_create(models_to_create, ignore_conflicts=True)

        qs = AIModel.objects.filter(is_active=True)
        if provider:
            qs = qs.filter(provider_id=provider)
        if free_only:
            qs = qs.filter(is_free=True)
        if search:
            qs = qs.filter(
                Q(model_identifier__icontains=search)
                | Q(display_name__icontains=search)
                | Q(description__icontains=search)
            )

        total = qs.count()
        limit = min(int(request.query_params.get("limit", 100)), 500)
        offset = max(int(request.query_params.get("offset", 0)), 0)
        items = qs[offset : offset + limit]

        return _envelope(
            {
                "count": total,
                "limit": limit,
                "offset": offset,
                "results": AIModelSerializer(items, many=True).data,
            }
        )


class ModelDetailView(APIView):
    """GET /api/v1/models/{id}/ — Model detail and capabilities."""

    permission_classes = [AllowAny]

    def get(self, request, model_id):
        # Decode slash if encoded
        model_id = model_id.replace("%2F", "/").replace("%2f", "/")
        model_obj = AIModel.objects.filter(model_identifier=model_id).first()
        if not model_obj:
            # Fall back to finding in live catalogue
            try:
                models = client.list_models()
                m = next((item for item in models if item["id"] == model_id), None)
                if m:
                    pricing = m.get("pricing") or {}
                    model_obj = AIModel.objects.create(
                        model_identifier=m["id"],
                        display_name=m.get("name") or m["id"],
                        provider_id=m.get("provider") or "openrouter",
                        description=m.get("description") or "",
                        context_length=m.get("context_length"),
                        is_free=client.is_free_model(m),
                        input_price_per_token=Decimal(str(pricing.get("prompt") or 0)) if pricing.get("prompt") else None,
                        output_price_per_token=Decimal(str(pricing.get("completion") or 0)) if pricing.get("completion") else None,
                    )
            except Exception:
                pass

        if not model_obj:
            return _error(f"Model '{model_id}' not found.", status.HTTP_404_NOT_FOUND)

        return _envelope(AIModelSerializer(model_obj).data)


class ModelHistoryView(APIView):
    """GET /api/v1/models/{id}/history/ — Historical pricing & metadata snapshots."""

    permission_classes = [AllowAny]

    def get(self, request, model_id):
        model_id = model_id.replace("%2F", "/").replace("%2f", "/")
        snapshots = ModelMetadataSnapshot.objects.filter(model_identifier=model_id).order_by("-observed_at")[:50]
        return _envelope(ModelMetadataSnapshotSerializer(snapshots, many=True).data)


class ModelRefreshView(APIView):
    """POST /api/v1/models/refresh/ — Refresh catalogue from providers and record snapshots."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            models = client.list_models(force_refresh=True)
            created_or_updated = 0
            for m in models:
                pricing = m.get("pricing") or {}
                prompt_rate = Decimal(str(pricing.get("prompt") or 0)) if pricing.get("prompt") else None
                comp_rate = Decimal(str(pricing.get("completion") or 0)) if pricing.get("completion") else None

                ai_model, _ = AIModel.objects.update_or_create(
                    model_identifier=m["id"],
                    defaults={
                        "display_name": m.get("name") or m["id"],
                        "provider_id": m.get("provider") or "openrouter",
                        "description": m.get("description") or "",
                        "context_length": m.get("context_length"),
                        "max_output_tokens": m.get("max_completion_tokens"),
                        "capabilities": m.get("supported_parameters") or [],
                        "is_free": client.is_free_model(m),
                        "input_price_per_token": prompt_rate,
                        "output_price_per_token": comp_rate,
                    },
                )

                # Record snapshot
                ModelMetadataSnapshot.objects.create(
                    model=ai_model,
                    model_identifier=m["id"],
                    metadata_json=m,
                    pricing_json=pricing,
                    source="catalogue_refresh",
                )
                created_or_updated += 1

            return _envelope({"refreshed_count": created_or_updated, "status": "success"})
        except Exception as exc:
            return _error(f"Failed to refresh catalogue: {exc}", status.HTTP_500_INTERNAL_SERVER_ERROR)


# ---------------------------------------------------------------------------
# Persistent Comparison Views (Module A)
# ---------------------------------------------------------------------------


class ComparisonListView(APIView):
    """GET /api/v1/comparisons/ & POST /api/v1/comparisons/"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = ComparisonSession.objects.filter(user=request.user).order_by("-created_at")
        limit = min(int(request.query_params.get("limit", 50)), 100)
        offset = max(int(request.query_params.get("offset", 0)), 0)
        items = qs[offset : offset + limit]
        return _envelope(
            {
                "count": qs.count(),
                "limit": limit,
                "offset": offset,
                "results": ComparisonSessionSerializer(items, many=True).data,
            }
        )

    def post(self, request):
        """Save a comparison session with its results."""
        prompt = request.data.get("prompt") or request.data.get("message")
        models = request.data.get("models")
        results = request.data.get("results") or []
        mode = request.data.get("mode") or "standard"

        if not prompt or not models:
            return _error("Prompt and models are required.", status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            session = ComparisonSession.objects.create(
                user=request.user,
                prompt=prompt,
                models_json=models,
                system_prompt=request.data.get("system_prompt", ""),
                temperature=request.data.get("temperature"),
                max_tokens=request.data.get("max_tokens"),
                mode=mode,
            )

            records = []
            for idx, r in enumerate(results):
                records.append(
                    ComparisonResultRecord(
                        session=session,
                        model_identifier=r.get("model", ""),
                        response=r.get("response", ""),
                        duration_ms=r.get("duration_ms", 0),
                        input_tokens=r.get("usage", {}).get("prompt_tokens"),
                        output_tokens=r.get("usage", {}).get("completion_tokens"),
                        total_tokens=r.get("usage", {}).get("total_tokens"),
                        used_own_key=r.get("used_own_key", False),
                        error=r.get("error"),
                        slot_index=idx,
                    )
                )
            ComparisonResultRecord.objects.bulk_create(records)

        return _envelope(ComparisonSessionSerializer(session).data, code=status.HTTP_201_CREATED)


class ComparisonDetailView(APIView):
    """GET /api/v1/comparisons/{id}/ & DELETE /api/v1/comparisons/{id}/"""

    permission_classes = [IsAuthenticated]

    def get(self, request, session_id):
        session = get_object_or_404(ComparisonSession, id=session_id, user=request.user)
        return _envelope(ComparisonSessionSerializer(session).data)

    def delete(self, request, session_id):
        session = get_object_or_404(ComparisonSession, id=session_id, user=request.user)
        session.delete()
        return _envelope({"message": "Comparison deleted successfully."})


# ---------------------------------------------------------------------------
# Dataset Management Views (Module C)
# ---------------------------------------------------------------------------


class DatasetListView(APIView):
    """GET /api/v1/datasets/ & POST /api/v1/datasets/"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        seed_builtin_datasets()
        # Return built-in public datasets and user-owned custom datasets
        qs = BenchmarkDataset.objects.filter(
            Q(is_builtin=True) | Q(owner=request.user)
        ).distinct()

        category = request.query_params.get("category")
        if category:
            qs = qs.filter(category=category)

        return _envelope(
            {
                "count": qs.count(),
                "results": BenchmarkDatasetSerializer(qs, many=True).data,
            }
        )

    def post(self, request):
        name = request.data.get("name")
        if not name or not str(name).strip():
            return _error("Dataset 'name' is required.", status.HTTP_400_BAD_REQUEST)

        description = request.data.get("description", "")
        category = request.data.get("category", "custom")
        test_cases = request.data.get("test_cases", [])

        with transaction.atomic():
            dataset = BenchmarkDataset.objects.create(
                owner=request.user,
                name=name.strip(),
                description=description,
                category=category,
                visibility="private",
                is_builtin=False,
            )

            checksum = compute_dataset_checksum(test_cases)
            version = BenchmarkDatasetVersion.objects.create(
                dataset=dataset,
                version=1,
                checksum=checksum,
                test_case_count=len(test_cases),
                schema_version="1.0",
            )

            records = []
            for idx, tc in enumerate(test_cases):
                records.append(
                    BenchmarkTestCase(
                        dataset_version=version,
                        external_case_id=tc.get("id") or f"case-{idx+1:03d}",
                        input_json=tc.get("input") or {"prompt": tc.get("prompt", "")},
                        expected_output_json=tc.get("expected") or {"answer": tc.get("answer", "")},
                        evaluation_config_json=tc.get("evaluation") or {"method": tc.get("evaluation_method", "exact_match")},
                        tags=tc.get("tags", []),
                        difficulty=tc.get("difficulty", "medium"),
                        order_index=idx,
                    )
                )
            if records:
                BenchmarkTestCase.objects.bulk_create(records)

        return _envelope(BenchmarkDatasetSerializer(dataset).data, code=status.HTTP_201_CREATED)


class DatasetDetailView(APIView):
    """GET /api/v1/datasets/{id}/ & PATCH /api/v1/datasets/{id}/ & DELETE /api/v1/datasets/{id}/"""

    permission_classes = [IsAuthenticated]

    def get(self, request, dataset_id):
        seed_builtin_datasets()
        dataset = get_object_or_404(
            BenchmarkDataset.objects.filter(Q(is_builtin=True) | Q(owner=request.user)),
            id=dataset_id,
        )
        data = BenchmarkDatasetSerializer(dataset).data
        # Include test cases from latest version
        latest_ver = dataset.latest_version
        data["test_cases"] = []
        if latest_ver:
            data["test_cases"] = [
                {
                    "id": tc.external_case_id,
                    "input": tc.input_json,
                    "expected": tc.expected_output_json,
                    "evaluation": tc.evaluation_config_json,
                    "difficulty": tc.difficulty,
                    "tags": tc.tags,
                }
                for tc in latest_ver.test_cases.all()
            ]
        return _envelope(data)

    def patch(self, request, dataset_id):
        dataset = get_object_or_404(BenchmarkDataset, id=dataset_id, owner=request.user)
        if dataset.is_builtin:
            return _error("Cannot modify built-in dataset.", status.HTTP_403_FORBIDDEN)

        if "name" in request.data:
            dataset.name = request.data["name"]
        if "description" in request.data:
            dataset.description = request.data["description"]
        if "category" in request.data:
            dataset.category = request.data["category"]
        dataset.save()

        return _envelope(BenchmarkDatasetSerializer(dataset).data)

    def delete(self, request, dataset_id):
        dataset = get_object_or_404(BenchmarkDataset, id=dataset_id, owner=request.user)
        if dataset.is_builtin:
            return _error("Cannot delete built-in dataset.", status.HTTP_403_FORBIDDEN)
        dataset.delete()
        return _envelope({"message": "Dataset deleted successfully."})


class DatasetVersionListView(APIView):
    """GET /api/v1/datasets/{id}/versions/ & POST /api/v1/datasets/{id}/versions/"""

    permission_classes = [IsAuthenticated]

    def get(self, request, dataset_id):
        dataset = get_object_or_404(
            BenchmarkDataset.objects.filter(Q(is_builtin=True) | Q(owner=request.user)),
            id=dataset_id,
        )
        return _envelope(BenchmarkDatasetVersionSerializer(dataset.versions.all(), many=True).data)

    def post(self, request, dataset_id):
        """Create a new immutable version of a custom dataset."""
        dataset = get_object_or_404(BenchmarkDataset, id=dataset_id, owner=request.user)
        if dataset.is_builtin:
            return _error("Cannot create versions on built-in dataset.", status.HTTP_403_FORBIDDEN)

        test_cases = request.data.get("test_cases", [])
        if not test_cases:
            return _error("New version requires 'test_cases'.", status.HTTP_400_BAD_REQUEST)

        latest_ver = dataset.versions.order_by("-version").first()
        next_ver_num = (latest_ver.version + 1) if latest_ver else 1

        with transaction.atomic():
            checksum = compute_dataset_checksum(test_cases)
            version = BenchmarkDatasetVersion.objects.create(
                dataset=dataset,
                version=next_ver_num,
                checksum=checksum,
                test_case_count=len(test_cases),
                schema_version="1.0",
            )
            records = [
                BenchmarkTestCase(
                    dataset_version=version,
                    external_case_id=tc.get("id") or f"case-{idx+1:03d}",
                    input_json=tc.get("input") or {"prompt": tc.get("prompt", "")},
                    expected_output_json=tc.get("expected") or {"answer": tc.get("answer", "")},
                    evaluation_config_json=tc.get("evaluation") or {"method": tc.get("evaluation_method", "exact_match")},
                    tags=tc.get("tags", []),
                    difficulty=tc.get("difficulty", "medium"),
                    order_index=idx,
                )
                for idx, tc in enumerate(test_cases)
            ]
            BenchmarkTestCase.objects.bulk_create(records)

        return _envelope(BenchmarkDatasetVersionSerializer(version).data, code=status.HTTP_201_CREATED)


class DatasetImportView(APIView):
    """POST /api/v1/datasets/import/ — Upload JSONL or CSV file."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        name = request.data.get("name")
        description = request.data.get("description", "")
        category = request.data.get("category", "custom")
        raw_content = request.data.get("content")

        file_obj = request.FILES.get("file")
        if file_obj:
            raw_content = file_obj.read().decode("utf-8")

        if not name or not raw_content:
            return _error("Dataset 'name' and 'content' or 'file' required.", status.HTTP_400_BAD_REQUEST)

        try:
            dataset, version = import_dataset_from_jsonl(
                request.user, name, description, category, raw_content
            )
            return _envelope(
                {
                    "dataset": BenchmarkDatasetSerializer(dataset).data,
                    "version": BenchmarkDatasetVersionSerializer(version).data,
                },
                code=status.HTTP_201_CREATED,
            )
        except Exception as exc:
            return _error(f"Import failed: {exc}", status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Benchmark Engine Views (Module B & G)
# ---------------------------------------------------------------------------


class BenchmarkExperimentListCreateView(APIView):
    """GET /api/v1/benchmarks/ & POST /api/v1/benchmarks/"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = BenchmarkExperiment.objects.filter(owner=request.user).order_by("-created_at")
        status_param = request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        limit = min(int(request.query_params.get("limit", 50)), 100)
        offset = max(int(request.query_params.get("offset", 0)), 0)
        items = qs[offset : offset + limit]

        return _envelope(
            {
                "count": qs.count(),
                "limit": limit,
                "offset": offset,
                "results": BenchmarkExperimentListSerializer(items, many=True).data,
            }
        )

    def post(self, request):
        name = request.data.get("name")
        dataset_id = request.data.get("dataset_id")
        dataset_version_id = request.data.get("dataset_version_id")
        models = request.data.get("models") or []
        config = request.data.get("configuration") or {}

        if not name or not models:
            return _error("Experiment 'name' and at least one model in 'models' are required.", status.HTTP_400_BAD_REQUEST)

        # Resolve dataset version
        version = None
        if dataset_version_id:
            version = get_object_or_404(BenchmarkDatasetVersion, id=dataset_version_id)
        elif dataset_id:
            dataset = get_object_or_404(
                BenchmarkDataset.objects.filter(Q(is_builtin=True) | Q(owner=request.user)),
                id=dataset_id,
            )
            version = dataset.latest_version

        if not version:
            return _error("Valid dataset version not found.", status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            experiment = BenchmarkExperiment.objects.create(
                owner=request.user,
                name=name,
                description=request.data.get("description", ""),
                dataset_version=version,
                configuration_json=config,
                status="queued",
            )

            exp_models = []
            for m in models:
                model_id = m if isinstance(m, str) else m.get("id")
                prov_id = "mock" if (isinstance(model_id, str) and model_id.startswith("mock")) else (m.get("provider", "openrouter") if isinstance(m, dict) else "openrouter")
                exp_models.append(
                    ExperimentModel(
                        experiment=experiment,
                        model_identifier=model_id,
                        provider_id=prov_id,
                    )
                )
            ExperimentModel.objects.bulk_create(exp_models)

        # Auto-start if requested
        if request.data.get("auto_start", True):
            BenchmarkOrchestrator.start_experiment_async(str(experiment.id))

        return _envelope(
            {
                "experiment_id": str(experiment.id),
                "status": "queued",
                "status_url": f"/api/v1/benchmarks/{experiment.id}/progress/",
                "experiment": BenchmarkExperimentListSerializer(experiment).data,
            },
            code=status.HTTP_202_ACCEPTED,
        )


class BenchmarkExperimentDetailView(APIView):
    """GET /api/v1/benchmarks/{id}/ & DELETE /api/v1/benchmarks/{id}/"""

    permission_classes = [IsAuthenticated]

    def get(self, request, experiment_id):
        exp = get_object_or_404(BenchmarkExperiment, id=experiment_id, owner=request.user)
        return _envelope(BenchmarkExperimentDetailSerializer(exp).data)

    def delete(self, request, experiment_id):
        exp = get_object_or_404(BenchmarkExperiment, id=experiment_id, owner=request.user)
        exp.delete()
        return _envelope({"message": "Benchmark experiment deleted."})


class BenchmarkStartView(APIView):
    """POST /api/v1/benchmarks/{id}/start/ — Asynchronous trigger."""

    permission_classes = [IsAuthenticated]

    def post(self, request, experiment_id):
        exp = get_object_or_404(BenchmarkExperiment, id=experiment_id, owner=request.user)
        if exp.status in ("running", "validating", "evaluating", "aggregating"):
            return _error("Experiment is already running.", status.HTTP_400_BAD_REQUEST)

        BenchmarkOrchestrator.start_experiment_async(str(exp.id))
        return _envelope(
            {
                "experiment_id": str(exp.id),
                "status": "queued",
                "status_url": f"/api/v1/benchmarks/{exp.id}/progress/",
            },
            code=status.HTTP_202_ACCEPTED,
        )


class BenchmarkCancelView(APIView):
    """POST /api/v1/benchmarks/{id}/cancel/"""

    permission_classes = [IsAuthenticated]

    def post(self, request, experiment_id):
        exp = get_object_or_404(BenchmarkExperiment, id=experiment_id, owner=request.user)
        cancelled_exp = BenchmarkOrchestrator.cancel_experiment(exp)
        return _envelope(
            {
                "experiment_id": str(cancelled_exp.id),
                "status": cancelled_exp.status,
                "message": "Cancellation requested.",
            }
        )


class BenchmarkProgressView(APIView):
    """GET /api/v1/benchmarks/{id}/progress/"""

    permission_classes = [IsAuthenticated]

    def get(self, request, experiment_id):
        exp = get_object_or_404(BenchmarkExperiment, id=experiment_id, owner=request.user)
        percent = (
            round(((exp.completed_cases + exp.failed_cases) / exp.total_cases) * 100.0, 1)
            if exp.total_cases > 0
            else 0.0
        )
        return _envelope(
            {
                "experiment_id": str(exp.id),
                "status": exp.status,
                "total_cases": exp.total_cases,
                "completed_cases": exp.completed_cases,
                "failed_cases": exp.failed_cases,
                "progress_percentage": min(100.0, percent),
                "started_at": exp.started_at,
                "completed_at": exp.completed_at,
            }
        )


class BenchmarkResultsView(APIView):
    """GET /api/v1/benchmarks/{id}/results/ — Detailed test case executions and drill-downs."""

    permission_classes = [IsAuthenticated]

    def get(self, request, experiment_id):
        exp = get_object_or_404(BenchmarkExperiment, id=experiment_id, owner=request.user)
        executions = (
            exp.executions.select_related("test_case", "response_record")
            .prefetch_related("evaluation_results")
            .all()
        )

        model_filter = request.query_params.get("model")
        if model_filter:
            executions = executions.filter(model_identifier=model_filter)

        status_filter = request.query_params.get("status")
        if status_filter:
            executions = executions.filter(status=status_filter)

        return _envelope(
            {
                "experiment": BenchmarkExperimentDetailSerializer(exp).data,
                "executions": BenchmarkExecutionSerializer(executions, many=True).data,
            }
        )


class BenchmarkExportView(APIView):
    """GET /api/v1/benchmarks/{id}/export/?export_format=csv|json (or format=...)"""

    permission_classes = [IsAuthenticated]
    format_kwarg = None

    def get(self, request, experiment_id):
        exp = get_object_or_404(BenchmarkExperiment, id=experiment_id, owner=request.user)
        export_format = (
            request.query_params.get("export_format")
            or request.query_params.get("format")
            or "json"
        ).lower()

        executions = (
            exp.executions.select_related("test_case", "response_record")
            .prefetch_related("evaluation_results")
            .all()
        )

        if export_format == "csv":
            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = f'attachment; filename="benchmark_{exp.id}.csv"'
            writer = csv.writer(response)
            writer.writerow(
                [
                    "execution_id",
                    "case_id",
                    "model",
                    "prompt",
                    "response",
                    "status",
                    "duration_ms",
                    "tokens_per_second",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "estimated_cost",
                    "eval_metric",
                    "eval_score",
                    "eval_passed",
                ]
            )

            for e in executions:
                resp_text = e.response_record.response_text if hasattr(e, "response_record") else ""
                first_eval = e.evaluation_results.first()
                writer.writerow(
                    [
                        str(e.id),
                        e.test_case.external_case_id,
                        e.model_identifier,
                        e.test_case.input_json.get("prompt", ""),
                        resp_text[:1000],
                        e.status,
                        e.duration_ms,
                        e.tokens_per_second,
                        e.input_tokens,
                        e.output_tokens,
                        e.total_tokens,
                        e.estimated_cost,
                        first_eval.metric_name if first_eval else "",
                        first_eval.score if first_eval else "",
                        first_eval.passed if first_eval else "",
                    ]
                )
            return response

        # Default JSON export
        data = {
            "experiment": BenchmarkExperimentDetailSerializer(exp).data,
            "results": BenchmarkExecutionSerializer(executions, many=True).data,
            "exported_at": timezone.now().isoformat(),
        }
        response = HttpResponse(json.dumps(data, indent=2, default=str), content_type="application/json")
        response["Content-Disposition"] = f'attachment; filename="benchmark_{exp.id}.json"'
        return response


# ---------------------------------------------------------------------------
# Evaluation Endpoints (Module E)
# ---------------------------------------------------------------------------


class EvaluationListView(APIView):
    """GET /api/v1/evaluations/"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = EvaluationRun.objects.filter(experiment__owner=request.user).order_by("-created_at")
        limit = min(int(request.query_params.get("limit", 50)), 100)
        return _envelope(
            [
                {
                    "id": str(r.id),
                    "experiment_id": str(r.experiment.id),
                    "experiment_name": r.experiment.name,
                    "evaluator_type": r.evaluator_type,
                    "status": r.status,
                    "created_at": r.created_at,
                }
                for r in qs[:limit]
            ]
        )


class HumanFeedbackView(APIView):
    """POST /api/v1/evaluations/human-feedback/"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        execution_id = request.data.get("execution_id")
        rating = int(request.data.get("rating", 3))
        preferred = bool(request.data.get("preferred_response", False))
        notes = request.data.get("notes", "")

        execution = get_object_or_404(
            BenchmarkExecution, id=execution_id, experiment__owner=request.user
        )

        feedback = HumanEvaluation.objects.create(
            execution=execution,
            evaluator_user=request.user,
            rating=max(1, min(5, rating)),
            preferred_response=preferred,
            notes=notes,
        )

        return _envelope(
            {
                "id": str(feedback.id),
                "execution_id": str(execution.id),
                "rating": feedback.rating,
                "preferred_response": feedback.preferred_response,
                "message": "Human feedback recorded.",
            },
            code=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# Analytics Endpoints (Module D, G, J)
# ---------------------------------------------------------------------------


class AnalyticsOverviewView(APIView):
    """GET /api/v1/analytics/overview/"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        data = AnalyticsService.get_overview_analytics(user=request.user)
        return _envelope(data)


class AnalyticsPerformanceView(APIView):
    """GET /api/v1/analytics/performance/ — Latency & throughput distributions."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        execs = BenchmarkExecution.objects.filter(
            experiment__owner=request.user, status="success"
        )
        models = execs.values_list("model_identifier", flat=True).distinct()

        results = []
        for m in models:
            m_execs = execs.filter(model_identifier=m)
            latencies = [e.duration_ms for e in m_execs if e.duration_ms is not None]
            tpss = [e.tokens_per_second for e in m_execs if e.tokens_per_second is not None]

            if latencies:
                sorted_lats = sorted(latencies)
                results.append(
                    {
                        "model": m,
                        "sample_count": len(latencies),
                        "avg_latency_ms": round(sum(latencies) / len(latencies), 1),
                        "p50_latency_ms": sorted_lats[int(len(sorted_lats) * 0.5)],
                        "p95_latency_ms": sorted_lats[min(int(len(sorted_lats) * 0.95), len(sorted_lats) - 1)],
                        "min_latency_ms": round(min(latencies), 1),
                        "max_latency_ms": round(max(latencies), 1),
                        "avg_tokens_per_second": round(sum(tpss) / len(tpss), 1) if tpss else 0,
                    }
                )

        return _envelope(results)


class AnalyticsCostView(APIView):
    """GET /api/v1/analytics/cost/ — Cost Intelligence & Workload Projections."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        daily_req = int(request.query_params.get("daily_requests", 1000))
        input_tok = int(request.query_params.get("avg_input_tokens", 800))
        output_tok = int(request.query_params.get("avg_output_tokens", 300))
        models_str = request.query_params.get("models")

        if models_str:
            model_ids = [m.strip() for m in models_str.split(",") if m.strip()]
        else:
            # Default comparison set
            model_ids = [
                "openai/gpt-4o-mini",
                "anthropic/claude-3.5-haiku",
                "google/gemini-2.0-flash-exp:free",
                "meta-llama/llama-3.3-70b-instruct",
            ]

        projections = CostIntelligenceService.project_workload_costs(
            daily_req, input_tok, output_tok, model_ids
        )
        return _envelope(
            {
                "workload_params": {
                    "daily_requests": daily_req,
                    "avg_input_tokens": input_tok,
                    "avg_output_tokens": output_tok,
                },
                "projections": projections,
            }
        )


# ---------------------------------------------------------------------------
# Leaderboard Endpoints (Module F)
# ---------------------------------------------------------------------------


class LeaderboardListView(APIView):
    """GET /api/v1/leaderboards/"""

    permission_classes = [AllowAny]

    def get(self, request):
        category = request.query_params.get("category", "overall")

        # Parse custom weights if passed
        weights = None
        if "w_quality" in request.query_params:
            try:
                weights = {
                    "quality": float(request.query_params.get("w_quality", 0.4)),
                    "speed": float(request.query_params.get("w_speed", 0.25)),
                    "cost": float(request.query_params.get("w_cost", 0.2)),
                    "reliability": float(request.query_params.get("w_reliability", 0.15)),
                }
            except ValueError:
                pass

        entries = LeaderboardService.get_leaderboard(category=category, weights=weights)
        return _envelope(
            {
                "category": category,
                "weights_used": weights or LeaderboardService.DEFAULT_WEIGHTS,
                "rankings": entries,
            }
        )


class LeaderboardCategoryView(APIView):
    """GET /api/v1/leaderboards/{category}/"""

    permission_classes = [AllowAny]

    def get(self, request, category):
        weights = None
        if "w_quality" in request.query_params:
            try:
                weights = {
                    "quality": float(request.query_params.get("w_quality", 0.4)),
                    "speed": float(request.query_params.get("w_speed", 0.25)),
                    "cost": float(request.query_params.get("w_cost", 0.2)),
                    "reliability": float(request.query_params.get("w_reliability", 0.15)),
                }
            except ValueError:
                pass

        entries = LeaderboardService.get_leaderboard(category=category, weights=weights)
        return _envelope(
            {
                "category": category,
                "weights_used": weights or LeaderboardService.DEFAULT_WEIGHTS,
                "rankings": entries,
            }
        )


class LeaderboardRecommendView(APIView):
    """GET /api/v1/leaderboards/recommend/ — Model Recommendation Advisor."""

    permission_classes = [AllowAny]

    def get(self, request):
        category = request.query_params.get("category", "overall")
        max_latency = request.query_params.get("max_latency_ms")
        max_cost = request.query_params.get("max_cost_per_1m")
        min_quality = request.query_params.get("min_quality")
        free_only = request.query_params.get("free_only") in ("true", "1")

        recommendations = LeaderboardService.recommend_models(
            category=category,
            max_latency_ms=float(max_latency) if max_latency else None,
            max_cost_per_1m=float(max_cost) if max_cost else None,
            min_quality=float(min_quality) if min_quality else None,
            free_only=free_only,
        )
        return _envelope({"category": category, "recommendations": recommendations})
