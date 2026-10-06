"""B2 (F-C2): re-score and regenerate reflections written with an inverted outcome.

Before B2, a BUY-like verdict not spelled "BUY" (Overweight, ...) was scored as a short, so its
reflection stored the negated outcome (``actual_outcome_pnl``), the negated alpha, the opposite
WIN/LOSS tag and a lesson drawn from the wrong result. This script compares every 24h reflection with
the corrected scoring (``outcomes.signed_outcome``, which reads the verdict through
``normalize_verdict``) and classifies it:

- ``inverted``: the stored outcome is the corrected one negated. The outcome and alpha are re-scored
  (the old alpha was ``-(outcome - benchmark)``, so the corrected alpha is its negation; no lake read
  is needed), and the lesson is regenerated through the gateway's **local tier** and re-embedded;
- ``retag``: the outcome is right but the WIN/LOSS/FLAT tag is not: only the tag is fixed;
- ``unexplained``: neither; reported, never touched.

Dry run by default (prints what it would change). ``--apply`` writes each fix with a guard on the old
value, so a second run finds nothing to do (idempotent). The reviewer model must be local
(``ollama:...``); it is pinned, so it never falls back to a paid cloud model. The lesson is embedded
with the ``embed`` route's model, like every other reflection, so retrieval keeps comparing like with like;
``--apply`` refuses to run when that route resolves to a cloud model.

    docker compose -f infra/docker-compose.yml run --rm --no-deps \\
        -e OLLAMA_BASE_URL=http://172.17.0.1:11434/v1 test \\
        uv run python scripts/regenerate_inverted_reflections.py            # dry run
    ... uv run python scripts/regenerate_inverted_reflections.py --apply    # write
"""

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
from cryptonite_agents.gateway import BudgetGuard, Gateway
from cryptonite_agents.gateway.local_tier import is_local

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "debate_worker"))
from outcomes import signed_outcome  # noqa: E402
from reflection import (  # noqa: E402
    BENCHMARK,
    REFLECTION_HORIZON,
    REVIEWER,
    outcome_tag,
    reflection_prompt,
)

DEFAULT_MODEL = "ollama:gemma4:12b-it-qat"
TOLERANCE = 2e-6  # actual_outcome_pnl / alpha_vs_benchmark are stored rounded to 6 decimals

SELECT = """
    SELECT r.id, r.actual_outcome_pnl, r.alpha_vs_benchmark, r.outcome,
           pd.run_id, pd.instrument_id, pd.recommendation, pd.reasoning, o.return_pct, o.mfe, o.mae
      FROM reflections r
      JOIN portfolio_decisions pd ON pd.run_id = r.run_id
      JOIN ai_decision_outcomes o ON o.decision_id = pd.id AND o.horizon = $1
     ORDER BY r.created_at
"""


@dataclass(frozen=True)
class Fix:
    reflection_id: Any
    kind: str  # inverted | retag | unexplained
    old_pnl: Decimal | None
    outcome: float | None = None
    alpha: float | None = None
    tag: str | None = None
    detail: str = ""


def classify(row) -> Fix | None:
    """The fix one reflection needs under the corrected scoring, or None when it is consistent."""
    old_pnl = row["actual_outcome_pnl"]
    try:
        expected = signed_outcome(row["recommendation"], float(row["return_pct"]))
    except ValueError as e:
        return Fix(row["id"], "unexplained", old_pnl, detail=str(e))
    if old_pnl is None:
        return Fix(row["id"], "unexplained", old_pnl, detail="no stored outcome")
    stored = float(old_pnl)
    tag = outcome_tag(round(expected, 6))
    if abs(stored - expected) <= TOLERANCE:
        if row["outcome"] == tag:
            return None
        return Fix(row["id"], "retag", old_pnl, outcome=stored, alpha=_float(row["alpha_vs_benchmark"]), tag=tag,
                   detail=f"tag {row['outcome']} -> {tag}")
    if abs(stored + expected) <= TOLERANCE:
        if row["instrument_id"].startswith(BENCHMARK):
            alpha = expected  # for the benchmark itself the alpha is the outcome
        elif row["alpha_vs_benchmark"] is None:
            return Fix(row["id"], "unexplained", old_pnl, detail="inverted outcome without a stored alpha")
        else:
            alpha = -float(row["alpha_vs_benchmark"])
        return Fix(row["id"], "inverted", old_pnl, outcome=expected, alpha=alpha, tag=tag,
                   detail=f"{row['recommendation']}: outcome {stored:+.6f} -> {expected:+.6f}, tag {row['outcome']} -> {tag}")
    return Fix(row["id"], "unexplained", old_pnl, detail=f"stored {stored:+.6f}, corrected {expected:+.6f}")


