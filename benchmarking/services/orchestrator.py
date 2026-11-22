import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import close_old_connections, transaction
from django.db.models import F
from django.utils import timezone

from benchmarking.models import (
    BenchmarkDatasetVersion,
    BenchmarkExecution,
    BenchmarkExperiment,
    BenchmarkResponse,
    BenchmarkRun,
    EvaluationResult,
    EvaluationRun,
    ExperimentModel,
)
from benchmarking.providers.base import ProviderRequest
from benchmarking.providers.registry import get_provider
from benchmarking.services.analytics import AnalyticsService
from benchmarking.services.evaluator import EvaluationEngine
from benchmarking.services.leaderboard import LeaderboardService

logger = logging.getLogger(__name__)


class BenchmarkOrchestrator:
    """Coordinates lifecycle, bounded parallel execution, evaluation, and analytics aggregation."""

    _executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="benchmark_worker_")
    _db_lock = threading.Lock()
    _cancelled_ids = set()

    @classmethod
    def start_experiment_async(cls, experiment_id: str) -> None:
        """Trigger experiment execution in a background worker thread."""
        cls._executor.submit(cls._run_experiment_lifecycle, experiment_id)

    @classmethod
    def cancel_experiment(cls, experiment: BenchmarkExperiment) -> BenchmarkExperiment:
        """Request cancellation of an active or queued experiment."""
        cls._cancelled_ids.add(str(experiment.id))
        with cls._db_lock:
            experiment.refresh_from_db()
            if experiment.status in ("completed", "cancelled", "failed"):
                return experiment
            experiment.status = "cancelled"
            experiment.error_message = "Cancelled by user."
            experiment.completed_at = timezone.now()
            experiment.save(update_fields=["status", "error_message", "completed_at"])

            # Cancel pending executions
            experiment.executions.filter(status="pending").update(
                status="cancelled",
                error_message="Cancelled by user.",
            )
        return experiment

    @classmethod
    def _run_experiment_lifecycle(cls, experiment_id: str) -> None:
        """Complete lifecycle pipeline: validating -> running -> evaluating -> aggregating -> completed."""
        try:
            experiment = BenchmarkExperiment.objects.get(id=experiment_id)
        except BenchmarkExperiment.DoesNotExist:
            logger.error("Experiment %s not found for execution", experiment_id)
            return

        try:
            # 1. Validating
            experiment.status = "validating"
            experiment.started_at = timezone.now()
            experiment.save(update_fields=["status", "started_at"])

            test_cases = list(experiment.dataset_version.test_cases.all())
            models = list(experiment.models.all())
            config = experiment.configuration_json or {}
            repetitions = max(1, int(config.get("repetitions", 1)))
            concurrency = min(10, max(1, int(config.get("concurrency", 3))))
            temperature = config.get("temperature")
            max_tokens = config.get("max_tokens")
            system_prompt = config.get("system_prompt")
            timeout_sec = int(config.get("timeout", 120))
            max_retries = int(config.get("max_retries", 1))

            if not test_cases:
                raise ValueError("Dataset version contains no test cases.")
            if not models:
                raise ValueError("No models selected for experiment.")

            # Create pending executions
            executions_to_create = []
            for tc in test_cases:
                for em in models:
                    for rep in range(repetitions):
                        executions_to_create.append(
                            BenchmarkExecution(
                                experiment=experiment,
                                test_case=tc,
                                experiment_model=em,
                                model_identifier=em.model_identifier,
                                provider_id=em.provider_id,
                                repetition_index=rep,
                                status="pending",
                            )
                        )

            with transaction.atomic():
                BenchmarkExecution.objects.bulk_create(executions_to_create)
                experiment.total_cases = len(executions_to_create)
                experiment.completed_cases = 0
                experiment.failed_cases = 0
                experiment.status = "running"
                experiment.save(
                    update_fields=[
                        "total_cases",
                        "completed_cases",
                        "failed_cases",
                        "status",
                    ]
                )

            run_record = BenchmarkRun.objects.create(
                experiment=experiment,
                status="running",
                worker_reference=f"thread-{threading.get_ident()}",
            )

            # 2. Executing with bounded concurrency
            executions = list(experiment.executions.filter(status="pending"))

            close_old_connections()

            def _execute_single(exec_obj: BenchmarkExecution) -> bool:
                close_old_connections()
                try:
                    if str(experiment.id) in cls._cancelled_ids:
                        with cls._db_lock:
                            exec_obj.status = "cancelled"
                            exec_obj.save(update_fields=["status"])
                        return False

                    with cls._db_lock:
                        exec_obj.status = "running"
                        exec_obj.started_at = timezone.now()
                        exec_obj.save(update_fields=["status", "started_at"])

                    provider = (
                        get_provider("mock")
                        if (exec_obj.model_identifier.startswith("mock/") or exec_obj.provider_id == "mock")
                        else get_provider(exec_obj.provider_id)
                    )
                    tc = exec_obj.test_case
                    prompt = tc.input_json.get("prompt", "")
                    sys_prompt = tc.input_json.get("system_prompt") or system_prompt

                    req = ProviderRequest(
                        prompt=prompt,
                        model=exec_obj.model_identifier,
                        system_prompt=sys_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout=timeout_sec,
                        user=experiment.owner,
                    )

                    attempt = 0
                    success = False
                    last_err = None

                    while attempt <= max_retries and not success:
                        try:
                            resp = provider.generate(req)
                            success = True

                            with cls._db_lock:
                                exec_obj.completed_at = timezone.now()
                                exec_obj.duration_ms = resp.duration_ms
                                exec_obj.ttft_ms = resp.ttft_ms
                                exec_obj.input_tokens = resp.input_tokens
                                exec_obj.output_tokens = resp.output_tokens
                                exec_obj.total_tokens = resp.total_tokens
                                exec_obj.estimated_cost = resp.estimated_cost
                                exec_obj.actual_cost = resp.actual_cost
                                exec_obj.status = "success"

                                # Calculate throughput
                                if resp.duration_ms and resp.duration_ms > 0 and resp.output_tokens:
                                    exec_obj.tokens_per_second = round(
                                        (resp.output_tokens / (resp.duration_ms / 1000.0)), 2
                                    )

                                exec_obj.save()

                                # Save generated response text
                                BenchmarkResponse.objects.create(
                                    execution=exec_obj,
                                    response_text=resp.text,
                                    response_metadata=resp.raw_response,
                                    provider_metadata=resp.provider_metadata,
                                )

                        except Exception as exc:
                            last_err = exc
                            attempt += 1
                            if attempt <= max_retries:
                                backoff = (2 ** attempt) * 0.5 + random.uniform(0.1, 0.5)
                                time.sleep(backoff)

                    if not success:
                        with cls._db_lock:
                            exec_obj.completed_at = timezone.now()
                            exec_obj.status = "error"
                            normalized = provider.normalize_error(last_err)
                            exec_obj.error_code = str(normalized.get("error_code") or "ERROR")
                            exec_obj.error_message = str(normalized.get("message") or last_err)
                            exec_obj.retry_count = attempt
                            exec_obj.save()

                    # Atomically update progress counts
                    with cls._db_lock:
                        if success:
                            BenchmarkExperiment.objects.filter(id=experiment.id).exclude(status="cancelled").update(
                                completed_cases=F("completed_cases") + 1
                            )
                        else:
                            BenchmarkExperiment.objects.filter(id=experiment.id).exclude(status="cancelled").update(
                                failed_cases=F("failed_cases") + 1
                            )

                    return success
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(_execute_single, item) for item in executions]
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception as e:
                        logger.exception("Error executing test case: %s", e)

            # Check if experiment was cancelled during execution
            experiment.refresh_from_db()
            if experiment.status == "cancelled":
                run_record.status = "cancelled"
                run_record.completed_at = timezone.now()
                run_record.save(update_fields=["status", "completed_at"])
                return

            # 3. Evaluating
            experiment.status = "evaluating"
            experiment.save(update_fields=["status"])

            eval_run = EvaluationRun.objects.create(
                experiment=experiment,
                evaluator_type="automated",
                status="running",
            )

            successful_execs = list(
                experiment.executions.filter(status="success").select_related("test_case", "response_record")
            )

            eval_results_to_create = []
            for exec_item in successful_execs:
                resp_text = (
                    exec_item.response_record.response_text
                    if hasattr(exec_item, "response_record")
                    else ""
                )
                tc = exec_item.test_case
                eval_config = tc.evaluation_config_json or {}
                expected_out = tc.expected_output_json or {}

                outputs = EvaluationEngine.evaluate(
                    response_text=resp_text,
                    expected_output=expected_out,
                    evaluation_config=eval_config,
                    prompt=tc.input_json.get("prompt", ""),
                    system_prompt=system_prompt or "",
                )

                for out in outputs:
                    eval_results_to_create.append(
                        EvaluationResult(
                            execution=exec_item,
                            evaluation_run=eval_run,
                            metric_name=out.metric_name,
                            score=out.score,
                            maximum_score=out.maximum_score,
                            passed=out.passed,
                            explanation=out.explanation,
                            raw_result_json=out.raw_details,
                        )
                    )

            if eval_results_to_create:
                EvaluationResult.objects.bulk_create(eval_results_to_create)

            eval_run.status = "completed"
            eval_run.save(update_fields=["status"])

            # 4. Aggregating
            experiment.status = "aggregating"
            experiment.save(update_fields=["status"])

            AnalyticsService.aggregate_experiment(experiment)
            LeaderboardService.refresh_leaderboard_snapshots()

            # 5. Completed / Partially Completed
            experiment.refresh_from_db()
            if experiment.status != "cancelled":
                if experiment.failed_cases > 0 and experiment.completed_cases > 0:
                    experiment.status = "partially_completed"
                elif experiment.failed_cases > 0 and experiment.completed_cases == 0:
                    experiment.status = "failed"
                else:
                    experiment.status = "completed"

                experiment.completed_at = timezone.now()
                experiment.save(update_fields=["status", "completed_at"])

            run_record.status = experiment.status
            run_record.completed_at = timezone.now()
            run_record.save(update_fields=["status", "completed_at"])

        except Exception as exc:
            logger.exception("Benchmark lifecycle error on %s: %s", experiment_id, exc)
            try:
                experiment.refresh_from_db()
                experiment.status = "failed"
                experiment.error_message = str(exc)
                experiment.completed_at = timezone.now()
                experiment.save(update_fields=["status", "error_message", "completed_at"])
            except Exception:
                pass
