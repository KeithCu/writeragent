"""
Metric for DSPy prompt optimization: judge-based score + token penalty.
Higher score = better. Use with track_usage=True so we can read token counts.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

# Token penalty weight: score -= LAMBDA * (total_tokens / 1000)
TOKEN_PENALTY_LAMBDA = 0.01
# Soft cap: winners should stay under ~2× the seed slice. Overflow is
# (len / (ratio * baseline) - 1); weight 1.0 zeros a perfect score at 4×.
SLICE_LENGTH_MAX_RATIO = 2.0
SLICE_LENGTH_PENALTY_LAMBDA = 1.0


def slice_length_penalty(
    slice_text: str,
    baseline_len: int,
    *,
    max_ratio: float = SLICE_LENGTH_MAX_RATIO,
    weight: float = SLICE_LENGTH_PENALTY_LAMBDA,
) -> float:
    """Penalty for a proposed slice longer than ``max_ratio`` × the seed."""
    if baseline_len <= 0 or max_ratio <= 0 or weight <= 0:
        return 0.0
    limit = max_ratio * baseline_len
    n = len(slice_text or "")
    if n <= limit:
        return 0.0
    return weight * (n / limit - 1.0)


def _get_total_tokens(pred: Any) -> int:
    """Extract total tokens from prediction (DSPy get_lm_usage or live attrs)."""
    direct = getattr(pred, "total_tokens", None)
    if isinstance(direct, int) and direct > 0:
        return direct
    try:
        usage = pred.get_lm_usage()
        if usage and isinstance(usage, dict):
            for model_data in usage.values():
                if isinstance(model_data, dict) and "total_tokens" in model_data:
                    return int(model_data["total_tokens"])
            for model_data in usage.values():
                if isinstance(model_data, dict):
                    p = int(model_data.get("prompt_tokens", 0))
                    c = int(model_data.get("completion_tokens", 0))
                    if p or c:
                        return p + c
    except Exception:
        pass
    return 0


def make_judge_metric(
    judge_lm: Any,
    token_penalty_lambda: float = TOKEN_PENALTY_LAMBDA,
    slice_length_max_ratio: float = SLICE_LENGTH_MAX_RATIO,
    slice_length_penalty_lambda: float = SLICE_LENGTH_PENALTY_LAMBDA,
) -> Callable[[Any, Any, Optional[Any]], float]:
    """
    Return a metric callable (example, pred, trace=None) -> float for MIPROv2.
    Uses shared score_with_judge from eval_core; applies token penalty.
    """
    from eval_core import _correctness_breakdown, _should_use_judge, score_with_judge
    from oracles import uses_llm_judge

    def metric(example: Any, pred: Any, trace: Optional[Any] = None) -> float:
        final_doc = getattr(pred, "final_document", None) or ""
        hard, _missing, _reject, _oracle = _correctness_breakdown(example, final_doc)
        category = getattr(example, "category", "structural")
        task_id = getattr(example, "task_id", "") or ""
        if (
            hard >= 1.0
            and uses_llm_judge(task_id, category)
            and _should_use_judge(example, judge_available=bool(judge_lm))
        ):
            score, _ = score_with_judge(
                judge_lm,
                document_content=getattr(example, "document_content", "") or "",
                user_question=getattr(example, "user_question", "") or "",
                model_answer=final_doc,
                gold_answer=getattr(example, "gold_document", "") or "N/A",
                rubric=getattr(example, "rubric", "") or "N/A",
                task_category=category,
            )
        else:
            score = hard
        total_tokens = _get_total_tokens(pred)
        penalty = token_penalty_lambda * (total_tokens / 1000.0)
        slice_text = getattr(pred, "slice_text", None)
        baseline_len = getattr(pred, "baseline_slice_len", None)
        if isinstance(slice_text, str) and isinstance(baseline_len, int):
            penalty += slice_length_penalty(
                slice_text,
                baseline_len,
                max_ratio=slice_length_max_ratio,
                weight=slice_length_penalty_lambda,
            )
        return max(0.0, score - penalty)

    return metric
