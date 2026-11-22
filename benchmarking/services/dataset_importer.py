import csv
import hashlib
import io
import json
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone

from benchmarking.models import BenchmarkDataset, BenchmarkDatasetVersion, BenchmarkTestCase

BUILTIN_DATASETS = [
    {
        "name": "General Knowledge QA",
        "description": "Curated factual multi-domain questions with verifiable ground truth references.",
        "category": "general_qa",
        "cases": [
            {
                "case_id": "gen-001",
                "prompt": "What is the capital of France?",
                "expected": {"answer": "Paris"},
                "evaluation": {"method": "exact_match"},
                "difficulty": "easy",
                "tags": ["geography", "facts"],
            },
            {
                "case_id": "gen-002",
                "prompt": "Who formulated the general theory of relativity?",
                "expected": {"answer": "Albert Einstein"},
                "evaluation": {"method": "exact_match"},
                "difficulty": "easy",
                "tags": ["physics", "history"],
            },
            {
                "case_id": "gen-003",
                "prompt": "What is the chemical formula for water?",
                "expected": {"answer": "H2O"},
                "evaluation": {"method": "exact_match"},
                "difficulty": "easy",
                "tags": ["chemistry"],
            },
            {
                "case_id": "gen-004",
                "prompt": "Which planet in our solar system has the most prominent ring system?",
                "expected": {"answer": "Saturn"},
                "evaluation": {"method": "exact_match"},
                "difficulty": "easy",
                "tags": ["astronomy"],
            },
            {
                "case_id": "gen-005",
                "prompt": "What is the primary currency used in Japan?",
                "expected": {"answer": "Yen"},
                "evaluation": {"method": "exact_match"},
                "difficulty": "easy",
                "tags": ["economics"],
            },
        ],
    },
    {
        "name": "GSM8K-style Reasoning",
        "description": "Multi-step arithmetic and reasoning word problems with precise numeric solutions.",
        "category": "reasoning",
        "cases": [
            {
                "case_id": "reasoning-001",
                "prompt": "A store has 40 apples. It sells 15 in the morning and receives a shipment of 25 in the afternoon. How many apples are in the store at the end of the day?",
                "expected": {"answer": "50"},
                "evaluation": {"method": "numeric_exact_match"},
                "difficulty": "medium",
                "tags": ["arithmetic", "word-problems"],
            },
            {
                "case_id": "reasoning-002",
                "prompt": "A product costs $80 and receives a 25% discount. What is the final price?",
                "expected": {"answer": "60"},
                "evaluation": {"method": "numeric_exact_match"},
                "difficulty": "medium",
                "tags": ["percentages", "finance"],
            },
            {
                "case_id": "reasoning-003",
                "prompt": "A train travels at 60 miles per hour for 2.5 hours. How many miles does it travel?",
                "expected": {"answer": "150"},
                "evaluation": {"method": "numeric_exact_match"},
                "difficulty": "easy",
                "tags": ["distance", "physics"],
            },
            {
                "case_id": "reasoning-004",
                "prompt": "If 3 painters can paint 3 rooms in 3 hours, how many hours does it take 6 painters to paint 6 rooms at the same rate?",
                "expected": {"answer": "3"},
                "evaluation": {"method": "numeric_exact_match"},
                "difficulty": "hard",
                "tags": ["logic", "rates"],
            },
            {
                "case_id": "reasoning-005",
                "prompt": "Lisa has twice as many marbles as Tom. Tom has 14 marbles. Lisa gives 5 marbles to her brother. How many marbles does Lisa have now?",
                "expected": {"answer": "23"},
                "evaluation": {"method": "numeric_exact_match"},
                "difficulty": "medium",
                "tags": ["algebra", "reasoning"],
            },
        ],
    },
    {
        "name": "Mathematics & Computation",
        "description": "Arithmetic calculations, algebraic equations, and numerical evaluations.",
        "category": "math",
        "cases": [
            {
                "case_id": "math-001",
                "prompt": "Calculate: (15 * 8) - (45 / 3)",
                "expected": {"answer": "105"},
                "evaluation": {"method": "numeric_exact_match"},
                "difficulty": "easy",
                "tags": ["order-of-operations"],
            },
            {
                "case_id": "math-002",
                "prompt": "Solve for x: 3x + 12 = 33. What is x?",
                "expected": {"answer": "7"},
                "evaluation": {"method": "numeric_exact_match"},
                "difficulty": "easy",
                "tags": ["linear-equation"],
            },
            {
                "case_id": "math-003",
                "prompt": "What is the square root of 144 plus 25?",
                "expected": {"answer": "37"},
                "evaluation": {"method": "numeric_exact_match"},
                "difficulty": "medium",
                "tags": ["roots"],
            },
            {
                "case_id": "math-004",
                "prompt": "What is 15% of 300?",
                "expected": {"answer": "45"},
                "evaluation": {"method": "numeric_exact_match"},
                "difficulty": "easy",
                "tags": ["percentages"],
            },
            {
                "case_id": "math-005",
                "prompt": "What is 2 raised to the power of 8?",
                "expected": {"answer": "256"},
                "evaluation": {"method": "numeric_exact_match"},
                "difficulty": "easy",
                "tags": ["exponents"],
            },
        ],
    },
    {
        "name": "Coding & Algorithms",
        "description": "Programming problem solving, algorithm design, and code generation.",
        "category": "coding",
        "cases": [
            {
                "case_id": "code-001",
                "prompt": "Write a Python function named `is_palindrome(s: str) -> bool` that returns True if the string is a palindrome ignoring case and spaces.",
                "expected": {"answer": "def is_palindrome"},
                "evaluation": {"method": "token_f1"},
                "difficulty": "medium",
                "tags": ["python", "strings"],
            },
            {
                "case_id": "code-002",
                "prompt": "Explain the time complexity of binary search on a sorted array of size n. State Big-O notation.",
                "expected": {"answer": "O(log n)"},
                "evaluation": {"method": "exact_match"},
                "difficulty": "easy",
                "tags": ["algorithms", "complexity"],
            },
            {
                "case_id": "code-003",
                "prompt": "Write a TypeScript function `sumArray(nums: number[]): number` using reduce.",
                "expected": {"answer": "reduce"},
                "evaluation": {"method": "exact_match"},
                "difficulty": "easy",
                "tags": ["typescript", "functional"],
            },
        ],
    },
    {
        "name": "Instruction Following & Constraints",
        "description": "Evaluates adherence to strict negative constraints and formatting instructions.",
        "category": "instruction_following",
        "cases": [
            {
                "case_id": "inst-001",
                "prompt": "Explain what photosynthesis is. Provide your answer in exactly three numbered bullet points. Do not include an introductory or concluding paragraph.",
                "expected": {"answer": "1."},
                "evaluation": {"method": "llm_judge"},
                "difficulty": "medium",
                "tags": ["constraints", "formatting"],
            },
            {
                "case_id": "inst-002",
                "prompt": "Write a summary of renewable energy without using the letter 'e' anywhere in the response.",
                "expected": {"answer": ""},
                "evaluation": {"method": "llm_judge"},
                "difficulty": "hard",
                "tags": ["negative-constraint"],
            },
        ],
    },
    {
        "name": "Structured Output & JSON Extraction",
        "description": "Extraction of structured entities conforming to specified JSON schemas.",
        "category": "structured_output",
        "cases": [
            {
                "case_id": "json-001",
                "prompt": "Extract the user information from this text into valid JSON: 'Alice Smith, age 29, lives in Seattle and works as a Software Engineer.' Output only JSON with keys: name, age, city, occupation.",
                "expected": {
                    "schema": {
                        "type": "object",
                        "required": ["name", "age", "city", "occupation"],
                    }
                },
                "evaluation": {"method": "json_schema_validation"},
                "difficulty": "medium",
                "tags": ["json", "extraction"],
            },
            {
                "case_id": "json-002",
                "prompt": "Generate a JSON object representing a book with title, author, year_published, and genres (a list of strings). Output valid JSON only.",
                "expected": {
                    "schema": {
                        "type": "object",
                        "required": ["title", "author", "year_published", "genres"],
                    }
                },
                "evaluation": {"method": "json_schema_validation"},
                "difficulty": "easy",
                "tags": ["json", "generation"],
            },
        ],
    },
    {
        "name": "Text Summarization",
        "description": "Passage summarization evaluated using ROUGE and Token F1 overlap against reference abstracts.",
        "category": "summarization",
        "cases": [
            {
                "case_id": "sum-001",
                "prompt": "Summarize in two concise sentences: 'The James Webb Space Telescope (JWST) is a space telescope designed primarily to conduct infrared astronomy. As the largest optical telescope in space, its high resolution and sensitivity allow it to view objects too old, distant, or faint for the Hubble Space Telescope. This enables investigations across many fields of astronomy and cosmology.'",
                "expected": {"answer": "JWST is an infrared space telescope designed to observe distant, faint celestial objects beyond Hubble's capabilities."},
                "evaluation": {"method": "rouge_scores"},
                "difficulty": "medium",
                "tags": ["summarization", "science"],
            },
        ],
    },
    {
        "name": "Classification & Intent",
        "description": "Multi-class sentiment and intent classification evaluated with accuracy and F1.",
        "category": "classification",
        "cases": [
            {
                "case_id": "cls-001",
                "prompt": "Classify the sentiment of this review as either POSITIVE, NEGATIVE, or NEUTRAL: 'The battery life is astonishingly good and charges rapidly, though the packaging was slightly dented.' Reply with just the label.",
                "expected": {"label": "positive"},
                "evaluation": {"method": "classification_accuracy"},
                "difficulty": "easy",
                "tags": ["sentiment"],
            },
            {
                "case_id": "cls-002",
                "prompt": "Classify the customer intent as BILLING, TECHNICAL_SUPPORT, or PRODUCT_INQUIRY: 'Can you tell me if the Pro tier supports up to 10 concurrent users?' Reply with just the label.",
                "expected": {"label": "product_inquiry"},
                "evaluation": {"method": "classification_accuracy"},
                "difficulty": "easy",
                "tags": ["intent"],
            },
        ],
    },
]


