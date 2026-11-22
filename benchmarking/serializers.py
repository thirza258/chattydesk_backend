from decimal import Decimal
from rest_framework import serializers

from benchmarking.models import (
    AIModel,
    BenchmarkDataset,
    BenchmarkDatasetVersion,
    BenchmarkExecution,
    BenchmarkExperiment,
    BenchmarkResponse,
    BenchmarkTestCase,
    ComparisonResultRecord,
    ComparisonSession,
    EvaluationResult,
    EvaluationRun,
    ExperimentModel,
    ExperimentSummary,
    HumanEvaluation,
    LeaderboardSnapshot,
    ModelMetadataSnapshot,
)


class AIModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIModel
        fields = [
            "id",
            "model_identifier",
            "display_name",
            "provider_id",
            "model_family",
            "description",
            "context_length",
            "max_output_tokens",
            "capabilities",
            "supported_parameters",
            "input_price_per_token",
            "output_price_per_token",
            "is_free",
            "is_active",
            "created_at",
            "updated_at",
        ]


class ModelMetadataSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = ModelMetadataSnapshot
        fields = [
            "id",
            "model_identifier",
            "metadata_json",
            "pricing_json",
            "source",
            "observed_at",
        ]


class BenchmarkTestCaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = BenchmarkTestCase
        fields = [
            "id",
            "external_case_id",
            "input_json",
            "expected_output_json",
            "evaluation_config_json",
            "tags",
            "difficulty",
            "order_index",
        ]


class BenchmarkDatasetVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = BenchmarkDatasetVersion
        fields = [
            "id",
            "version",
            "schema_version",
            "checksum",
            "test_case_count",
            "metadata_json",
            "created_at",
        ]


class BenchmarkDatasetSerializer(serializers.ModelSerializer):
    latest_version = serializers.SerializerMethodField()
    version_count = serializers.SerializerMethodField()

    class Meta:
        model = BenchmarkDataset
        fields = [
            "id",
            "name",
            "description",
            "category",
            "visibility",
            "is_builtin",
            "created_at",
            "updated_at",
            "latest_version",
            "version_count",
        ]

    def get_latest_version(self, obj):
        v = obj.latest_version
        if not v:
            return None
        return BenchmarkDatasetVersionSerializer(v).data

    def get_version_count(self, obj):
        return obj.versions.count()


class ExperimentModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExperimentModel
        fields = [
            "id",
            "model_identifier",
            "provider_id",
            "model_identifier_snapshot",
            "pricing_snapshot",
            "generation_config_json",
        ]


class BenchmarkExperimentListSerializer(serializers.ModelSerializer):
    dataset_name = serializers.CharField(source="dataset_version.dataset.name", read_only=True)
    dataset_category = serializers.CharField(source="dataset_version.dataset.category", read_only=True)
    dataset_version_num = serializers.IntegerField(source="dataset_version.version", read_only=True)
    models = serializers.SerializerMethodField()

    class Meta:
        model = BenchmarkExperiment
        fields = [
            "id",
            "name",
            "description",
            "status",
            "dataset_name",
            "dataset_category",
            "dataset_version_num",
            "total_cases",
            "completed_cases",
            "failed_cases",
            "models",
            "created_at",
            "started_at",
            "completed_at",
        ]

    def get_models(self, obj):
        return list(obj.models.values_list("model_identifier", flat=True))


class BenchmarkExperimentDetailSerializer(serializers.ModelSerializer):
    dataset = serializers.SerializerMethodField()
    models = ExperimentModelSerializer(many=True, read_only=True)
    summaries = serializers.SerializerMethodField()

    class Meta:
        model = BenchmarkExperiment
        fields = [
            "id",
            "name",
            "description",
            "dataset_version",
            "dataset",
            "configuration_json",
            "status",
            "total_cases",
            "completed_cases",
            "failed_cases",
            "error_message",
            "models",
            "summaries",
            "created_at",
            "started_at",
            "completed_at",
        ]

    def get_dataset(self, obj):
        v = obj.dataset_version
        return {
            "id": str(v.dataset.id),
            "name": v.dataset.name,
            "category": v.dataset.category,
            "version": v.version,
            "checksum": v.checksum,
            "test_case_count": v.test_case_count,
        }

    def get_summaries(self, obj):
        return [
            {
                "model_identifier": s.model_identifier,
                "metrics": s.metrics_json,
                "sample_count": s.sample_count,
            }
            for s in obj.summaries.all()
        ]


class EvaluationResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = EvaluationResult
        fields = [
            "id",
            "metric_name",
            "score",
            "maximum_score",
            "passed",
            "explanation",
            "raw_result_json",
        ]


class BenchmarkExecutionSerializer(serializers.ModelSerializer):
    test_case_id = serializers.CharField(source="test_case.external_case_id", read_only=True)
    prompt = serializers.CharField(source="test_case.input_json.prompt", read_only=True)
    expected_answer = serializers.SerializerMethodField()
    response_text = serializers.SerializerMethodField()
    evaluations = EvaluationResultSerializer(source="evaluation_results", many=True, read_only=True)

    class Meta:
        model = BenchmarkExecution
        fields = [
            "id",
            "test_case_id",
            "model_identifier",
            "provider_id",
            "repetition_index",
            "status",
            "duration_ms",
            "ttft_ms",
            "tokens_per_second",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "estimated_cost",
            "actual_cost",
            "error_code",
            "error_message",
            "retry_count",
            "prompt",
            "expected_answer",
            "response_text",
            "evaluations",
            "started_at",
            "completed_at",
        ]

    def get_expected_answer(self, obj):
        return obj.test_case.expected_output_json.get("answer")

    def get_response_text(self, obj):
        if hasattr(obj, "response_record"):
            return obj.response_record.response_text
        return ""


class LeaderboardSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaderboardSnapshot
        fields = [
            "id",
            "benchmark_category",
            "model_identifier",
            "provider_id",
            "score",
            "quality_score",
            "speed_score",
            "cost_score",
            "reliability_score",
            "ranking",
            "sample_count",
            "evaluation_method",
            "generated_at",
        ]


class ComparisonResultRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = ComparisonResultRecord
        fields = [
            "id",
            "model_identifier",
            "response",
            "duration_ms",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "estimated_cost",
            "used_own_key",
            "error",
            "slot_index",
            "sample_index",
        ]


class ComparisonSessionSerializer(serializers.ModelSerializer):
    models = serializers.JSONField(source="models_json")
    results = ComparisonResultRecordSerializer(many=True, read_only=True)

    class Meta:
        model = ComparisonSession
        fields = [
            "id",
            "prompt",
            "models",
            "system_prompt",
            "temperature",
            "max_tokens",
            "mode",
            "results",
            "created_at",
        ]
