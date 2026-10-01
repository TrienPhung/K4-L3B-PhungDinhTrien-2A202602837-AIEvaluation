"""
Day 14 — AI Evaluation & Benchmarking Pipeline
AICB-P1: AI Practical Competency Program, Phase 1

Key concepts from lecture:
    - Evaluation = Scientific Method for AI (Hypothesis → Experiment → Measure → Conclude → Iterate)
    - 4 nhóm metrics: Task Completion, Answer Quality, RAG-Specific, Business
    - RAG pipeline metrics: Context Recall → Context Precision → Faithfulness → Answer Relevancy
    - LLM-as-Judge: rubric scoring 1-5, detect bias (positional, verbosity, self-preference)
    - Golden dataset: stratified sampling (5 Easy + 7 Medium + 5 Hard + 3 Adversarial)
    - Failure taxonomy: hallucination, irrelevant, incomplete, off_topic, refusal
    - 5 Whys method for root cause analysis
    - CI/CD integration: eval as quality gate (score < threshold = block deploy)
    - Continuous Improvement Loop: Evaluate → Analyze → Improve → Augment → Repeat

Instructions:
    1. Fill in every required section marked with TODO.
    2. Do NOT change class/function signatures. The optional ``contexts``
       parameter in ``run_full_eval`` is part of the required interface.
    3. Copy this file to solution/solution.py when done.
    4. Run: pytest tests/ -v

The reranking helper is an optional bonus exercise and may remain unimplemented.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Task 1 — Data Models (Golden Dataset + Evaluation Results)
# ---------------------------------------------------------------------------

@dataclass
class QAPair:
    """
    A question-answer pair for evaluation (part of the Golden Dataset).

    Fields:
        question:           The question to answer.
        expected_answer:    The reference/ground-truth answer (expert-written).
        context:            Source context (may be empty string if not applicable).
        metadata:           Optional metadata dict (difficulty, category, etc.).
        retrieved_contexts: List of retrieved chunks (ORDER = retriever rank).
                            Used by the retrieval-side metrics (Task 2b).
    """
    question: str
    expected_answer: str
    context: str = ""
    metadata: dict = field(default_factory=dict)
    retrieved_contexts: list = field(default_factory=list)


@dataclass
class EvalResult:
    """
    Evaluation result for a single Q&A pair.

    Fields:
        qa_pair:        The original QAPair.
        actual_answer:  What the agent actually returned.
        faithfulness:   Float 0-1, how grounded the answer is in context.
        relevance:      Float 0-1, how relevant the answer is to the question.
        completeness:   Float 0-1, how complete the answer is vs expected.
        passed:         True if all three scores >= 0.5.
        failure_type:   None if passed, otherwise one of:
                        "hallucination", "irrelevant", "incomplete", "off_topic".
        context_precision: Float 0-1 or None — quality of retrieval ranking.
        context_recall:    Float 0-1 or None — coverage of expected by context.
                        (Both stay None unless retrieved chunks are supplied;
                         they are NOT part of overall_score().)
    """
    qa_pair: QAPair
    actual_answer: str
    faithfulness: float
    relevance: float
    completeness: float
    passed: bool = False
    failure_type: str | None = None
    context_precision: float | None = None
    context_recall: float | None = None

    def overall_score(self) -> float:
        """Average of faithfulness, relevance, and completeness."""
        return (self.faithfulness + self.relevance + self.completeness) / 3.0


# ---------------------------------------------------------------------------
# Task 2 — RAGAS Evaluator (Simplified word-overlap heuristic)
# ---------------------------------------------------------------------------

# Common English stopwords are ignored so overlap reflects *content* words,
# not filler (otherwise "is"/"a"/"the" inflate every score).
STOPWORDS: set[str] = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "at", "to", "for", "with", "as", "by", "and", "or",
    "it", "its", "this", "that", "these", "those", "from", "into", "than",
}


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokenization, ignoring punctuation and stopwords."""
    if not text:
        return set()
    tokens = re.findall(r"\b\w+\b", text.lower())
    return {t for t in tokens if t not in STOPWORDS}


