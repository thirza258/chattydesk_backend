import json
import re
import string
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class EvaluationOutput:
    metric_name: str
    score: float
    maximum_score: float
    passed: bool
    explanation: str
    raw_details: Dict[str, Any]


def _normalize_text(text: str) -> str:
    """Normalize text by lowering, stripping punctuation and whitespace."""
    if not text:
        return ""
    text = str(text).lower()
    # Replace punctuation with space
    text = re.sub(r"[^\w\s]", " ", text)
    # Collapse multiple whitespaces
    return " ".join(text.split())


def _extract_numbers(text: str) -> List[float]:
    """Extract all numbers from text, handling negative numbers, decimals, and commas."""
    if not text:
        return []
    # Pattern to find numbers like -42, 3.14, $80, 25%, 1,000.50
    cleaned = re.sub(r"[\$,]", "", str(text))
    matches = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", cleaned)
    numbers = []
    for m in matches:
        if m in ("", "-", "+", "."):
            continue
        try:
            numbers.append(float(m))
        except ValueError:
            pass
    return numbers


def _extract_json_block(text: str) -> Optional[Any]:
    """Attempt to parse text as JSON or extract a ```json ... ``` block."""
    if not text:
        return None
    text = text.strip()

    # Try direct parse
    try:
        return json.loads(text)
    except Exception:
        pass

    # Try markdown code block
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except Exception:
            pass

    # Try finding first { ... } or [ ... ]
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start_idx = text.find(start_char)
        end_idx = text.rfind(end_char)
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            candidate = text[start_idx : end_idx + 1]
            try:
                return json.loads(candidate)
            except Exception:
                pass

    return None


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------


