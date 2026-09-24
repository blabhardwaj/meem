"""
Ragas-based RAG quality evaluation — a developer-facing tool, not part of
the running application. Used to produce metric numbers (faithfulness,
answer relevancy, context precision/recall) for a set of real or
representative question/answer/context triples, e.g. for a demo report.

Nothing here is imported by any router or agent — it's invoked directly via
scripts/eval_rag_ragas.py.

Two real packaging bugs in ragas==0.4.3 (current latest, as of this
writing) needed a workaround, both applied by `_patch_ragas_import_bugs()`
below, called once at module import:

1. ragas/llms/base.py unconditionally imports
   `langchain_community.chat_models.vertexai.ChatVertexAI` for an
   isinstance() check we never exercise (we don't use Vertex AI). That
   submodule no longer exists in the langchain-community version pip
   resolves alongside ragas (langchain-community is being sunset upstream
   and dropped its provider-specific integrations). Fixed by registering a
   placeholder module in sys.modules before ragas imports it.
2. ragas' native `provider="groq"` adapter (via the `instructor` library)
   references `instructor.Provider.GENAI`, which doesn't exist in the
   `instructor` version pip resolves alongside ragas. Sidestepped
   entirely — not by patching instructor, but by using `provider="openai"`
   with a plain `openai.OpenAI` client pointed at Groq's own
   OpenAI-compatible endpoint instead. Groq's API is OpenAI-compatible, so
   this path is unaffected by the broken provider-specific code and is
   also the better-tested path in ragas/instructor generally.

Both are upstream issues, not anything in this codebase — revisit this
shim if/when ragas ships a fix (check CHANGELOG for the `instructor`
Provider enum and the vertexai import before removing).
"""
import sys
import types


def _patch_ragas_import_bugs() -> None:
    if "langchain_community.chat_models.vertexai" in sys.modules:
        return
    shim = types.ModuleType("langchain_community.chat_models.vertexai")

    class ChatVertexAI:  # pragma: no cover — never instantiated, only imported
        pass

    shim.ChatVertexAI = ChatVertexAI
    sys.modules["langchain_community.chat_models.vertexai"] = shim


_patch_ragas_import_bugs()

from openai import AsyncOpenAI  # noqa: E402
from ragas.embeddings.base import BaseRagasEmbedding  # noqa: E402
from ragas.llms import llm_factory  # noqa: E402
from ragas.metrics.collections import (  # noqa: E402
    AnswerRelevancy,
    ContextPrecisionWithoutReference,
    ContextRecall,
    Faithfulness,
)

from app.config import GROQ_API_KEY, GROQ_MODEL  # noqa: E402
from app.services.rag.embedding import embed_dense  # noqa: E402

GROQ_OPENAI_COMPAT_BASE_URL = "https://api.groq.com/openai/v1"


def build_judge_llm(model: str | None = None, max_tokens: int | None = None):
    """
    The LLM ragas uses to JUDGE answers (faithfulness/relevancy checks) —
    a separate concern from the app's own generation LLM. Uses Groq's
    OpenAI-compatible endpoint (see module docstring, workaround #2) rather
    than the `groq` SDK client directly.

    Defaults to GROQ_MODEL, but accepts an override: GROQ_MODEL
    (openai/gpt-oss-20b) is a reasoning model, and ragas' structured-output
    judge calls (via instructor) were observed reproducibly returning a
    fully EMPTY completion ("failed_generation": "") for these prompts —
    consistent with the model spending its entire token budget on hidden
    <think> reasoning and leaving nothing for the actual structured JSON
    output, not the "genuinely stochastic json_validate_failed" flakiness
    this module's docstring originally documented for a different failure
    shape. A non-reasoning model sidesteps that budget fight entirely.

    max_tokens is also settable per-account: some alternate models on a
    given Groq account have a much smaller output-tokens-per-minute limit
    than instructor's ~1024-token default request (e.g. qwen/qwen3.8-27b
    capped at 1000 OTPM on this account) — pass a value under that cap.
    """
    client = AsyncOpenAI(base_url=GROQ_OPENAI_COMPAT_BASE_URL, api_key=GROQ_API_KEY)
    resolved_model = model or GROQ_MODEL
    kwargs: dict = {}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if "gpt-oss" in resolved_model.lower():
        kwargs["reasoning_effort"] = "low"
    return llm_factory(resolved_model, provider="openai", client=client, **kwargs)