def _ratio(numerator_tokens: set[str], denominator_tokens: set[str]) -> float:
    """|num ∩ denom| / |denom|, clamped to [0, 1]. Empty denominator -> 1.0."""
    if not denominator_tokens:
        return 1.0
    score = len(numerator_tokens & denominator_tokens) / len(denominator_tokens)
    return max(0.0, min(1.0, score))


class RAGASEvaluator:
    """
    Evaluates RAG pipeline outputs using RAGAS-inspired heuristics.

    All metrics use word overlap rather than LLM calls for simplicity.
    """

    def evaluate_faithfulness(self, answer: str, context: str) -> float:
        """|answer ∩ context| / |answer|. Returns 1.0 if answer is empty."""
        return _ratio(_tokenize(context), _tokenize(answer))

    def evaluate_relevance(self, answer: str, question: str) -> float:
        """|answer ∩ question| / |question|. Returns 1.0 if question is empty."""
        return _ratio(_tokenize(answer), _tokenize(question))

    def evaluate_completeness(self, answer: str, expected: str) -> float:
        """|answer ∩ expected| / |expected|. Returns 1.0 if expected is empty."""
        return _ratio(_tokenize(answer), _tokenize(expected))

    # -----------------------------------------------------------------------
    # Task 2b — Retrieval-side metrics (evaluate the GET-CONTEXT step)
    # -----------------------------------------------------------------------

    def evaluate_context_recall(self, contexts: list[str], expected: str) -> float:
        """Coverage of expected tokens by the UNION of retrieved chunks.

        Returns 1.0 if expected is empty.
        """
        union_tokens: set[str] = set()
        for chunk in contexts:
            union_tokens |= _tokenize(chunk)
        return _ratio(union_tokens, _tokenize(expected))

    def evaluate_context_precision(
        self,
        contexts: list[str],
        expected: str,
        relevance_threshold: float = 0.1,
    ) -> float:
        """Rank-aware Average Precision (AP@K).

        A chunk is relevant if |chunk ∩ expected| / |expected| >= threshold.
        AP@K = (1 / #relevant) * Σ_k [ Precision@k · relevant_k ].
        Returns 1.0 if expected is empty; 0.0 if no chunks or none relevant.
        """
        expected_tokens = _tokenize(expected)
        if not expected_tokens:
            return 1.0
        if not contexts:
            return 0.0

        relevant_count = 0
        precision_sum = 0.0
        for k, chunk in enumerate(contexts, start=1):
            coverage = len(_tokenize(chunk) & expected_tokens) / len(expected_tokens)
            if coverage >= relevance_threshold:
                relevant_count += 1
                precision_sum += relevant_count / k
        if relevant_count == 0:
            return 0.0
        return max(0.0, min(1.0, precision_sum / relevant_count))

    def run_full_eval(
        self,
        answer: str,
        question: str,
        context: str,
        expected: str,
        contexts: list[str] | None = None,
    ) -> EvalResult:
        """
        Run the three answer-side evaluations and, when ``contexts`` is
        supplied (even as an empty list), both retrieval-side evaluations.

        passed = all three scores >= 0.5.
        failure_type (first match wins):
            faithfulness < 0.3  → "hallucination"
            relevance < 0.3     → "irrelevant"
            completeness < 0.3  → "incomplete"
            otherwise if failed → "off_topic"
        """
        faith = self.evaluate_faithfulness(answer, context)
        rel = self.evaluate_relevance(answer, question)
        comp = self.evaluate_completeness(answer, expected)

        passed = faith >= 0.5 and rel >= 0.5 and comp >= 0.5

        failure_type: str | None = None
        if faith < 0.3:
            failure_type = "hallucination"
        elif rel < 0.3:
            failure_type = "irrelevant"
        elif comp < 0.3:
            failure_type = "incomplete"
        elif not passed:
            failure_type = "off_topic"

        recall: float | None = None
        precision: float | None = None
        if contexts is not None:
            recall = self.evaluate_context_recall(contexts, expected)
            precision = self.evaluate_context_precision(contexts, expected)

        return EvalResult(
            qa_pair=QAPair(
                question=question,
                expected_answer=expected,
                context=context,
                retrieved_contexts=list(contexts) if contexts else [],
            ),
            actual_answer=answer,
            faithfulness=faith,
            relevance=rel,
            completeness=comp,
            passed=passed,
            failure_type=failure_type,
            context_precision=precision,
            context_recall=recall,
        )


