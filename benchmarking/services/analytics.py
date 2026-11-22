import math
from decimal import Decimal
from typing import Any, Dict, List, Optional
from django.db.models import Avg, Count, Sum
from django.utils import timezone

from benchmarking.models import (
    BenchmarkExecution,
    BenchmarkExperiment,
    EvaluationResult,
    ExperimentSummary,
)


def calculate_percentile(values: List[float], percentile: float) -> Optional[float]:
    """Calculate the p-th percentile of a list of floats (0 <= percentile <= 100)."""
    if not values:
        return None
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (percentile / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    d0 = sorted_vals[int(f)] * (c - k)
    d1 = sorted_vals[int(c)] * (k - f)
    return round(d0 + d1, 2)


def calculate_statistics(values: List[float]) -> Dict[str, Any]:
    """Calculate mean, median, min, max, std_dev, and standard percentiles."""
    if not values:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
            "std_dev": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
        }

    n = len(values)
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n if n > 1 else 0.0
    std_dev = math.sqrt(variance)

    return {
        "count": n,
        "mean": round(mean, 2),
        "median": calculate_percentile(values, 50),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "std_dev": round(std_dev, 2),
        "p50": calculate_percentile(values, 50),
        "p90": calculate_percentile(values, 90),
        "p95": calculate_percentile(values, 95),
        "p99": calculate_percentile(values, 99),
    }


class AnalyticsService:
    """Calculates statistical distributions and model summaries for benchmark runs."""

    @classmethod
    def aggregate_experiment(cls, experiment: BenchmarkExperiment) -> List[ExperimentSummary]:
        """Aggregate performance and quality metrics for all models in an experiment."""
        models = (
            experiment.executions.values_list("model_identifier", flat=True)
            .distinct()
        )
        summaries: List[ExperimentSummary] = []

        for model_id in models:
            execs = list(
                experiment.executions.filter(model_identifier=model_id)
            )
            if not execs:
                continue

            total_runs = len(execs)
            success_execs = [e for e in execs if e.status == "success"]
            error_execs = [e for e in execs if e.status == "error"]
            timeout_execs = [e for e in execs if e.status == "timeout"]

            success_rate = (len(success_execs) / total_runs) if total_runs > 0 else 0.0
            error_rate = (len(error_execs) / total_runs) if total_runs > 0 else 0.0
            timeout_rate = (len(timeout_execs) / total_runs) if total_runs > 0 else 0.0

            # Latency statistics
            latencies = [e.duration_ms for e in success_execs if e.duration_ms is not None]
            latency_stats = calculate_statistics(latencies)

            # Throughput (tokens / second)
            tps_values = [
                e.tokens_per_second
                for e in success_execs
                if e.tokens_per_second is not None
            ]
            tps_stats = calculate_statistics(tps_values)

            # Token usage & Cost
            total_prompt_tokens = sum(e.input_tokens or 0 for e in success_execs)
            total_completion_tokens = sum(e.output_tokens or 0 for e in success_execs)
            total_tokens = total_prompt_tokens + total_completion_tokens

            total_cost = sum(
                (e.estimated_cost or Decimal(0)) for e in success_execs
            )
            avg_cost = (
                (total_cost / Decimal(len(success_execs)))
                if success_execs
                else Decimal(0)
            )

            # Quality metrics
            eval_results = EvaluationResult.objects.filter(
                execution__in=success_execs
            )
            quality_by_metric: Dict[str, Dict[str, Any]] = {}
            metric_names = eval_results.values_list("metric_name", flat=True).distinct()

            total_passed_evals = 0
            total_evals_count = 0

            for m_name in metric_names:
                m_results = list(eval_results.filter(metric_name=m_name))
                scores = [r.score for r in m_results]
                passed_count = sum(1 for r in m_results if r.passed)
                total_passed_evals += passed_count
                total_evals_count += len(m_results)

                quality_by_metric[m_name] = {
                    "count": len(m_results),
                    "mean_score": round(sum(scores) / len(scores), 3) if scores else 0,
                    "max_score": m_results[0].maximum_score if m_results else 1.0,
                    "pass_rate": round(passed_count / len(m_results), 3) if m_results else 0,
                }

            overall_quality_score = (
                round((total_passed_evals / total_evals_count) * 100.0, 1)
                if total_evals_count > 0
                else 0.0
            )

            metrics_payload = {
                "total_runs": total_runs,
                "successful_runs": len(success_execs),
                "failed_runs": len(error_execs),
                "timeout_runs": len(timeout_execs),
                "success_rate": round(success_rate * 100.0, 1),
                "error_rate": round(error_rate * 100.0, 1),
                "timeout_rate": round(timeout_rate * 100.0, 1),
                "latency_ms": latency_stats,
                "tokens_per_second": tps_stats,
                "total_input_tokens": total_prompt_tokens,
                "total_output_tokens": total_completion_tokens,
                "total_tokens": total_tokens,
                "total_cost_usd": float(total_cost),
                "avg_cost_per_request_usd": float(avg_cost),
                "cost_per_1m_tokens_usd": (
                    float((total_cost / Decimal(total_tokens)) * Decimal(1_000_000))
                    if total_tokens > 0
                    else 0.0
                ),
                "overall_quality_score": overall_quality_score,
                "quality_metrics": quality_by_metric,
            }

            summary, _ = ExperimentSummary.objects.update_or_create(
                experiment=experiment,
                model_identifier=model_id,
                defaults={
                    "metrics_json": metrics_payload,
                    "sample_count": total_runs,
                    "generated_at": timezone.now(),
                },
            )
            summaries.append(summary)

        return summaries

    @classmethod
    def get_overview_analytics(cls, user: Any = None) -> Dict[str, Any]:
        """Aggregate system-wide or user-scoped benchmark analytics."""
        exp_qs = BenchmarkExperiment.objects.all()
        if user and user.is_authenticated:
            exp_qs = exp_qs.filter(owner=user)

        total_experiments = exp_qs.count()
        completed_experiments = exp_qs.filter(status="completed").count()

        exec_qs = BenchmarkExecution.objects.all()
        if user and user.is_authenticated:
            exec_qs = exec_qs.filter(experiment__owner=user)

        total_requests = exec_qs.count()
        successful_requests = exec_qs.filter(status="success").count()
        avg_success_rate = (
            round((successful_requests / total_requests) * 100.0, 1)
            if total_requests > 0
            else 0.0
        )

        models_evaluated = (
            exec_qs.values_list("model_identifier", flat=True)
            .distinct()
            .count()
        )

        return {
            "total_experiments": total_experiments,
            "completed_experiments": completed_experiments,
            "total_benchmark_requests": total_requests,
            "successful_requests": successful_requests,
            "avg_success_rate": avg_success_rate,
            "models_evaluated_count": models_evaluated,
        }
