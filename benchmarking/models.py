import uuid
from django.contrib.auth import get_user_model
from django.db import models as dj_models
from django.utils import timezone

models = dj_models
User = get_user_model()


class AIModel(models.Model):
    """Normalized metadata about AI models across providers."""

    id = models.BigAutoField(primary_key=True)
    model_identifier = models.CharField(max_length=255, unique=True, db_index=True)
    display_name = models.CharField(max_length=255)
    provider_id = models.CharField(max_length=100, default="openrouter", db_index=True)
    model_family = models.CharField(max_length=100, blank=True, default="")
    description = models.TextField(blank=True, default="")
    context_length = models.IntegerField(null=True, blank=True)
    max_output_tokens = models.IntegerField(null=True, blank=True)
    capabilities = models.JSONField(default=list, blank=True)
    supported_parameters = models.JSONField(default=list, blank=True)
    input_price_per_token = models.DecimalField(
        max_digits=18, decimal_places=12, null=True, blank=True
    )
    output_price_per_token = models.DecimalField(
        max_digits=18, decimal_places=12, null=True, blank=True
    )
    is_free = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name"]

    def __str__(self):
        return f"{self.display_name} ({self.model_identifier})"


class ModelMetadataSnapshot(models.Model):
    """Historical snapshot of model specs and pricing at a specific point in time."""

    id = models.BigAutoField(primary_key=True)
    model = models.ForeignKey(
        AIModel, on_delete=models.CASCADE, related_name="snapshots", null=True, blank=True
    )
    model_identifier = models.CharField(max_length=255, db_index=True)
    metadata_json = models.JSONField(default=dict)
    pricing_json = models.JSONField(default=dict)
    source = models.CharField(max_length=100, default="catalogue")
    observed_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-observed_at"]

    def __str__(self):
        return f"Snapshot of {self.model_identifier} at {self.observed_at}"


