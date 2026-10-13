"""Session storage for the Brain (migration 024: ai_sessions, ai_messages, ai_session_summaries).

``PgSessionStore`` runs as the ``cryptonite_ai_telemetry`` role (SELECT/INSERT/UPDATE, no DELETE);
model-callable SQL runs as ``cryptonite_agent_ro``, which has no access to these tables.
``MemorySessionStore`` has the same interface for tests and for a worker without a database.
"""

import datetime
import json
import uuid
from typing import Any, Protocol


class SessionStore(Protocol):
    async def create_session(self, operator_id: str, title: str | None = None) -> str: ...
    async def get_session(self, session_id: str, operator_id: str) -> dict[str, Any] | None: ...
    async def list_sessions(self, operator_id: str, limit: int = 20, *, query: str | None = None,
                            archived: bool = False, before: str | None = None) -> list[dict[str, Any]]: ...
    async def update_session(self, session_id: str, operator_id: str, *, title: str | None = None,
                             archived: bool | None = None) -> dict[str, Any] | None: ...
    async def append_message(self, session_id: str, role: str, content: str,
                             widgets: list[dict[str, Any]] | None = None, trace_id: str | None = None) -> int: ...
    async def messages(self, session_id: str, after_id: int = 0) -> list[dict[str, Any]]: ...
    async def latest_summary(self, session_id: str) -> dict[str, Any] | None: ...
    async def save_summary(self, session_id: str, summary: str, covers_message_id: int,
                           trace_id: str | None = None) -> None: ...
    async def update_widget(self, session_id: str, widget_id: str, patch: dict[str, Any],
                            expect_status: str | None = None) -> bool: ...


