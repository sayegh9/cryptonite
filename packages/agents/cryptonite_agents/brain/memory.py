"""Conversation memory with summary compaction (blueprint §4.2, A3.1).

The planner sees: the latest session summary + every turn after it (at most ``COMPACT_AFTER``
messages, then compaction runs) + the operator's new message. Compaction folds everything but the
last ``KEEP_MESSAGES`` into a new summary with the ``summarize`` role (cheap model, DEGRADE).

History goes into the prompt as a plain transcript rather than provider message objects, so it
survives reloads, restarts and model changes, and needs no provider types outside the backend.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from cryptonite_agents.brain.store import SessionStore
from cryptonite_agents.brain.widgets import describe_widget
from cryptonite_agents.gateway import CallContext, Gateway

logger = logging.getLogger("cryptonite.agents.brain.memory")

KEEP_MESSAGES = 12  # last N turns (user + assistant messages) kept verbatim after a compaction
COMPACT_AFTER = 20  # compact once this many messages follow the latest summary
MAX_MESSAGE_CHARS = 4000  # a single stored message is clipped to this in the prompt

SUMMARY_ROLE = "summarize"
SUMMARY_INSTRUCTIONS = (
    "You maintain the running summary of a conversation between a crypto trading operator and their AI "
    "trading assistant (the Brain). Merge the previous summary with the new turns into one summary of at most "
    "200 words. Keep: instruments and timeframes discussed, the operator's stated preferences and limits, "
    "analyses and their conclusions, every order card with its side, size, status and paper command id, "
    "open questions. Drop pleasantries. Write plain prose, no headings."
)


@dataclass
class BrainContext:
    summary: str | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)


def _clip(text: str) -> str:
    return text if len(text) <= MAX_MESSAGE_CHARS else text[:MAX_MESSAGE_CHARS] + " …[clipped]"


def _utc(value: Any) -> datetime | None:
    """An ISO timestamp as an aware UTC datetime (a naive one is read as UTC); None when absent or malformed."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def transcript(messages: list[dict[str, Any]], today: date | None = None) -> str:
    """Turns with UTC times. ``today`` (UTC) is the day the Brain is answering on: a turn off that day gets its date.
    Without it, the newest turn's day is used (compaction)."""
    speaker = {"user": "Operator", "assistant": "Brain", "system": "System"}
    stamps = [t for t in (_utc(m.get("created_at")) for m in messages) if t is not None]
    day = today if today is not None else (max(stamps).date() if stamps else None)
    lines = []
    for m in messages:
        stamp = _utc(m.get("created_at"))
        # The date too when a turn is not on that day, so an old price reads as old
        if stamp is None:
            when = ""
        else:
            when = f"[{stamp:%H:%M}] " if stamp.date() == day else f"[{stamp:%m-%d %H:%M}] "
        body = _clip(m.get("content") or "")
        extras = " ".join(describe_widget(w) for w in m.get("widgets") or [])
        lines.append(f"{when}{speaker.get(m['role'], m['role'])}: {body}{(' ' + extras) if extras else ''}".rstrip())
    return "\n".join(lines)


async def load_context(store: SessionStore, session_id: str) -> BrainContext:
    summary = await store.latest_summary(session_id)
    after = summary["covers_message_id"] if summary else 0
    return BrainContext(summary=summary["summary"] if summary else None,
                        turns=await store.messages(session_id, after_id=after))


def render_prompt(ctx: BrainContext, message: str, now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now(UTC)
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    parts = [f"## Now\n{now:%Y-%m-%d %H:%M} UTC"]  # the Brain has no other clock: old prices must read as old
    if ctx.summary:
        parts.append(f"## Conversation so far (summary)\n{ctx.summary}")
    if ctx.turns:
        parts.append(f"## Recent turns\n{transcript(ctx.turns, today=now.date())}")
    parts.append(f"## Operator's new message\n{message}")
    return "\n\n".join(parts)


async def compact(gateway: Gateway, store: SessionStore, session_id: str) -> bool:
    """Fold older turns into a new summary when enough have piled up. Returns True if it compacted."""
    ctx = await load_context(store, session_id)
    if len(ctx.turns) <= COMPACT_AFTER:
        return False
    fold = ctx.turns[:-KEEP_MESSAGES]
    prompt = (f"## Previous summary\n{ctx.summary or '(none yet)'}\n\n## New turns to merge\n{transcript(fold)}\n\n"
              "Write the updated summary.")
    res = await gateway.run(SUMMARY_ROLE, prompt, instructions=SUMMARY_INSTRUCTIONS,
                            context=CallContext(role=SUMMARY_ROLE, session_id=session_id))
    summary = str(res.output).strip()
    if not summary:
        return False
    await store.save_summary(session_id, summary, fold[-1]["id"], str(res.trace_id))
    return True
