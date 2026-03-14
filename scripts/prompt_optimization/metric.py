"""
Metric for DSPy prompt optimization: judge-based score + token penalty.
Higher score = better. Use with track_usage=True so we can read token counts.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

# Token penalty weight: score -= LAMBDA * (total_tokens / 1000)
TOKEN_PENALTY_LAMBDA = 0.01


def _get_total_tokens(pred: Any) -> int:
    """Extract total tokens from prediction (DSPy get_lm_usage)."""
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
) -> Callable[[Any, Any, Optional[Any]], float]:
    """
    Return a metric callable (example, pred, trace=None) -> float for MIPROv2.
    Uses shared score_with_judge from eval_core; applies token penalty.
    """
    from eval_core import score_with_judge

    def metric(example: Any, pred: Any, trace: Optional[Any] = None) -> float:
        final_doc = getattr(pred, "final_document", None) or ""
        score, _ = score_with_judge(
            judge_lm,
            document_content=getattr(example, "document_content", "") or "",
            user_question=getattr(example, "user_question", "") or "",
            model_answer=final_doc,
            gold_answer=getattr(example, "gold_document", "") or "N/A",
            rubric=getattr(example, "rubric", "") or "N/A",
            task_category=getattr(example, "category", "structural"),
        )
        total_tokens = _get_total_tokens(pred)
        penalty = token_penalty_lambda * (total_tokens / 1000.0)
        return max(0.0, score - penalty)

    return metric
