"""
Dev tool — NOT part of the running app, no user-facing surface. Produces
Ragas quality metrics (faithfulness, answer relevancy, context precision)
for a set of question/contexts/answer triples, for internal reporting
(e.g. a demo talking point: "here's how we measure RAG answer quality").

Usage:
    .venv/Scripts/python.exe scripts/eval_rag_ragas.py

By default scores the SAMPLE_CASES below (representative, hand-written
triples matching this project's actual domain/document style) so it runs
standalone with no DB/Qdrant dependency. To score REAL retrieval+generation
output instead, replace SAMPLE_CASES with real (question, contexts, answer)
triples pulled from app.services.rag.retrieval.retrieve() /
app.services.rag.generation.generate_answer() for a real project.
"""
import asyncio

from app.services.rag.evaluation import build_metrics, score_sample

# Representative of what real Qdrant chunks + a real generate_answer() call
# would look like for a DocFlow AI seeded demo tenant. Contexts explicitly
# name "DocFlow AI" the way a real indexed chunk from the app's own docs
# would (ragas' Faithfulness check is STRICT literal entailment — a context
# that describes a feature without naming the subject won't "support" an
# answer that attributes it to that subject, even when true; see
# evaluation.py usage notes / the investigation that caught this).
SAMPLE_CASES = [
    {
        "question": "What password hashing algorithm does DocFlow AI use, and how many iterations?",
        "contexts": [
            "DocFlow AI implements authentication in app/services/auth.py "
            "without a third-party auth library. DocFlow AI hashes "
            "passwords with PBKDF2-HMAC-SHA256 using 310,000 iterations and "
            "a per-user random salt, so a database leak does not expose "
            "them in plaintext.",
        ],
        "answer": (
            "DocFlow AI hashes passwords with PBKDF2-HMAC-SHA256, using "
            "310,000 iterations and a per-user random salt."
        ),
    },
    {
        "question": "What vector database does the RAG pipeline use, and does it support hybrid search?",
        "contexts": [
            "DocFlow AI's RAG pipeline uses Qdrant as the vector database "
            "for AI semantic search. A sparse BM25 vector is stored "
            "alongside the dense embedding vector for each chunk — this is "
            "hybrid search, combining keyword-style matching with "
            "meaning-based matching for better recall.",
        ],
        "answer": (
            "The RAG pipeline uses Qdrant, and yes — it supports hybrid "
            "search by storing a sparse BM25 vector alongside each chunk's "
            "dense embedding vector."
        ),
    },
    {
        # Deliberately unfaithful answer (invents a detail absent from the
        # context) to confirm faithfulness actually penalizes fabrication,
        # not just always scoring high.
        "question": "What embedding model does the RAG pipeline use?",
        "contexts": [
            "Each chunk is converted into a vector using fastembed with the "
            "BAAI/bge-small-en-v1.5 model.",
        ],
        "answer": (
            "The RAG pipeline uses OpenAI's text-embedding-3-large model, "
            "run through fastembed."
        ),
    },
]


async def main() -> None:
    metrics = build_metrics(reference_free=True)
    results = []
    for case in SAMPLE_CASES:
        scores = await score_sample(
            metrics,
            question=case["question"],
            contexts=case["contexts"],
            answer=case["answer"],
        )
        results.append({"question": case["question"], **scores})

    print(f"{'question':<70} {'faithfulness':>13} {'answer_relev':>13} {'ctx_precision':>13}")
    print("-" * 112)
    for r in results:
        q = r["question"][:67] + "..." if len(r["question"]) > 70 else r["question"]
        print(f"{q:<70} {r['faithfulness']:>13.2f} {r['answer_relevancy']:>13.2f} {r['context_precision']:>13.2f}")

    n = len(results)
    avg = lambda key: sum(r[key] for r in results) / n  # noqa: E731
    print("-" * 112)
    print(f"{'AVERAGE':<70} {avg('faithfulness'):>13.2f} {avg('answer_relevancy'):>13.2f} {avg('context_precision'):>13.2f}")


if __name__ == "__main__":
    asyncio.run(main())