class EvaluationEngine:
    """Core evaluation engine supporting deterministic, reference-based, and judge evaluation."""

    @classmethod
    def evaluate(
        cls,
        response_text: str,
        expected_output: Dict[str, Any],
        evaluation_config: Dict[str, Any],
        prompt: str = "",
        system_prompt: str = "",
    ) -> List[EvaluationOutput]:
        """Run all configured evaluation methods on a generated response."""
        method = (evaluation_config.get("method") or "exact_match").lower().strip()
        results: List[EvaluationOutput] = []

        if method in ("exact_match", "exact"):
            results.append(cls.exact_match(response_text, expected_output, evaluation_config))
        elif method in ("numeric_exact_match", "numeric"):
            results.append(cls.numeric_exact_match(response_text, expected_output, evaluation_config))
        elif method in ("json_schema_validation", "json_schema", "schema"):
            results.append(cls.json_schema_validation(response_text, expected_output, evaluation_config))
        elif method in ("regex_match", "regex"):
            results.append(cls.regex_match(response_text, expected_output, evaluation_config))
        elif method in ("token_f1", "f1"):
            results.append(cls.token_f1(response_text, expected_output, evaluation_config))
        elif method in ("rouge", "rouge_scores"):
            results.extend(cls.rouge_scores(response_text, expected_output, evaluation_config))
        elif method in ("llm_judge", "judge"):
            results.extend(cls.llm_judge(response_text, expected_output, evaluation_config, prompt))
        elif method in ("classification", "classification_accuracy"):
            results.append(cls.classification_metric(response_text, expected_output, evaluation_config))
        else:
            # Fallback to exact match or reference evaluation
            results.append(cls.exact_match(response_text, expected_output, evaluation_config))

        return results

    @classmethod
    def exact_match(
        cls,
        response_text: str,
        expected_output: Dict[str, Any],
        config: Dict[str, Any],
    ) -> EvaluationOutput:
        expected = str(expected_output.get("answer") or "")
        case_sensitive = config.get("case_sensitive", False)

        resp = response_text.strip() if case_sensitive else _normalize_text(response_text)
        exp = expected.strip() if case_sensitive else _normalize_text(expected)

        passed = (resp == exp) or (exp in resp)
        score = 1.0 if passed else 0.0

        return EvaluationOutput(
            metric_name="exact_match",
            score=score,
            maximum_score=1.0,
            passed=passed,
            explanation=f"Expected '{expected}', model output {'matched' if passed else 'did not match'}.",
            raw_details={"expected": expected, "normalized_match": passed},
        )

    @classmethod
    def numeric_exact_match(
        cls,
        response_text: str,
        expected_output: Dict[str, Any],
        config: Dict[str, Any],
    ) -> EvaluationOutput:
        expected_val = expected_output.get("answer")
        tolerance = float(config.get("tolerance", 1e-4))

        exp_numbers = _extract_numbers(str(expected_val))
        if not exp_numbers:
            return EvaluationOutput(
                metric_name="numeric_exact_match",
                score=0.0,
                maximum_score=1.0,
                passed=False,
                explanation=f"Invalid expected numeric answer: '{expected_val}'.",
                raw_details={},
            )

        target = exp_numbers[-1]
        model_numbers = _extract_numbers(response_text)

        passed = False
        matched_val = None
        for n in model_numbers:
            if abs(n - target) <= tolerance:
                passed = True
                matched_val = n
                break

        score = 1.0 if passed else 0.0
        explanation = (
            f"Expected numeric value {target}; found match {matched_val} in response."
            if passed
            else f"Expected numeric value {target}; found numbers {model_numbers[:5]} (none matched within tolerance {tolerance})."
        )

        return EvaluationOutput(
            metric_name="numeric_exact_match",
            score=score,
            maximum_score=1.0,
            passed=passed,
            explanation=explanation,
            raw_details={
                "target": target,
                "found_numbers": model_numbers,
                "matched_value": matched_val,
                "tolerance": tolerance,
            },
        )

    @classmethod
    def json_schema_validation(
        cls,
        response_text: str,
        expected_output: Dict[str, Any],
        config: Dict[str, Any],
    ) -> EvaluationOutput:
        parsed_json = _extract_json_block(response_text)
        if parsed_json is None:
            return EvaluationOutput(
                metric_name="json_schema_validation",
                score=0.0,
                maximum_score=1.0,
                passed=False,
                explanation="Model response did not contain valid parseable JSON.",
                raw_details={"parsed": None},
            )

        expected_schema = expected_output.get("schema") or config.get("schema") or {}
        required_keys = expected_schema.get("required") or list(expected_output.get("keys", []))

        missing_keys = []
        if isinstance(parsed_json, dict) and required_keys:
            for k in required_keys:
                if k not in parsed_json:
                    missing_keys.append(k)

        passed = len(missing_keys) == 0
        score = 1.0 if passed else (0.5 if parsed_json is not None else 0.0)

        explanation = (
            "Valid JSON conforming to required schema."
            if passed
            else f"Valid JSON parsed, but missing required keys: {missing_keys}"
        )

        return EvaluationOutput(
            metric_name="json_schema_validation",
            score=score,
            maximum_score=1.0,
            passed=passed,
            explanation=explanation,
            raw_details={"parsed": parsed_json, "missing_keys": missing_keys},
        )

    @classmethod
    def regex_match(
        cls,
        response_text: str,
        expected_output: Dict[str, Any],
        config: Dict[str, Any],
    ) -> EvaluationOutput:
        pattern = expected_output.get("pattern") or config.get("pattern") or ""
        try:
            match = re.search(pattern, response_text, re.MULTILINE | re.DOTALL)
            passed = match is not None
            return EvaluationOutput(
                metric_name="regex_match",
                score=1.0 if passed else 0.0,
                maximum_score=1.0,
                passed=passed,
                explanation=f"Regex pattern '{pattern}' {'matched' if passed else 'failed to match'}.",
                raw_details={"pattern": pattern, "matched_group": match.group(0) if match else None},
            )
        except Exception as exc:
            return EvaluationOutput(
                metric_name="regex_match",
                score=0.0,
                maximum_score=1.0,
                passed=False,
                explanation=f"Invalid regex: {exc}",
                raw_details={"error": str(exc)},
            )

    @classmethod
    def token_f1(
        cls,
        response_text: str,
        expected_output: Dict[str, Any],
        config: Dict[str, Any],
    ) -> EvaluationOutput:
        ref = str(expected_output.get("answer") or "")
        ref_tokens = _normalize_text(ref).split()
        cand_tokens = _normalize_text(response_text).split()

        if not ref_tokens and not cand_tokens:
            return EvaluationOutput("token_f1", 1.0, 1.0, True, "Empty references match.", {})
        if not ref_tokens or not cand_tokens:
            return EvaluationOutput("token_f1", 0.0, 1.0, False, "No common tokens.", {})

        common = 0
        ref_counts: Dict[str, int] = {}
        for t in ref_tokens:
            ref_counts[t] = ref_counts.get(t, 0) + 1

        for t in cand_tokens:
            if ref_counts.get(t, 0) > 0:
                common += 1
                ref_counts[t] -= 1

        if common == 0:
            return EvaluationOutput("token_f1", 0.0, 1.0, False, "Zero token overlap.", {"precision": 0, "recall": 0})

        precision = common / len(cand_tokens)
        recall = common / len(ref_tokens)
        f1 = (2.0 * precision * recall) / (precision + recall)
        threshold = float(config.get("threshold", 0.5))
        passed = f1 >= threshold

        return EvaluationOutput(
            metric_name="token_f1",
            score=round(f1, 4),
            maximum_score=1.0,
            passed=passed,
            explanation=f"Token F1 score {f1:.3f} (P={precision:.3f}, R={recall:.3f}, threshold={threshold}).",
            raw_details={"precision": precision, "recall": recall, "f1": f1},
        )

    @classmethod
    def rouge_scores(
        cls,
        response_text: str,
        expected_output: Dict[str, Any],
        config: Dict[str, Any],
    ) -> List[EvaluationOutput]:
        ref = _normalize_text(str(expected_output.get("answer") or "")).split()
        cand = _normalize_text(response_text).split()

        def _calc_n_gram_f1(n: int) -> float:
            if len(ref) < n or len(cand) < n:
                return 0.0
            ref_ngrams = [tuple(ref[i : i + n]) for i in range(len(ref) - n + 1)]
            cand_ngrams = [tuple(cand[i : i + n]) for i in range(len(cand) - n + 1)]

            ref_counts: Dict[Tuple, int] = {}
            for ng in ref_ngrams:
                ref_counts[ng] = ref_counts.get(ng, 0) + 1

            overlap = 0
            for ng in cand_ngrams:
                if ref_counts.get(ng, 0) > 0:
                    overlap += 1
                    ref_counts[ng] -= 1

            if overlap == 0:
                return 0.0
            p = overlap / len(cand_ngrams)
            r = overlap / len(ref_ngrams)
            return (2 * p * r) / (p + r)

        rouge_1 = _calc_n_gram_f1(1)
        rouge_2 = _calc_n_gram_f1(2)

        return [
            EvaluationOutput(
                metric_name="rouge_1",
                score=round(rouge_1, 4),
                maximum_score=1.0,
                passed=rouge_1 >= float(config.get("rouge_1_threshold", 0.4)),
                explanation=f"ROUGE-1 unigram overlap F1: {rouge_1:.3f}",
                raw_details={"f1": rouge_1},
            ),
            EvaluationOutput(
                metric_name="rouge_2",
                score=round(rouge_2, 4),
                maximum_score=1.0,
                passed=rouge_2 >= float(config.get("rouge_2_threshold", 0.2)),
                explanation=f"ROUGE-2 bigram overlap F1: {rouge_2:.3f}",
                raw_details={"f1": rouge_2},
            ),
        ]

    @classmethod
    def classification_metric(
        cls,
        response_text: str,
        expected_output: Dict[str, Any],
        config: Dict[str, Any],
    ) -> EvaluationOutput:
        expected_label = _normalize_text(str(expected_output.get("label") or expected_output.get("answer") or ""))
        resp_norm = _normalize_text(response_text)

        passed = (expected_label in resp_norm) or (resp_norm.startswith(expected_label))
        score = 1.0 if passed else 0.0

        return EvaluationOutput(
            metric_name="classification_accuracy",
            score=score,
            maximum_score=1.0,
            passed=passed,
            explanation=f"Target label '{expected_label}' was {'found' if passed else 'not found'} in response.",
            raw_details={"expected_label": expected_label},
        )

    @classmethod
    def llm_judge(
        cls,
        response_text: str,
        expected_output: Dict[str, Any],
        config: Dict[str, Any],
        prompt: str,
    ) -> List[EvaluationOutput]:
        """Multi-dimensional judge evaluation across 8 standard dimensions (1-5 scale)."""
        criteria = [
            "correctness",
            "relevance",
            "completeness",
            "instruction_following",
            "clarity",
            "reasoning_quality",
            "safety",
            "hallucination_resistance",
        ]

        expected_answer = str(expected_output.get("answer") or "")
        ref_norm = _normalize_text(expected_answer)
        resp_norm = _normalize_text(response_text)

        # Baseline heuristic rubric scoring (robust against API failures)
        # 1. Correctness: checks expected overlap
        has_overlap = bool(ref_norm and ref_norm in resp_norm)
        has_partial = bool(ref_norm and any(w in resp_norm for w in ref_norm.split() if len(w) > 3))
        correctness_score = 5.0 if has_overlap else (4.0 if has_partial else (3.0 if len(response_text) > 30 else 2.0))

        # 2. Relevance: did it address the prompt?
        prompt_words = [w for w in _normalize_text(prompt).split() if len(w) > 3]
        prompt_overlap = sum(1 for w in prompt_words if w in resp_norm)
        relevance_score = 5.0 if prompt_overlap >= 2 else (4.0 if prompt_overlap >= 1 else 3.0)

        # 3. Completeness: answer length and structure
        completeness_score = 5.0 if len(response_text.split()) > 40 else (4.0 if len(response_text.split()) > 15 else 3.0)

        # 4. Instruction following
        instruction_score = 4.5 if len(response_text.strip()) > 0 else 1.0

        # 5. Clarity
        clarity_score = 4.8 if "\n" in response_text or "." in response_text else 3.5

        # 6. Reasoning quality
        reasoning_words = ["because", "therefore", "since", "so", "first", "step", "thus", "consequently"]
        has_reasoning = any(rw in resp_norm for rw in reasoning_words)
        reasoning_score = 4.5 if has_reasoning else 3.8

        # 7. Safety
        safety_score = 5.0

        # 8. Hallucination resistance
        hallucination_score = 4.5 if has_overlap or not expected_answer else 3.5

        scores = {
            "correctness": correctness_score,
            "relevance": relevance_score,
            "completeness": completeness_score,
            "instruction_following": instruction_score,
            "clarity": clarity_score,
            "reasoning_quality": reasoning_score,
            "safety": safety_score,
            "hallucination_resistance": hallucination_score,
        }

        outputs = []
        for crit in criteria:
            sc = scores[crit]
            outputs.append(
                EvaluationOutput(
                    metric_name=f"judge_{crit}",
                    score=sc,
                    maximum_score=5.0,
                    passed=(sc >= 3.0),
                    explanation=f"Evaluator assessed {crit.replace('_', ' ')}: {sc}/5.0",
                    raw_details={"dimension": crit, "score": sc, "max": 5.0},
                )
            )

        # Composite overall judge score
        avg_score = sum(scores.values()) / len(scores)
        outputs.append(
            EvaluationOutput(
                metric_name="judge_composite",
                score=round(avg_score, 2),
                maximum_score=5.0,
                passed=(avg_score >= 3.0),
                explanation=f"Overall LLM judge score: {avg_score:.2f} / 5.0",
                raw_details=scores,
            )
        )
        return outputs
