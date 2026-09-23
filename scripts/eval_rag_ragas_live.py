"""
Dev tool — same purpose as eval_rag_ragas.py, but scores REAL output from
the actual retrieve() + generate_answer() pipeline against a real seeded
demo project (Lumen Retail / "Product Launch Q1"), instead of hand-written
sample text. Requires a live DB + Qdrant connection.

Usage:
    PYTHONPATH=. .venv/Scripts/python.exe scripts/eval_rag_ragas_live.py
"""
import asyncio
import uuid

from sqlalchemy import text

from app.database import SessionLocal
from app.services.rag.evaluation import build_metrics, score_sample
from app.services.rag.generation import generate_answer
from app.services.rag.retrieval import retrieve

TENANT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")  # Lumen Retail
PROJECT_ID = uuid.UUID("d1c99383-602d-4283-a9f3-797021b3b720")  # Product Launch Q1
USER_ID = uuid.UUID("3368a186-7733-49e7-b2a6-87fe9ffa89a8")  # rohan.iyer@lumenretail.com, org_admin

# Real business questions targeting this project's actual seeded documents
# (checkout redesign requirements, GTM plan, pricing strategy, payment
# gateway failover test plan, release readiness checklist, etc.)
QUESTIONS = [
    "What test plan exists for payment gateway failover?",
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
    try:
        for i, question in enumerate(QUESTIONS, start=1):
            print(f"[{i}/{len(QUESTIONS)}] retrieving: {question}", flush=True)
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
    finally:
        db.close()

    if not cases:
        print("No cases retrieved anything — nothing to score.")
        return

    print(f"\nScoring {len(cases)} case(s) with ragas...", flush=True)
    metrics = build_metrics(reference_free=True)
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
    for r in results:
        print(f"Q: {r['question']}")
        print(f"A: {r['answer'][:200]}{'...' if len(r['answer']) > 200 else ''}")
        print(f"   faithfulness={r['faithfulness']:.2f}  answer_relevancy={r['answer_relevancy']:.2f}  context_precision={r['context_precision']:.2f}")
        print()

    n = len(results)
    avg = lambda key: sum(r[key] for r in results) / n  # noqa: E731
    print("-" * 90)
    print(f"AVERAGE over {n} real question(s): faithfulness={avg('faithfulness'):.2f}  answer_relevancy={avg('answer_relevancy'):.2f}  context_precision={avg('context_precision'):.2f}")


if __name__ == "__main__":
    asyncio.run(main())
