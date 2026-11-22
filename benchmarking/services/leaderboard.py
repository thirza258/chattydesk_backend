from typing import Any, Dict, List, Optional
from django.db.models import Avg, Q
from django.utils import timezone

from benchmarking.models import (
    BenchmarkDatasetVersion,
    BenchmarkExperiment,
    ExperimentSummary,
    LeaderboardSnapshot,
)


class LeaderboardService:
    """Generates multi-dimensional rankings, composite scores, and model recommendations."""

    DEFAULT_WEIGHTS = {
        "quality": 0.40,
        "speed": 0.25,
        "cost": 0.20,
        "reliability": 0.15,
    }

    @classmethod
    def compute_composite_score(
        cls,
        quality_score: float,  # 0 to 100
        latency_p50_ms: Optional[float],
        tokens_per_second: Optional[float],
        cost_per_1m: float,
        success_rate: float,  # 0 to 100
        weights: Optional[Dict[str, float]] = None,
    ) -> float:
        """Calculate composite score: S = w_q * Q + w_s * Sp + w_c * Ce + w_r * R."""
        w = weights or cls.DEFAULT_WEIGHTS
        wq = w.get("quality", 0.40)
        ws = w.get("speed", 0.25)
        wc = w.get("cost", 0.20)
        wr = w.get("reliability", 0.15)

        # Normalize Quality (0 - 100)
        Q = min(max(quality_score, 0.0), 100.0)

        # Normalize Speed (higher tokens/s & lower latency -> higher score up to 100)
        # Latency target: 500ms or lower = 100, 5000ms = 0
        lat = latency_p50_ms if latency_p50_ms is not None else 2000.0
        lat_score = max(0.0, min(100.0, 100.0 - ((lat - 500.0) / 45.0)))
        tps = tokens_per_second if tokens_per_second is not None else 30.0
        tps_score = min(100.0, (tps / 100.0) * 100.0)
        Sp = (lat_score * 0.5) + (tps_score * 0.5)

        # Normalize Cost Efficiency: $0/1M = 100, $10/1M = 0
        Ce = max(0.0, min(100.0, 100.0 - (cost_per_1m * 10.0)))

        # Reliability (Success rate 0 - 100)
        R = min(max(success_rate, 0.0), 100.0)

        composite = (wq * Q) + (ws * Sp) + (wc * Ce) + (wr * R)
        return round(composite, 2)

    @classmethod
    def refresh_leaderboard_snapshots(cls) -> int:
        """Regenerate leaderboard rankings from completed experiments across categories."""
        summaries = ExperimentSummary.objects.select_related("experiment__dataset_version__dataset").all()
        if not summaries.exists():
            return 0

        # Group summaries by category and model
        by_category_model: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for s in summaries:
            category = (
                s.experiment.dataset_version.dataset.category
                if s.experiment.dataset_version and s.experiment.dataset_version.dataset
                else "general"
            )
            by_category_model.setdefault(category, {})
            by_category_model[category].setdefault(s.model_identifier, []).append(s.metrics_json)
            # Also add to overall category
            by_category_model.setdefault("overall", {})
            by_category_model["overall"].setdefault(s.model_identifier, []).append(s.metrics_json)

        count = 0
        for category, models_dict in by_category_model.items():
            ranked_list = []
            for model_id, metric_runs in models_dict.items():
                # Average metrics across runs
                qualities = [m.get("overall_quality_score", 0.0) for m in metric_runs]
                latencies = [m.get("latency_ms", {}).get("p50", 1500) for m in metric_runs if m.get("latency_ms", {}).get("p50")]
                tpss = [m.get("tokens_per_second", {}).get("mean", 30) for m in metric_runs if m.get("tokens_per_second", {}).get("mean")]
                costs = [m.get("cost_per_1m_tokens_usd", 0.0) for m in metric_runs]
                reliabilities = [m.get("success_rate", 100.0) for m in metric_runs]

                avg_q = sum(qualities) / len(qualities) if qualities else 0.0
                avg_lat = sum(latencies) / len(latencies) if latencies else 1500.0
                avg_tps = sum(tpss) / len(tpss) if tpss else 30.0
                avg_cost = sum(costs) / len(costs) if costs else 0.0
                avg_rel = sum(reliabilities) / len(reliabilities) if reliabilities else 100.0

                composite = cls.compute_composite_score(
                    avg_q, avg_lat, avg_tps, avg_cost, avg_rel
                )

                ranked_list.append(
                    {
                        "model_identifier": model_id,
                        "composite_score": composite,
                        "quality_score": round(avg_q, 1),
                        "speed_score": round(avg_tps, 1),
                        "cost_score": round(avg_cost, 4),
                        "reliability_score": round(avg_rel, 1),
                        "sample_count": sum(m.get("total_runs", 1) for m in metric_runs),
                    }
                )

            # Sort by composite score descending
            ranked_list.sort(key=lambda x: x["composite_score"], reverse=True)

            # Clear previous category snapshots and save new
            LeaderboardSnapshot.objects.filter(benchmark_category=category).delete()
            for rank_idx, item in enumerate(ranked_list):
                LeaderboardSnapshot.objects.create(
                    benchmark_category=category,
                    model_identifier=item["model_identifier"],
                    score=item["composite_score"],
                    quality_score=item["quality_score"],
                    speed_score=item["speed_score"],
                    cost_score=item["cost_score"],
                    reliability_score=item["reliability_score"],
                    ranking=rank_idx + 1,
                    sample_count=item["sample_count"],
                    is_public=True,
                    generated_at=timezone.now(),
                )
                count += 1

        return count

    @classmethod
    def get_leaderboard(
        cls,
        category: str = "overall",
        weights: Optional[Dict[str, float]] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch leaderboard entries, recalculating composite score if custom weights are provided."""
        snapshots = list(
            LeaderboardSnapshot.objects.filter(benchmark_category=category)
            .order_by("ranking")
        )

        # If no snapshots exist yet, generate from summaries
        if not snapshots:
            cls.refresh_leaderboard_snapshots()
            snapshots = list(
                LeaderboardSnapshot.objects.filter(benchmark_category=category)
                .order_by("ranking")
            )

        results = []
        for s in snapshots:
            score = s.score
            if weights:
                score = cls.compute_composite_score(
                    s.quality_score,
                    latency_p50_ms=None,
                    tokens_per_second=s.speed_score,
                    cost_per_1m=s.cost_score,
                    success_rate=s.reliability_score,
                    weights=weights,
                )

            results.append(
                {
                    "ranking": s.ranking,
                    "model_identifier": s.model_identifier,
                    "provider": s.provider_id,
                    "category": s.benchmark_category,
                    "composite_score": score,
                    "quality_score": s.quality_score,
                    "speed_score": s.speed_score,
                    "cost_per_1m": s.cost_score,
                    "reliability_score": s.reliability_score,
                    "sample_count": s.sample_count,
                    "updated_at": s.generated_at,
                }
            )

        if weights:
            results.sort(key=lambda x: x["composite_score"], reverse=True)
            for idx, r in enumerate(results):
                r["ranking"] = idx + 1

        return results

    @classmethod
    def recommend_models(
        cls,
        category: str = "overall",
        max_latency_ms: Optional[float] = None,
        max_cost_per_1m: Optional[float] = None,
        min_quality: Optional[float] = None,
        free_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """Task-specific model recommendation advisor based on measurable benchmark constraints."""
        entries = cls.get_leaderboard(category=category)
        recommendations = []

        for e in entries:
            # Check cost constraint
            if max_cost_per_1m is not None and e["cost_per_1m"] > max_cost_per_1m:
                continue
            # Check free only
            if free_only and e["cost_per_1m"] > 0:
                continue
            # Check quality constraint
            if min_quality is not None and e["quality_score"] < min_quality:
                continue

            reason = []
            if e["quality_score"] >= 80:
                reason.append("High response quality")
            if e["speed_score"] >= 40:
                reason.append("Fast token generation")
            if e["cost_per_1m"] == 0:
                reason.append("Completely free to run")
            elif e["cost_per_1m"] < 1.0:
                reason.append("Highly economical")
            if e["reliability_score"] >= 99:
                reason.append("Rock-solid 99%+ reliability")

            recommendations.append(
                {
                    **e,
                    "recommendation_reason": " · ".join(reason) if reason else "Balanced performance",
                }
            )

        recommendations.sort(key=lambda x: x["composite_score"], reverse=True)
        return recommendations[:5]
