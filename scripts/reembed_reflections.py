"""A6.4: re-embed every reflection with a new embedding model and switch the ``embed`` route, in one
transaction.

Vectors from different models are not comparable. This job embeds all lessons with ``--model`` first
(outside the transaction; a failure changes nothing), then, in one transaction, rewrites every row's
``embedding`` + ``embedding_model`` and points ``ai_model_routes.embed`` at the new model. Readers
filter by ``embedding_model``, so no query ever compares vectors across models, even mid-switch.

    docker compose -f infra/docker-compose.yml run --rm --no-deps \\
        -e OLLAMA_BASE_URL=http://172.17.0.1:11434/v1 test \\
        uv run python scripts/reembed_reflections.py --model ollama:embeddinggemma
"""

import argparse
import asyncio
import os

import asyncpg
from cryptonite_agents.gateway import BudgetGuard, Gateway


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--batch", type=int, default=32)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    gw = Gateway(budget=BudgetGuard(default_cap_usd=10))
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch("SELECT id, lessons_learned FROM reflections ORDER BY created_at")
        vectors: dict = {}
        for i in range(0, len(rows), args.batch):
            chunk = rows[i:i + args.batch]
            embedded, model = await gw.embed_tagged([r["lessons_learned"] for r in chunk], kind="document",
                                                    model_override=args.model)
            if len(embedded) != len(chunk) or any(len(v) != 768 for v in embedded):
                raise SystemExit(f"{model} returned {len(embedded)} vectors / dims {set(map(len, embedded))}; "
                                 "reflections.embedding is vector(768)")
            vectors.update({r["id"]: v for r, v in zip(chunk, embedded, strict=True)})
        print(f"embedded {len(vectors)} reflections with {args.model}")
        if args.dry_run:
            return
        async with conn.transaction():
            # writers wait while we switch; rows that arrived after the first pass are embedded too
            await conn.execute("LOCK TABLE reflections IN SHARE ROW EXCLUSIVE MODE")
            late = await conn.fetch("SELECT id, lessons_learned FROM reflections WHERE NOT (id = ANY($1::uuid[]))",
                                    list(vectors))
            if late:
                embedded, _ = await gw.embed_tagged([r["lessons_learned"] for r in late], kind="document",
                                                    model_override=args.model)
                vectors.update({r["id"]: v for r, v in zip(late, embedded, strict=True)})
            for rid, v in vectors.items():
                await conn.execute("UPDATE reflections SET embedding = $1, embedding_model = $2 WHERE id = $3",
                                   f"[{','.join(map(str, v))}]", args.model, rid)
            await conn.execute(
                """INSERT INTO ai_model_routes (role, primary_model, certified_primary) VALUES ('embed', $1, true)
                   ON CONFLICT (role) DO UPDATE SET primary_model = EXCLUDED.primary_model,
                       certified_primary = true, updated_at = now()""", args.model)
        print(f"embed route -> {args.model}; {len(vectors)} reflections re-embedded in one transaction")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