def compute_dataset_checksum(cases: List[Dict[str, Any]]) -> str:
    """Compute SHA-256 checksum over normalized test cases."""
    canonical = json.dumps(cases, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def seed_builtin_datasets() -> int:
    """Idempotently seed the built-in benchmark datasets and create initial immutable versions."""
    created_count = 0
    for ds_def in BUILTIN_DATASETS:
        dataset, _ = BenchmarkDataset.objects.get_or_create(
            name=ds_def["name"],
            is_builtin=True,
            defaults={
                "description": ds_def["description"],
                "category": ds_def["category"],
                "visibility": "public",
            },
        )

        cases_data = ds_def["cases"]
        checksum = compute_dataset_checksum(cases_data)

        # Check if version 1 exists
        version, ver_created = BenchmarkDatasetVersion.objects.get_or_create(
            dataset=dataset,
            version=1,
            defaults={
                "checksum": checksum,
                "test_case_count": len(cases_data),
                "schema_version": "1.0",
                "metadata_json": {"source": "builtin_curated"},
            },
        )

        if ver_created or version.test_cases.count() == 0:
            for idx, c in enumerate(cases_data):
                BenchmarkTestCase.objects.create(
                    dataset_version=version,
                    external_case_id=c["case_id"],
                    input_json={"prompt": c["prompt"]},
                    expected_output_json=c.get("expected", {}),
                    evaluation_config_json=c.get("evaluation", {}),
                    tags=c.get("tags", []),
                    difficulty=c.get("difficulty", "medium"),
                    order_index=idx,
                )
            created_count += 1

    return created_count


def import_dataset_from_jsonl(
    user: Any,
    name: str,
    description: str,
    category: str,
    file_content: str,
) -> Tuple[BenchmarkDataset, BenchmarkDatasetVersion]:
    """Parse JSONL content and create a new custom BenchmarkDataset with immutable version 1."""
    cases = []
    for line in file_content.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        cases.append(json.loads(line))

    if not cases:
        raise ValueError("JSONL file contains no test cases.")

    with transaction.atomic():
        dataset = BenchmarkDataset.objects.create(
            owner=user,
            name=name,
            description=description,
            category=category,
            visibility="private",
            is_builtin=False,
        )

        checksum = compute_dataset_checksum(cases)
        version = BenchmarkDatasetVersion.objects.create(
            dataset=dataset,
            version=1,
            checksum=checksum,
            test_case_count=len(cases),
            schema_version="1.0",
        )

        for idx, item in enumerate(cases):
            BenchmarkTestCase.objects.create(
                dataset_version=version,
                external_case_id=item.get("id") or f"case-{idx+1:03d}",
                input_json=item.get("input") or {"prompt": item.get("prompt", "")},
                expected_output_json=item.get("expected") or {"answer": item.get("answer", "")},
                evaluation_config_json=item.get("evaluation") or {"method": item.get("evaluation_method", "exact_match")},
                tags=item.get("tags", []),
                difficulty=item.get("difficulty", "medium"),
                order_index=idx,
            )

    return dataset, version


def export_dataset_to_jsonl(version: BenchmarkDatasetVersion) -> str:
    """Export a dataset version to JSONL format."""
    lines = []
    for tc in version.test_cases.all():
        row = {
            "id": tc.external_case_id,
            "category": version.dataset.category,
            "difficulty": tc.difficulty,
            "input": tc.input_json,
            "expected": tc.expected_output_json,
            "evaluation": tc.evaluation_config_json,
            "tags": tc.tags,
        }
        lines.append(json.dumps(row))
    return "\n".join(lines)