# ---------------------------------------------------------------------------
# Reranking helper (Bonus — Exercise 3.5)
# ---------------------------------------------------------------------------

def rerank_by_overlap(contexts: list[str], query: str) -> list[str]:
    """Minimal lexical reranker: sort chunks by word overlap with the query,
    most-overlapping first. Python's sort is stable, so ties keep their order.
    """
    query_tokens = _tokenize(query)
    return sorted(
        contexts,
        key=lambda c: len(_tokenize(c) & query_tokens),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Task 3 — LLM Judge
# ---------------------------------------------------------------------------

class LLMJudge:
    """
    Uses an LLM to score AI responses according to a rubric.
    """

    def __init__(self, judge_llm_fn: Callable[[str], str]) -> None:
        self.judge_llm_fn = judge_llm_fn

    def score_response(
        self,
        question: str,
        answer: str,
        rubric: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Score an AI response using the judge LLM (scores on a 0-1 scale).

        If the LLM response can't be parsed as JSON scores, each criterion
        falls back to 0.5.

        Returns:
            {"scores": dict[str, float], "reasoning": str}
        """
        criteria_text = "\n".join(f"- {name}: {desc}" for name, desc in rubric.items())
        prompt = (
            "You are an impartial judge. Score the answer for each criterion "
            "on a scale from 0 to 1.\n\n"
            f"Question: {question}\n\n"
            f"Answer: {answer}\n\n"
            f"Rubric:\n{criteria_text}\n\n"
            'Respond with JSON only, for example {"criterion_name": 0.8}.'
        )
        raw = self.judge_llm_fn(prompt)

        parsed: Any = None
        try:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            parsed = json.loads(match.group(0)) if match else None
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("scores"), dict):
            parsed = parsed["scores"]

        scores: dict[str, float] = {}
        for name in rubric:
            value = parsed.get(name) if isinstance(parsed, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                scores[name] = max(0.0, min(1.0, float(value)))
            else:
                scores[name] = 0.5
        return {"scores": scores, "reasoning": raw}

    def detect_bias(self, scores_batch: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Detect potential bias patterns in a batch of judge scores.

        positional_bias: the first response scores higher than all others.
        leniency_bias:   average score > 0.8.
        severity_bias:   average score < 0.3.
        """
        averages: list[float] = []
        for item in scores_batch:
            vals = list(item.get("scores", {}).values())
            if vals:
                averages.append(sum(vals) / len(vals))

        if not averages:
            return {
                "positional_bias": False,
                "leniency_bias": False,
                "severity_bias": False,
            }

        overall = sum(averages) / len(averages)
        positional = len(averages) >= 2 and averages[0] > max(averages[1:])
        return {
            "positional_bias": positional,
            "leniency_bias": overall > 0.8,
            "severity_bias": overall < 0.3,
        }


# ---------------------------------------------------------------------------
# Task 4 — Benchmark Runner
# ---------------------------------------------------------------------------

class BenchmarkRunner:
    """
    Runs a full evaluation benchmark.
    """

    @staticmethod
    def _avg(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def run(
        self,
        qa_pairs: list[QAPair],
        agent_fn: Callable[[str], str],
        evaluator: RAGASEvaluator,
    ) -> list[EvalResult]:
        """Run all QA pairs through the agent and evaluate each result."""
        results: list[EvalResult] = []
        for pair in qa_pairs:
            answer = agent_fn(pair.question)
            result = evaluator.run_full_eval(
                answer=answer,
                question=pair.question,
                context=pair.context,
                expected=pair.expected_answer,
                contexts=pair.retrieved_contexts,
            )
            result.qa_pair = pair  # preserve the original pair (needs metadata.id)
            results.append(result)
        return results

    def generate_report(self, results: list[EvalResult]) -> dict[str, Any]:
        """Generate an aggregate report from evaluation results."""
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        recalls = [r.context_recall for r in results if r.context_recall is not None]
        precisions = [
            r.context_precision for r in results if r.context_precision is not None
        ]

        failure_types: dict[str, int] = {}
        for r in results:
            if r.failure_type:
                failure_types[r.failure_type] = failure_types.get(r.failure_type, 0) + 1

        return {
            "total": total,
            "passed": passed,
            "pass_rate": passed / total if total else 0.0,
            "avg_faithfulness": self._avg([r.faithfulness for r in results]),
            "avg_relevance": self._avg([r.relevance for r in results]),
            "avg_completeness": self._avg([r.completeness for r in results]),
            "avg_context_recall": self._avg(recalls) if recalls else None,
            "avg_context_precision": self._avg(precisions) if precisions else None,
            "failure_types": failure_types,
        }

    def run_regression(self, new_results: list, baseline_results: list) -> dict:
        """Compare new results against a baseline.

        A regression is a metric average dropping by MORE than 0.05.
        """
        out: dict[str, Any] = {}
        regressions: list[str] = []
        for name in ("faithfulness", "relevance", "completeness"):
            new_avg = self._avg([getattr(r, name) for r in new_results])
            base_avg = self._avg([getattr(r, name) for r in baseline_results])
            out[f"new_avg_{name}"] = new_avg
            out[f"baseline_avg_{name}"] = base_avg
            # round() avoids float noise, e.g. 0.8 - 0.75 = 0.05000000000000004
            if round(base_avg - new_avg, 10) > 0.05:
                regressions.append(name)
        out["regressions"] = regressions
        out["passed"] = not regressions
        return out

    def identify_failures(
        self,
        results: list[EvalResult],
        threshold: float = 0.5,
    ) -> list[EvalResult]:
        """Return EvalResults where any score is below threshold."""
        return [
            r for r in results
            if min(r.faithfulness, r.relevance, r.completeness) < threshold
        ]


# ---------------------------------------------------------------------------
# Task 5 — Failure Analyzer
# ---------------------------------------------------------------------------

class FailureAnalyzer:
    """
    Analyzes failed evaluation results to identify patterns and suggest fixes.
    """

    def categorize_failures(self, failures: list[EvalResult]) -> dict[str, int]:
        """Count failures by failure_type."""
        counts: dict[str, int] = {}
        for f in failures:
            if f.failure_type:
                counts[f.failure_type] = counts.get(f.failure_type, 0) + 1
        return counts

    def find_root_cause(self, failure: EvalResult) -> str:
        """Suggest a root cause for a single failure based on its scores."""
        scores = {
            "faithfulness": failure.faithfulness,
            "relevance": failure.relevance,
            "completeness": failure.completeness,
        }
        low = [name for name, v in scores.items() if v < 0.5]
        if len(low) > 1:
            return "Multiple issues detected — review full pipeline"

        lowest = min(scores, key=lambda k: scores[k])
        if lowest == "faithfulness":
            return "Context is missing or irrelevant — improve retrieval"
        if lowest == "relevance":
            return "Answer does not address the question — improve prompt clarity"
        return (
            "Answer is missing key information — "
            "increase context window or improve generation"
        )

    def generate_improvement_log(self, failures: list, suggestions: list[str]) -> str:
        """Generate a Markdown table logging failures and improvement actions.

        Status is always "Open".
        """
        lines = [
            "| Failure ID | Type | Root Cause | Suggested Fix | Status |",
            "|------------|------|------------|---------------|--------|",
        ]
        for i, f in enumerate(failures):
            fix = suggestions[i] if i < len(suggestions) else ""
            lines.append(
                f"| F{i + 1:03d} | {f.failure_type or 'unknown'} | "
                f"{self.find_root_cause(f)} | {fix} | Open |"
            )
        return "\n".join(lines)

    def generate_improvement_suggestions(
        self, failures: list[EvalResult]
    ) -> list[str]:
        """Prioritized, actionable suggestions (most frequent failure type first).

        Returns at least 3 suggestions, or an empty list if failures is empty.
        """
        if not failures:
            return []

        by_type = self.categorize_failures(failures)
        mapping = {
            "hallucination": "Implement hallucination checker to filter unsupported claims",
            "irrelevant": "Clarify the system prompt so the answer addresses the question directly",
            "incomplete": "Add few-shot examples showing complete answers to improve completeness",
            "off_topic": "Improve intent detection to keep answers on the asked topic",
        }
        ordered = sorted(by_type, key=lambda t: by_type[t], reverse=True)
        suggestions = [mapping[t] for t in ordered if t in mapping]

        for extra in (
            "Increase chunk size in RAG pipeline to reduce context fragmentation",
            "Add a reranker so relevant chunks appear earlier in retrieved contexts",
            "Expand the golden dataset with new cases covering these failures",
        ):
            if len(suggestions) >= 3:
                break
            if extra not in suggestions:
                suggestions.append(extra)
        return suggestions


# ---------------------------------------------------------------------------
# Entry point for manual testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    qa_pairs = [
        QAPair(
            question="What is RAG?",
            expected_answer="RAG stands for Retrieval-Augmented Generation, which combines retrieval with text generation.",
            context="RAG is a technique that retrieves relevant documents and uses them to ground LLM generation.",
            metadata={"difficulty": "easy", "category": "definition"},
        ),
        QAPair(
            question="What is the capital of France?",
            expected_answer="Paris is the capital of France.",
            context="France is a country in Western Europe. Its capital city is Paris.",
            metadata={"difficulty": "easy", "category": "factual"},
        ),
        QAPair(
            question="Explain backpropagation and why it matters for training",
            expected_answer="Backpropagation is an algorithm for training neural networks by computing gradients efficiently, enabling deep learning models to learn from errors.",
            context="Neural networks learn through gradient descent. Backpropagation efficiently computes these gradients layer by layer.",
            metadata={"difficulty": "medium", "category": "explanation"},
        ),
        QAPair(
            question="Should I use RAG or fine-tuning for my chatbot?",
            expected_answer="It depends on the use case: RAG is better for frequently updated knowledge, fine-tuning for consistent style/behavior. Consider cost, latency, and data freshness.",
            context="RAG retrieves external documents at inference time. Fine-tuning modifies model weights during training.",
            metadata={"difficulty": "hard", "category": "comparison"},
        ),
        QAPair(
            question="What is the meaning of life?",
            expected_answer="This question is outside the scope of this system. I can help with AI and technology questions.",
            context="This is an AI assistant specialized in technology topics.",
            metadata={"difficulty": "adversarial", "category": "out_of_scope"},
        ),
    ]

    evaluator = RAGASEvaluator()
    runner = BenchmarkRunner()

    def mock_agent(question: str) -> str:
        """Simple mock agent for testing. Replace with your actual agent."""
        return f"Based on my knowledge: {question[:30]}... The answer involves key concepts."

    results = runner.run(qa_pairs, mock_agent, evaluator)
    report = runner.generate_report(results)
    print("=== Benchmark Report ===")
    for k, v in report.items():
        print(f"  {k}: {v}")

    failures = runner.identify_failures(results, threshold=0.5)
    print(f"\n=== Failures ({len(failures)}) ===")
    analyzer = FailureAnalyzer()

    categories = analyzer.categorize_failures(failures)
    print("Failure Categories:", categories)

    for f in failures:
        print(f"  Root cause: {analyzer.find_root_cause(f)}")

    suggestions = analyzer.generate_improvement_suggestions(failures)
    print("\nImprovement Suggestions:")
    for s in suggestions:
        print(f"  - {s}")

    log = analyzer.generate_improvement_log(failures, suggestions)
    print("\n=== Improvement Log ===")
    print(log)