class BenchmarkDataset(models.Model):
    """Dataset entity grouping versions of benchmark test suites."""

    CATEGORY_CHOICES = [
        ("general_qa", "General Knowledge QA"),
        ("reasoning", "Reasoning & Logic"),
        ("math", "Mathematics & Arithmetic"),
        ("coding", "Coding & Algorithms"),
        ("instruction_following", "Instruction Following"),
        ("summarization", "Summarization"),
        ("structured_output", "Structured Output & JSON"),
        ("classification", "Classification"),
        ("custom", "Custom Workload"),
    ]

    VISIBILITY_CHOICES = [
        ("public", "Public"),
        ("private", "Private"),
        ("workspace", "Workspace"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        User, on_delete=models.CASCADE, null=True, blank=True, related_name="benchmark_datasets"
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    category = models.CharField(max_length=100, choices=CATEGORY_CHOICES, default="general_qa", db_index=True)
    visibility = models.CharField(max_length=50, choices=VISIBILITY_CHOICES, default="private")
    is_builtin = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def latest_version(self):
        return self.versions.order_by("-version").first()


class BenchmarkDatasetVersion(models.Model):
    """Immutable version of a dataset."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dataset = models.ForeignKey(
        BenchmarkDataset, on_delete=models.CASCADE, related_name="versions"
    )
    version = models.PositiveIntegerField(default=1)
    schema_version = models.CharField(max_length=50, default="1.0")
    checksum = models.CharField(max_length=64, blank=True, default="")
    test_case_count = models.PositiveIntegerField(default=0)
    metadata_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("dataset", "version")
        ordering = ["-version"]

    def __str__(self):
        return f"{self.dataset.name} v{self.version}"


class BenchmarkTestCase(models.Model):
    """Individual test case in an immutable dataset version."""

    DIFFICULTY_CHOICES = [
        ("easy", "Easy"),
        ("medium", "Medium"),
        ("hard", "Hard"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dataset_version = models.ForeignKey(
        BenchmarkDatasetVersion, on_delete=models.CASCADE, related_name="test_cases"
    )
    external_case_id = models.CharField(max_length=100, db_index=True)
    input_json = models.JSONField(default=dict)  # {"prompt": "...", "system_prompt": "..."}
    expected_output_json = models.JSONField(default=dict, blank=True)  # {"answer": "...", "schema": {...}}
    evaluation_config_json = models.JSONField(default=dict, blank=True)  # {"method": "...", "rubric": "..."}
    tags = models.JSONField(default=list, blank=True)
    difficulty = models.CharField(max_length=50, choices=DIFFICULTY_CHOICES, default="medium")
    order_index = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order_index", "created_at"]

    def __str__(self):
        return f"{self.external_case_id} ({self.dataset_version})"


class BenchmarkExperiment(models.Model):
    """Benchmark experiment execution definition and progress."""

    STATUS_CHOICES = [
        ("queued", "Queued"),
        ("validating", "Validating"),
        ("running", "Running"),
        ("evaluating", "Evaluating"),
        ("aggregating", "Aggregating"),
        ("completed", "Completed"),
        ("partially_completed", "Partially Completed"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="benchmark_experiments")
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    dataset_version = models.ForeignKey(
        BenchmarkDatasetVersion, on_delete=models.PROTECT, related_name="experiments"
    )
    configuration_json = models.JSONField(default=dict)  # models, repetitions, concurrency, temperature, etc.
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="queued", db_index=True)
    total_cases = models.PositiveIntegerField(default=0)
    completed_cases = models.PositiveIntegerField(default=0)
    failed_cases = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} [{self.status}]"


class ExperimentModel(models.Model):
    """Model participant in a benchmark experiment."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    experiment = models.ForeignKey(
        BenchmarkExperiment, on_delete=models.CASCADE, related_name="models"
    )
    model_identifier = models.CharField(max_length=255)
    provider_id = models.CharField(max_length=100, default="openrouter")
    model_identifier_snapshot = models.CharField(max_length=255, blank=True, default="")
    pricing_snapshot = models.JSONField(default=dict, blank=True)
    generation_config_json = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["model_identifier"]

    def __str__(self):
        return f"{self.model_identifier} in {self.experiment.name}"


class BenchmarkRun(models.Model):
    """Worker run tracking for an experiment."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    experiment = models.ForeignKey(
        BenchmarkExperiment, on_delete=models.CASCADE, related_name="runs"
    )
    status = models.CharField(max_length=50, default="running")
    worker_reference = models.CharField(max_length=255, blank=True, default="")
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_summary = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-started_at"]


class BenchmarkExecution(models.Model):
    """A single execution of one test case on one model at a specific repetition."""

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("success", "Success"),
        ("error", "Error"),
        ("timeout", "Timeout"),
        ("cancelled", "Cancelled"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    experiment = models.ForeignKey(
        BenchmarkExperiment, on_delete=models.CASCADE, related_name="executions"
    )
    test_case = models.ForeignKey(
        BenchmarkTestCase, on_delete=models.CASCADE, related_name="executions"
    )
    experiment_model = models.ForeignKey(
        ExperimentModel, on_delete=models.CASCADE, related_name="executions", null=True, blank=True
    )
    model_identifier = models.CharField(max_length=255, db_index=True)
    provider_id = models.CharField(max_length=100, default="openrouter")
    repetition_index = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="pending", db_index=True)
    request_id = models.CharField(max_length=100, blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.FloatField(null=True, blank=True)
    ttft_ms = models.FloatField(null=True, blank=True)
    tokens_per_second = models.FloatField(null=True, blank=True)
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    total_tokens = models.IntegerField(null=True, blank=True)
    estimated_cost = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    actual_cost = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    error_code = models.CharField(max_length=50, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    retry_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["started_at"]
        indexes = [
            models.Index(fields=["experiment", "status"]),
            models.Index(fields=["experiment", "model_identifier"]),
        ]

    def __str__(self):
        return f"{self.model_identifier} on {self.test_case.external_case_id} [rep {self.repetition_index}]: {self.status}"


class BenchmarkResponse(models.Model):
    """The generated answer text and metadata for a benchmark execution."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    execution = models.OneToOneField(
        BenchmarkExecution, on_delete=models.CASCADE, related_name="response_record"
    )
    response_text = models.TextField(blank=True, default="")
    response_metadata = models.JSONField(default=dict, blank=True)
    provider_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Response for {self.execution_id}"