def _valid_uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _message_row(row: Any) -> dict[str, Any]:
    widgets = row["widgets"]
    if isinstance(widgets, str):
        widgets = json.loads(widgets)
    return {
        "id": row["id"], "role": row["role"], "content": row["content"], "widgets": widgets or [],
        "trace_id": str(row["trace_id"]) if row["trace_id"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def _patch(widgets: list[dict[str, Any]], widget_id: str, patch: dict[str, Any], expect_status: str | None) -> bool:
    changed = False
    for w in widgets:
        if w.get("id") == widget_id and (expect_status is None or w.get("status") == expect_status):
            w.update(patch)
            changed = True
    return changed


def _session_row(row: Any) -> dict[str, Any]:
    out = {"id": str(row["id"]), "title": row["title"], "created_at": row["created_at"].isoformat(),
           "updated_at": row["updated_at"].isoformat()}
    if "archived" in row.keys():
        out["archived"] = bool(row["archived"])
    return out


PREVIEW_CHARS = 160
# Roles a person reads as the conversation; system lines (approved cards, notes) are not previews
CONVERSATION_ROLES = ("user", "assistant")


def preview_text(content: str | None) -> str:
    """One line of a message for the history list: whitespace collapsed, clipped with an ellipsis."""
    text = " ".join((content or "").split())
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 1].rstrip() + "…"


def session_cursor(session: dict[str, Any]) -> str:
    """Keyset cursor for the next page: the last row's (updated_at, id)."""
    return f"{session['updated_at']}|{session['id']}"


def parse_cursor(cursor: str | None) -> tuple[datetime.datetime, uuid.UUID] | None:
    """``updated_at|id`` back into its parts; a malformed cursor is no cursor (first page)."""
    if not cursor or "|" not in cursor:
        return None
    ts, _, sid = cursor.rpartition("|")
    try:
        return datetime.datetime.fromisoformat(ts), uuid.UUID(sid)
    except ValueError:
        return None


def like_pattern(query: str) -> str:
    """A substring ILIKE pattern with the operator's % and _ taken literally."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class PgSessionStore:
    def __init__(self, pool) -> None:
        self.pool = pool

    async def create_session(self, operator_id: str, title: str | None = None) -> str:
        sid = await self.pool.fetchval(
            "INSERT INTO ai_sessions (operator_id, title) VALUES ($1, $2) RETURNING id", operator_id, title)
        return str(sid)

    async def get_session(self, session_id: str, operator_id: str) -> dict[str, Any] | None:
        sid = _valid_uuid(session_id)
        if sid is None:
            return None
        row = await self.pool.fetchrow(
            "SELECT id, title, created_at, updated_at FROM ai_sessions WHERE id = $1 AND operator_id = $2",
            sid, operator_id)
        return _session_row(row) if row else None

    async def list_sessions(self, operator_id: str, limit: int = 20, *, query: str | None = None,
                            archived: bool = False, before: str | None = None) -> list[dict[str, Any]]:
        """Newest activity first. ``query`` matches the title or any message (case-insensitive substring);
        ``before`` is the previous page's cursor. Each row carries its message count and last line."""
        cursor = parse_cursor(before)
        q = (query or "").strip()
        rows = await self.pool.fetch(
            r"""SELECT s.id, s.title, s.archived, s.created_at, s.updated_at,
                      (SELECT count(*) FROM ai_messages m
                        WHERE m.session_id = s.id AND m.role = ANY($7::text[])) AS message_count,
                      last.role AS last_role, last.content AS last_content
                 FROM ai_sessions s
                 LEFT JOIN LATERAL (
                      SELECT role, content FROM ai_messages m
                       WHERE m.session_id = s.id AND m.role = ANY($7::text[])
                       ORDER BY m.id DESC LIMIT 1) last ON true
                WHERE s.operator_id = $1 AND s.archived = $2
                  AND ($3::timestamptz IS NULL OR (s.updated_at, s.id) < ($3::timestamptz, $4::uuid))
                  AND ($5::text IS NULL
                       OR s.title ILIKE $5 ESCAPE '\'
                       OR EXISTS (SELECT 1 FROM ai_messages m
                                   WHERE m.session_id = s.id AND m.content ILIKE $5 ESCAPE '\'))
                ORDER BY s.updated_at DESC, s.id DESC
                LIMIT $6""",
            operator_id, archived, cursor[0] if cursor else None, cursor[1] if cursor else None,
            like_pattern(q) if q else None, limit, list(CONVERSATION_ROLES))
        out = []
        for r in rows:
            row = _session_row(r)
            row["message_count"] = int(r["message_count"])
            row["preview"] = ({"role": r["last_role"], "content": preview_text(r["last_content"])}
                              if r["last_role"] else None)
            out.append(row)
        return out

    async def update_session(self, session_id: str, operator_id: str, *, title: str | None = None,
                             archived: bool | None = None) -> dict[str, Any] | None:
        """Rename and/or archive. Activity order (``updated_at``) is left alone: a rename is not a new turn."""
        sid = _valid_uuid(session_id)
        if sid is None:
            return None
        row = await self.pool.fetchrow(
            """UPDATE ai_sessions SET title = COALESCE($3, title), archived = COALESCE($4, archived)
                WHERE id = $1 AND operator_id = $2
            RETURNING id, title, archived, created_at, updated_at""",
            sid, operator_id, title, archived)
        return _session_row(row) if row else None

    async def append_message(self, session_id: str, role: str, content: str,
                             widgets: list[dict[str, Any]] | None = None, trace_id: str | None = None) -> int:
        sid = uuid.UUID(session_id)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                msg_id = await conn.fetchval(
                    """INSERT INTO ai_messages (session_id, role, content, widgets, trace_id)
                       VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING id""",
                    sid, role, content, json.dumps(widgets or [], default=str), _valid_uuid(trace_id))
                # A new message brings an archived conversation back into the history
                await conn.execute("UPDATE ai_sessions SET updated_at = now(), archived = false WHERE id = $1", sid)
        return int(msg_id)

    async def messages(self, session_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """SELECT id, role, content, widgets, trace_id, created_at FROM ai_messages
               WHERE session_id = $1 AND id > $2 ORDER BY id""",
            uuid.UUID(session_id), after_id)
        return [_message_row(r) for r in rows]

    async def latest_summary(self, session_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """SELECT summary, covers_message_id FROM ai_session_summaries
               WHERE session_id = $1 ORDER BY id DESC LIMIT 1""",
            uuid.UUID(session_id))
        return {"summary": row["summary"], "covers_message_id": row["covers_message_id"]} if row else None

    async def save_summary(self, session_id: str, summary: str, covers_message_id: int,
                           trace_id: str | None = None) -> None:
        await self.pool.execute(
            """INSERT INTO ai_session_summaries (session_id, summary, covers_message_id, trace_id)
               VALUES ($1, $2, $3, $4)""",
            uuid.UUID(session_id), summary, covers_message_id, _valid_uuid(trace_id))

    async def update_widget(self, session_id: str, widget_id: str, patch: dict[str, Any],
                            expect_status: str | None = None) -> bool:
        """Patch a widget; with ``expect_status`` only if it currently has that status (row-locked, so
        an approval and a rejection of the same card cannot both win)."""
        updated = False
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """SELECT id, widgets FROM ai_messages
                       WHERE session_id = $1 AND widgets @> $2::jsonb FOR UPDATE""",
                    uuid.UUID(session_id), json.dumps([{"id": widget_id}]))
                for row in rows:
                    widgets = row["widgets"]
                    if isinstance(widgets, str):
                        widgets = json.loads(widgets)
                    changed = _patch(widgets, widget_id, patch, expect_status)
                    if changed:
                        updated = True
                        await conn.execute("UPDATE ai_messages SET widgets = $2::jsonb WHERE id = $1",
                                           row["id"], json.dumps(widgets, default=str))
        return updated


