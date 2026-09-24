"""
Ragas RAG-quality evaluation against REAL Lendora Financial data (SME
FlexCredit Launch project) — same tool as eval_rag_ragas_live.py, pointed at
the real demo tenant instead of the old Lumen Retail seed data.

Two separate things are measured here, on purpose:
  1. IN-CORPUS questions -> scored with ragas (faithfulness, answer_relevancy,
     context_precision) — how good is the answer when the knowledge base
     genuinely has the information.
  2. OUT-OF-CORPUS questions -> NOT scored with ragas (there's no context to
     judge faithfulness against). Checked directly instead: does retrieve()
     return zero usable chunks / does the agent honestly decline, rather
     than inventing an answer. This is the concrete evidence for "the
     system doesn't hallucinate outside its own knowledge corpus."

Usage:
    PYTHONPATH=. .venv/Scripts/python.exe scripts/eval_rag_ragas_lendora.py
"""
import asyncio
import uuid

from sqlalchemy import text

from app.database import SessionLocal
from app.services.rag.evaluation import build_metrics, score_sample
from app.services.rag.generation import generate_answer
from app.services.rag.retrieval import retrieve

TENANT_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")  # Lendora Financial
PROJECT_ID = uuid.UUID("d438e33b-7d89-4ca9-bc07-b2e9fe9c0e6c")  # SME FlexCredit Launch
USER_ID = uuid.UUID("2419fccf-83b9-4bbc-b3fb-e17c43d3e7cb")  # orgadmin@lendorafinancial.com

# Real business questions targeting this project's actual seeded documents.
IN_CORPUS_QUESTIONS = [
    "What eligibility criteria does an SME need to meet for FlexCredit, and how is the credit limit decided?",
    "What's the target gross NPA ceiling for SME FlexCredit?",
    "Is the SME FlexCredit product ready for go-live?",
    "What happens when a borrower defaults — what are the repayment and recovery terms?",
    "How quickly must a fraud alert be escalated?",
]

# Deliberately NOT answerable from anything in this project's documents —
# checked separately, not run through ragas (see module docstring).
OUT_OF_CORPUS_QUESTIONS = [
    "What is Lendora's policy on cryptocurrency staking rewards for employees?",
    "What was Lendora's quarterly revenue for Q3 2025?",
]


def _build_sources(db, chunks):
    stage_ids = {c.stage_id for c in chunks}
    stage_names = {}
    if stage_ids:
        rows = db.execute(
            text("SELECT stage_id, name FROM stages WHERE stage_id = ANY(:ids)"),
            {"ids": list(stage_ids)},
        ).fetchall()
        stage_names = {r[0]: r[1] for r in rows}

    sources = []
    for i, ch in enumerate(chunks, start=1):
        stage_name = stage_names.get(ch.stage_id, "Unknown")
        sources.append({
            "label": f"Source {i} — {stage_name} stage",
            "section": ch.section_title,
            "text": ch.chunk_text,
        })
    return sources


async def main() -> None:
    db = SessionLocal()
    db.info["tenant_id"] = str(TENANT_ID)
    db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(TENANT_ID)})

    cases = []
    print("=" * 90)
    print("IN-CORPUS questions (scored with ragas)")
    print("=" * 90)
    try:
        for i, question in enumerate(IN_CORPUS_QUESTIONS, start=1):
            print(f"[{i}/{len(IN_CORPUS_QUESTIONS)}] retrieving: {question}", flush=True)
            result = retrieve(db, USER_ID, PROJECT_ID, question)
            if not result.chunks:
                print(f"  [skip] no chunks retrieved", flush=True)
                continue
            print(f"  retrieved {len(result.chunks)} chunks, generating answer...", flush=True)
            sources = _build_sources(db, result.chunks)
            answer = generate_answer(question, sources)
            print(f"  answer: {answer[:120]}{'...' if len(answer) > 120 else ''}", flush=True)
            cases.append({
                "question": question,
                "contexts": [s["text"] for s in sources],
                "answer": answer,
            })

        print()
        print("=" * 90)
        print("OUT-OF-CORPUS questions (checked directly, NOT scored with ragas)")
        print("=" * 90)
        oo_results = []
        for i, question in enumerate(OUT_OF_CORPUS_QUESTIONS, start=1):
            print(f"[{i}/{len(OUT_OF_CORPUS_QUESTIONS)}] retrieving: {question}", flush=True)
            result = retrieve(db, USER_ID, PROJECT_ID, question)
            if not result.chunks:
                print(f"  -> retrieve() returned ZERO chunks (correctly nothing relevant found)", flush=True)
                oo_results.append({"question": question, "chunks": 0, "answer": None})
                continue
            sources = _build_sources(db, result.chunks)
            answer = generate_answer(question, sources)
            print(f"  -> retrieved {len(result.chunks)} chunk(s) anyway; answer: {answer[:200]}", flush=True)
            oo_results.append({"question": question, "chunks": len(result.chunks), "answer": answer})
    finally:
        db.close()

    if not cases:
        print("No in-corpus cases retrieved anything — nothing to score.")
        return

    print()
    print(f"Scoring {len(cases)} in-corpus case(s) with ragas...", flush=True)
    # GROQ_MODEL (openai/gpt-oss-20b) with instructor's default ~1024
    # max_tokens returned a fully EMPTY completion (all budget spent on
    # hidden reasoning). qwen/qwen3.8-27b avoided that but is hard-capped at
    # 1000 output tokens PER REQUEST on this account, too small for the
    # verdict-generation step on a multi-claim answer (truncated JSON).
    # Back to GROQ_MODEL, but with enough max_tokens headroom for reasoning
    # tokens AND the real structured output to both fit.
    metrics = build_metrics(reference_free=True, judge_max_tokens=4000)
    results = []
    for i, case in enumerate(cases, start=1):
        print(f"[{i}/{len(cases)}] scoring: {case['question']}", flush=True)
        scores = await score_sample(
            metrics,
            question=case["question"],
            contexts=case["contexts"],
            answer=case["answer"],
        )
        print(f"  -> {scores}", flush=True)
        results.append({"question": case["question"], "answer": case["answer"], **scores})

    print()
    print("=" * 90)
    print("PER-QUESTION RESULTS")
    print("=" * 90)
    for r in results:
        print(f"Q: {r['question']}")
        print(f"A: {r['answer'][:200]}{'...' if len(r['answer']) > 200 else ''}")
        print(f"   faithfulness={r['faithfulness']:.2f}  answer_relevancy={r['answer_relevancy']:.2f}  context_precision={r['context_precision']:.2f}")
        print()

    n = len(results)
    avg = lambda key: sum(r[key] for r in results) / n  # noqa: E731
    print("-" * 90)
    print(f"AVERAGE over {n} real in-corpus question(s):")
    print(f"  faithfulness      = {avg('faithfulness'):.3f}")
    print(f"  answer_relevancy  = {avg('answer_relevancy'):.3f}")
    print(f"  context_precision = {avg('context_precision'):.3f}")
    print()
    print("=" * 90)
    print("OUT-OF-CORPUS SUMMARY")
    print("=" * 90)
    for r in oo_results:
        status = "correctly found NOTHING" if r["chunks"] == 0 else f"retrieved {r['chunks']} chunk(s)"
        print(f"Q: {r['question']}\n  -> {status}")
        if r["answer"]:
            print(f"  answer given: {r['answer'][:200]}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