class FastEmbedRagasEmbedding(BaseRagasEmbedding):
    """
    Wraps the app's own embed_dense() (fastembed, BAAI/bge-small-en-v1.5) —
    the SAME model/code path used to index real chunks into Qdrant — rather
    than adding a second embedding provider just for evaluation.
    """

    def embed_text(self, text: str, **kwargs) -> list[float]:
        return embed_dense([text])[0]

    def embed_texts(self, texts: list[str], **kwargs) -> list[list[float]]:
        return embed_dense(texts)

    async def aembed_text(self, text: str, **kwargs) -> list[float]:
        return self.embed_text(text)

    async def aembed_texts(self, texts: list[str], **kwargs) -> list[list[float]]:
        return self.embed_texts(texts)


def build_metrics(reference_free: bool = True, judge_model: str | None = None, judge_max_tokens: int | None = None):
    """
    Returns the standard metric set, wired to the Groq judge LLM and the
    app's own embeddings.

    reference_free: if True (default), only include metrics that don't
        need a hand-written ground-truth answer (Faithfulness,
        AnswerRelevancy, ContextPrecisionWithoutReference) — the practical
        choice when you haven't built a golden answer set yet. Set False
        to also include ContextRecall, which needs `reference` per sample.
    judge_model / judge_max_tokens: overrides for the judge LLM — see
        build_judge_llm().
    """
    llm = build_judge_llm(judge_model, judge_max_tokens)
    embeddings = FastEmbedRagasEmbedding()

    metrics = {
        "faithfulness": Faithfulness(llm=llm),
        "answer_relevancy": AnswerRelevancy(llm=llm, embeddings=embeddings),
        "context_precision": ContextPrecisionWithoutReference(llm=llm),
    }
    if not reference_free:
        metrics["context_recall"] = ContextRecall(llm=llm)
    return metrics


_MAX_METRIC_RETRIES = 3


async def _ascore_with_retries(metric, **kwargs):
    """
    Groq's structured-output judge calls (via `instructor`) fail
    intermittently with `openai.BadRequestError: json_validate_failed` on
    this project's model — confirmed empirically to be genuinely
    stochastic (an identical call sometimes fails, sometimes succeeds), not
    tied to particular input content. Retrying is the practical fix ragas
    itself doesn't apply at this layer.
    """
    last_exc: Exception = RuntimeError("_ascore_with_retries: unreachable")
    for attempt in range(_MAX_METRIC_RETRIES):
        try:
            return await metric.ascore(**kwargs)
        except Exception as exc:  # noqa: BLE001 — deliberately broad, see docstring
            last_exc = exc
    raise last_exc


async def score_sample(
    metrics: dict,
    *,
    question: str,
    contexts: list[str],
    answer: str,
    reference: str | None = None,
) -> dict[str, float]:
    """
    Scores one question/contexts/answer triple against every metric in
    `metrics`, retrying each metric up to _MAX_METRIC_RETRIES times on
    failure (see _ascore_with_retries). `reference` (a ground-truth answer)
    is required only if `metrics` includes context_recall — see
    build_metrics(reference_free=False).
    """
    scores: dict[str, float] = {}
    for name, metric in metrics.items():
        if name == "context_recall":
            result = await _ascore_with_retries(metric, user_input=question, retrieved_contexts=contexts, reference=reference)
        elif name == "answer_relevancy":
            result = await _ascore_with_retries(metric, user_input=question, response=answer)
        else:
            result = await _ascore_with_retries(metric, user_input=question, response=answer, retrieved_contexts=contexts)
        scores[name] = result.value
    return scores
