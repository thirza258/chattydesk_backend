from django.urls import path
from benchmarking.views import (
    AnalyticsCostView,
    AnalyticsOverviewView,
    AnalyticsPerformanceView,
    BenchmarkCancelView,
    BenchmarkExperimentDetailView,
    BenchmarkExperimentListCreateView,
    BenchmarkExportView,
    BenchmarkProgressView,
    BenchmarkResultsView,
    BenchmarkStartView,
    ComparisonDetailView,
    ComparisonListView,
    DatasetDetailView,
    DatasetImportView,
    DatasetListView,
    DatasetVersionListView,
    EvaluationListView,
    HumanFeedbackView,
    LeaderboardCategoryView,
    LeaderboardListView,
    LeaderboardRecommendView,
    ModelDetailView,
    ModelHistoryView,
    ModelListView,
    ModelRefreshView,
)

urlpatterns = [
    # Model Registry
    path("models/", ModelListView.as_view(), name="models-list"),
    path("models/refresh/", ModelRefreshView.as_view(), name="models-refresh"),
    path("models/<path:model_id>/history/", ModelHistoryView.as_view(), name="model-history"),
    path("models/<path:model_id>/", ModelDetailView.as_view(), name="model-detail"),

    # Persistent Comparisons (Module A)
    path("comparisons/", ComparisonListView.as_view(), name="comparisons-list"),
    path("comparisons/<uuid:session_id>/", ComparisonDetailView.as_view(), name="comparison-detail"),

    # Datasets (Module C)
    path("datasets/", DatasetListView.as_view(), name="datasets-list"),
    path("datasets/import/", DatasetImportView.as_view(), name="datasets-import"),
    path("datasets/<uuid:dataset_id>/", DatasetDetailView.as_view(), name="dataset-detail"),
    path("datasets/<uuid:dataset_id>/versions/", DatasetVersionListView.as_view(), name="dataset-versions"),

    # Benchmarks (Module B & G)
    path("benchmarks/", BenchmarkExperimentListCreateView.as_view(), name="benchmarks-list"),
    path("benchmarks/<uuid:experiment_id>/", BenchmarkExperimentDetailView.as_view(), name="benchmark-detail"),
    path("benchmarks/<uuid:experiment_id>/start/", BenchmarkStartView.as_view(), name="benchmark-start"),
    path("benchmarks/<uuid:experiment_id>/cancel/", BenchmarkCancelView.as_view(), name="benchmark-cancel"),
    path("benchmarks/<uuid:experiment_id>/progress/", BenchmarkProgressView.as_view(), name="benchmark-progress"),
    path("benchmarks/<uuid:experiment_id>/results/", BenchmarkResultsView.as_view(), name="benchmark-results"),
    path("benchmarks/<uuid:experiment_id>/export/", BenchmarkExportView.as_view(), name="benchmark-export"),

    # Evaluations (Module E)
    path("evaluations/", EvaluationListView.as_view(), name="evaluations-list"),
    path("evaluations/human-feedback/", HumanFeedbackView.as_view(), name="human-feedback"),

    # Analytics (Module D, G, J)
    path("analytics/overview/", AnalyticsOverviewView.as_view(), name="analytics-overview"),
    path("analytics/performance/", AnalyticsPerformanceView.as_view(), name="analytics-performance"),
    path("analytics/cost/", AnalyticsCostView.as_view(), name="analytics-cost"),

    # Leaderboards (Module F)
    path("leaderboards/", LeaderboardListView.as_view(), name="leaderboard-list"),
    path("leaderboards/recommend/", LeaderboardRecommendView.as_view(), name="leaderboard-recommend"),
    path("leaderboards/<str:category>/", LeaderboardCategoryView.as_view(), name="leaderboard-category"),
]