def _float(value) -> float | None:
    return float(value) if value is not None else None


async def find_fixes(conn) -> tuple[list[Fix], dict[Any, Any]]:
    """Every reflection that disagrees with the corrected scoring, plus the rows by reflection id."""
    rows = await conn.fetch(SELECT, REFLECTION_HORIZON)
    fixes = [f for f in map(classify, rows) if f is not None]
    return fixes, {r["id"]: r for r in rows}


async def apply_fix(conn, gateway, fix: Fix, row, model: str, embed_model: str) -> bool:
    """Write one fix; False when the row changed since it was read (another run fixed it). The reviewer
    and the embedder are both pinned (``model`` / ``embed_model``), so neither can fall back to a cloud model."""
    if fix.kind == "retag":
        status = await conn.execute("UPDATE reflections SET outcome = $2 WHERE id = $1 AND actual_outcome_pnl = $3",
                                    fix.reflection_id, fix.tag, fix.old_pnl)
        return status.endswith(" 1")
    lesson = str((await REVIEWER.run(gateway, reflection_prompt(row, fix.outcome, fix.alpha),
                                     config={"model_name": model})).output)
    embedding_str = embedding_model = None
    try:
        (vector,), embedding_model = await gateway.embed_tagged([lesson], kind="document", model_override=embed_model)
        embedding_str = f"[{','.join(map(str, vector))}]"
    except Exception as e:  # stored without a vector, like ReflectionEngine: retrieval falls back to recency
        print(f"  embedding failed, stored without a vector: {e}")
        embedding_model = None
    status = await conn.execute(
        """UPDATE reflections SET actual_outcome_pnl = $2, alpha_vs_benchmark = $3, outcome = $4, lessons_learned = $5,
                                  embedding = $6, embedding_model = $7
            WHERE id = $1 AND actual_outcome_pnl = $8""",
        fix.reflection_id, round(fix.outcome, 6), round(fix.alpha, 6), fix.tag, lesson, embedding_str, embedding_model,
        fix.old_pnl)
    return status.endswith(" 1")


def _default_gateway(pool):
    return Gateway(pool=pool, budget=BudgetGuard(pool=pool))


async def main(argv: list[str] | None = None, make_gateway=_default_gateway) -> int:
    """Returns the number of reflections written (0 on a dry run). ``make_gateway`` is for tests."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="write the fixes (default: dry run)")
    p.add_argument("--model", default=DEFAULT_MODEL, help="local reviewer model (ollama:...)")
    args = p.parse_args(argv)

    if not is_local(args.model):
        raise SystemExit(f"--model must be a local (ollama:) model, got {args.model}")
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            fixes, rows = await find_fixes(conn)
        for f in fixes:
            print(f"{f.kind:<11} reflection {f.reflection_id} (run {rows[f.reflection_id]['run_id']}): {f.detail}")
        todo = [f for f in fixes if f.kind != "unexplained"]
        print(f"{len(todo)} to fix, {len(fixes) - len(todo)} unexplained (left alone)")
        if not args.apply or not todo:
            if todo:
                print("dry run: nothing written; re-run with --apply")
            return 0
        gateway = make_gateway(pool)
        # Lessons are embedded with the embed route's model so retrieval compares like with like; that
        # model must be local too, or nothing is written.
        embed_model = (await gateway.route("embed")).primary_model
        if not is_local(embed_model):
            raise SystemExit(f"the embed route resolves to {embed_model}, not a local (ollama:) model; nothing written")
        written = 0
        for f in todo:
            async with pool.acquire() as conn:
                ok = await apply_fix(conn, gateway, f, rows[f.reflection_id], args.model, embed_model)
            written += ok
            print(f"{'fixed' if ok else 'skipped (changed meanwhile)'}: {f.reflection_id}")
        print(f"{written} reflection(s) written")
        return written
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
