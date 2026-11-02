"""Pure helpers shared by the provider shims (no plugin imports, so the LibreHarper and LibrePy bundles can ship it)."""

from __future__ import annotations

from typing import Any


def _dict_parts(content: Any) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
    """Content blocks that are dicts. Null, a string, or a non-dict part is skipped.

    Calling ``.get`` on sync ``content: null`` or a non-dict part raises
    TypeError / AttributeError. The Google image path already treats null
    content as no blocks.
    """
    if not isinstance(content, list):
        return []
    return [part for part in content if isinstance(part, dict)]


def _system_text(content: Any) -> str:  # pyright: ignore[reportUnusedFunction]
    """Text of one system or developer message. List parts keep their order."""
    if isinstance(content, list):
        return "\n\n".join(str(p.get("text", "")) for p in _dict_parts(content) if p.get("type") == "text")
    return str(content or "")


def _normalize_usage(raw: Any) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
    """Copy provider usage and add the OpenAI token names callers already read.

    Librarian and PPT host use ``prompt_tokens`` / ``completion_tokens``.
    Anthropic/Responses sends ``input_tokens`` / ``output_tokens``. Cache fields stay.
    """
    if not isinstance(raw, dict) or not raw:
        return {}
    usage = dict(raw)
    if "prompt_tokens" not in usage and "input_tokens" in usage:
        usage["prompt_tokens"] = usage["input_tokens"]
    if "completion_tokens" not in usage and "output_tokens" in usage:
        usage["completion_tokens"] = usage["output_tokens"]
    return usage


def _openai_model_leaf(model_name: str | None) -> str:  # pyright: ignore[reportUnusedFunction]
    """Strip ft: and vendor prefix (e.g. openai/...) from a model identifier.

    Fine-tune ids are ft:<base>:<org>:<suffix>:<id>. The base is the field after
    ft:, including when a provider prefix sits in front (openai/ft:...). A '/'
    inside that base (ft:openai/gpt-5-mini:...) is stripped afterwards.
    https://developers.openai.com/api/reference/resources/fine_tuning/subresources/jobs/methods/create
    """
    name = str(model_name or "").strip().lower()
    ft_at = name.find("ft:")
    if ft_at != -1 and (ft_at == 0 or name[ft_at - 1] in "/:"):
        base = name[ft_at + 3 :].split(":", 1)[0]
        if base:
            name = base
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name