class MemorySessionStore:
    """In-process store with the same behaviour (tests; a worker without a database)."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.rows: list[dict[str, Any]] = []
        self.summaries: list[dict[str, Any]] = []
        self._last = datetime.datetime.min.replace(tzinfo=datetime.UTC)

    def _now(self) -> str:
        # Strictly increasing, so two writes in the same microsecond still order (as Postgres' now() does per turn)
        now = max(datetime.datetime.now(datetime.UTC), self._last + datetime.timedelta(microseconds=1))
        self._last = now
        return now.isoformat()

    async def create_session(self, operator_id: str, title: str | None = None) -> str:
        sid = str(uuid.uuid4())
        now = self._now()
        self.sessions[sid] = {"id": sid, "operator_id": operator_id, "title": title, "archived": False,
                              "created_at": now, "updated_at": now}
        return sid

    async def get_session(self, session_id: str, operator_id: str) -> dict[str, Any] | None:
        s = self.sessions.get(session_id)
        if not s or s["operator_id"] != operator_id:
            return None
        return {k: v for k, v in s.items() if k != "operator_id"}

    async def list_sessions(self, operator_id: str, limit: int = 20, *, query: str | None = None,
                            archived: bool = False, before: str | None = None) -> list[dict[str, Any]]:
        q = (query or "").strip().lower()
        cursor = parse_cursor(before)
        own = [s for s in self.sessions.values() if s["operator_id"] == operator_id and s["archived"] == archived]
        if q:
            own = [s for s in own if q in (s["title"] or "").lower()
                   or any(q in r["content"].lower() for r in self.rows if r["session_id"] == s["id"])]
        own.sort(key=lambda s: (s["updated_at"], s["id"]), reverse=True)
        if cursor:
            own = [s for s in own
                   if (datetime.datetime.fromisoformat(s["updated_at"]), uuid.UUID(s["id"])) < cursor]
        out = []
        for s in own[:limit]:
            said = [r for r in self.rows if r["session_id"] == s["id"] and r["role"] in CONVERSATION_ROLES]
            row = {k: v for k, v in s.items() if k != "operator_id"}
            row["message_count"] = len(said)
            row["preview"] = ({"role": said[-1]["role"], "content": preview_text(said[-1]["content"])}
                              if said else None)
            out.append(row)
        return out

    async def update_session(self, session_id: str, operator_id: str, *, title: str | None = None,
                             archived: bool | None = None) -> dict[str, Any] | None:
        s = self.sessions.get(session_id)
        if not s or s["operator_id"] != operator_id:
            return None
        if title is not None:
            s["title"] = title
        if archived is not None:
            s["archived"] = archived
        return {k: v for k, v in s.items() if k != "operator_id"}

    async def append_message(self, session_id: str, role: str, content: str,
                             widgets: list[dict[str, Any]] | None = None, trace_id: str | None = None) -> int:
        msg_id = len(self.rows) + 1
        self.rows.append({"id": msg_id, "session_id": session_id, "role": role, "content": content,
                          "widgets": list(widgets or []), "trace_id": trace_id, "created_at": self._now()})
        self.sessions[session_id]["updated_at"] = self._now()
        self.sessions[session_id]["archived"] = False
        return msg_id

    async def messages(self, session_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        return [{k: v for k, v in r.items() if k != "session_id"}
                for r in self.rows if r["session_id"] == session_id and r["id"] > after_id]

    async def latest_summary(self, session_id: str) -> dict[str, Any] | None:
        own = [s for s in self.summaries if s["session_id"] == session_id]
        return {"summary": own[-1]["summary"], "covers_message_id": own[-1]["covers_message_id"]} if own else None

    async def save_summary(self, session_id: str, summary: str, covers_message_id: int,
                           trace_id: str | None = None) -> None:
        self.summaries.append({"session_id": session_id, "summary": summary,
                               "covers_message_id": covers_message_id, "trace_id": trace_id})

    async def update_widget(self, session_id: str, widget_id: str, patch: dict[str, Any],
                            expect_status: str | None = None) -> bool:
        return any([_patch(r["widgets"], widget_id, patch, expect_status)
                    for r in self.rows if r["session_id"] == session_id])