class EvaluationRun(models.Model):
    """An evaluation sweep over an experiment's executions."""

    EVALUATOR_CHOICES = [
        ("deterministic", "Deterministic (Exact / Numeric / Regex / Schema)"),
        ("reference", "Reference-Based (F1 / ROUGE)"),
        ("llm_judge", "LLM-as-a-Judge"),
        ("human", "Human Feedback"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    experiment = models.ForeignKey(
        BenchmarkExperiment, on_delete=models.CASCADE, related_name="evaluation_runs"
    )
    evaluator_type = models.CharField(max_length=50, choices=EVALUATOR_CHOICES, default="deterministic")
    evaluator_model_id = models.CharField(max_length=255, blank=True, default="")
    rubric_version = models.CharField(max_length=50, default="1.0")
    status = models.CharField(max_length=50, default="completed")
    configuration_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.evaluator_type} evaluation for {self.experiment.name}"


class EvaluationResult(models.Model):
    """Quality evaluation score for a single execution."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    execution = models.ForeignKey(
        BenchmarkExecution, on_delete=models.CASCADE, related_name="evaluation_results"
    )
    evaluation_run = models.ForeignKey(
        EvaluationRun, on_delete=models.CASCADE, related_name="results"
    )
    metric_name = models.CharField(max_length=100, db_index=True)
    score = models.FloatField(default=0.0)
    maximum_score = models.FloatField(default=1.0)
    passed = models.BooleanField(default=False)
    explanation = models.TextField(blank=True, default="")
    raw_result_json = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["metric_name"]

    def __str__(self):
        return f"{self.metric_name}: {self.score}/{self.maximum_score} (passed={self.passed})"


class HumanEvaluation(models.Model):
    """User evaluation feedback on a model response."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    execution = models.ForeignKey(
        BenchmarkExecution, on_delete=models.CASCADE, related_name="human_evaluations"
    )
    evaluator_user = models.ForeignKey(User, on_delete=models.CASCADE)
    rating = models.PositiveSmallIntegerField(default=3)  # 1 to 5
    preferred_response = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)


class ExperimentSummary(models.Model):
    """Precomputed statistical summary for a model in an experiment."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    experiment = models.ForeignKey(
        BenchmarkExperiment, on_delete=models.CASCADE, related_name="summaries"
    )
    model_identifier = models.CharField(max_length=255, db_index=True)
    metrics_json = models.JSONField(default=dict)
    sample_count = models.PositiveIntegerField(default=0)
    generated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("experiment", "model_identifier")
        ordering = ["model_identifier"]

    def __str__(self):
        return f"Summary of {self.model_identifier} in {self.experiment.name}"


class LeaderboardSnapshot(models.Model):
    """Aggregated leaderboard ranking entries."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    benchmark_category = models.CharField(max_length=100, db_index=True)
    dataset_version = models.ForeignKey(
        BenchmarkDatasetVersion, on_delete=models.SET_NULL, null=True, blank=True
    )
    model_identifier = models.CharField(max_length=255, db_index=True)
    provider_id = models.CharField(max_length=100, default="openrouter")
    score = models.FloatField(default=0.0)
    quality_score = models.FloatField(default=0.0)
    speed_score = models.FloatField(default=0.0)
    cost_score = models.FloatField(default=0.0)
    reliability_score = models.FloatField(default=0.0)
    ranking = models.PositiveIntegerField(default=1)
    sample_count = models.PositiveIntegerField(default=0)
    evaluation_method = models.CharField(max_length=100, default="automated")
    is_public = models.BooleanField(default=True)
    generated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["ranking"]

    def __str__(self):
        return f"#{self.ranking} {self.model_identifier} ({self.benchmark_category}): {self.score}"


class ComparisonSession(dj_models.Model):
    """Persistent record of an interactive multi-model comparison (Module A)."""

    MODE_CHOICES = [
        ("standard", "Standard Comparison"),
        ("repeated_sampling", "Repeated Sampling"),
        ("configuration_comparison", "Configuration Comparison"),
        ("provider_comparison", "Provider Comparison"),
    ]

    id = dj_models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = dj_models.ForeignKey(
        User, on_delete=dj_models.CASCADE, related_name="comparison_sessions"
    )
    prompt = dj_models.TextField()
    models_json = dj_models.JSONField(default=list)  # list of model ids
    system_prompt = dj_models.TextField(blank=True, default="")
    temperature = dj_models.FloatField(null=True, blank=True)
    max_tokens = dj_models.IntegerField(null=True, blank=True)
    mode = dj_models.CharField(max_length=50, choices=MODE_CHOICES, default="standard")
    created_at = dj_models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def models(self):
        return self.models_json

    @models.setter
    def models(self, value):
        self.models_json = value

    def __str__(self):
        return f"Comparison session {self.id} by {self.user.username}"


class ComparisonResultRecord(models.Model):
    """Result for one model in a comparison session."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        ComparisonSession, on_delete=models.CASCADE, related_name="results"
    )
    model_identifier = models.CharField(max_length=255)
    response = models.TextField(blank=True, default="")
    duration_ms = models.IntegerField(default=0)
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    total_tokens = models.IntegerField(null=True, blank=True)
    estimated_cost = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    used_own_key = models.BooleanField(default=False)
    error = models.TextField(null=True, blank=True)
    slot_index = models.PositiveIntegerField(default=0)
    sample_index = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["slot_index", "sample_index"]
