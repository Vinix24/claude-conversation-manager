#!/usr/bin/env python3
"""
Claude Code Session Dashboard desktop app.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import shutil
import subprocess
import sys
from pathlib import Path

import webview

from config import APP_NAME, CONFIG_DIR, DB_PATH, get_config, save_default_config
from dashboard_data import get_dashboard_payload, get_session_detail
from file_browser import get_directory_tree, read_file_content
from indexer import init_db, run_index
from terminal_opener import open_session


class ConversationAPI:
    """Python API exposed to the webview frontend."""

    def __init__(self):
        self.db_path = str(DB_PATH)
        self.config = get_config()
        self._db_ready = False
        self._ensure_db()

    def _ensure_db(self):
        if self._db_ready:
            return
        conn = init_db(self.db_path)
        conn.close()
        self._db_ready = True

    def _get_conn(self):
        self._ensure_db()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _serialize_conversation_row(self, row: sqlite3.Row) -> dict:
        item = dict(row)
        session_id = item.get("session_id", "")
        created_at = item.get("created_at") or item.get("first_message")
        updated_at = item.get("updated_at") or item.get("last_message")
        first_preview = self._clean_preview_text(item.get("first_preview"), item.get("excerpt"))
        last_preview = self._clean_preview_text(item.get("last_preview"), item.get("excerpt"))
        item.update(
            {
                "created_at": created_at,
                "updated_at": updated_at,
                "last_message_at": item.get("last_message_at") or updated_at,
                "working_directory": item.get("working_directory") or item.get("cwd") or "",
                "first_preview": first_preview,
                "last_preview": last_preview,
                "resume_command": f"claude -r {session_id}" if session_id else "",
                "can_resume": bool(session_id),
            }
        )
        return item

    def _clean_preview_text(self, preview: str | None, fallback: str | None = None) -> str:
        text = (preview or "").strip()
        if text:
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            text = text[:220].strip()
            if text and not text.lower().startswith(("caveat:", "note:", "/clear", "clear ")):
                return text

        fallback_text = (fallback or "").strip()
        fallback_text = re.sub(r"<[^>]+>", " ", fallback_text)
        fallback_text = re.sub(r"\s+", " ", fallback_text).strip()
        return fallback_text[:220].strip()

    def _project_scope_clause(self, alias: str, project_path: str | None, params: list) -> str:
        if not project_path or project_path == "all":
            return ""
        params.extend([project_path, f"{project_path}/%"])
        return f" AND ({alias}.project_path = ? OR {alias}.project_path LIKE ?)"

    def get_projects(self):
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT
                project_path,
                COUNT(*) AS count,
                MAX(COALESCE(last_message, first_message)) AS latest,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
            FROM conversations
            GROUP BY project_path
            ORDER BY CASE WHEN project_path = 'root' THEN 0 ELSE 1 END, latest DESC, count DESC
            """
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def _build_fts_query(self, raw: str) -> str:
        """Convert user input into a safe FTS5 MATCH expression.

        Multi-word input becomes implicit-AND with prefix matching on each
        token. Power users can use FTS5 syntax (quoted phrases, NEAR, NOT,
        column filters) by including ``"`` or ``:`` in the query.
        """
        text = raw.strip()
        if not text:
            return ""
        if any(c in text for c in '":^'):
            return text
        tokens = re.findall(r"\S+", text)
        parts: list[str] = []
        for token in tokens:
            upper = token.upper()
            if upper in {"AND", "OR", "NOT"}:
                parts.append(upper)
                continue
            if token.startswith(("+", "-")):
                head, tail = token[0], token[1:]
                cleaned = re.sub(r"[^\wÀ-￿*]", "", tail)
                if cleaned:
                    parts.append(f"{head}{cleaned}*" if not cleaned.endswith("*") else f"{head}{cleaned}")
                continue
            cleaned = re.sub(r"[^\wÀ-￿*]", "", token)
            if not cleaned:
                continue
            if cleaned.endswith("*"):
                parts.append(cleaned)
            else:
                parts.append(f"{cleaned}*")
        return " ".join(parts)

    def get_conversations(
        self,
        project_path=None,
        search=None,
        sort="newest",
        limit=200,
        offset=0,
    ):
        conn = self._get_conn()
        params: list = []

        select_fields = """
            c.session_id,
            c.project_path,
            c.slug,
            c.title,
            c.excerpt,
            (
                SELECT m.content
                FROM messages m
                WHERE m.session_id = c.session_id
                  AND m.content IS NOT NULL
                  AND TRIM(m.content) != ''
                ORDER BY m.timestamp ASC, m.id ASC
                LIMIT 1
            ) AS first_preview,
            (
                SELECT m.content
                FROM messages m
                WHERE m.session_id = c.session_id
                  AND m.content IS NOT NULL
                  AND TRIM(m.content) != ''
                ORDER BY m.timestamp DESC, m.id DESC
                LIMIT 1
            ) AS last_preview,
            c.first_message,
            c.last_message,
            c.first_message AS created_at,
            c.last_message AS updated_at,
            c.last_message AS last_message_at,
            c.message_count,
            c.user_message_count,
            c.total_tokens,
            c.estimated_cost_usd,
            c.primary_model,
            c.model_count,
            c.model_display,
            c.cwd,
            c.cwd AS working_directory
        """

        search_meta: dict = {"active": False, "error": None, "fts_query": None}
        snippet_map: dict[str, dict] = {}

        if search:
            fts_query = self._build_fts_query(search)
            search_meta.update({"active": True, "fts_query": fts_query})

            if not fts_query:
                conn.close()
                return {
                    "conversations": [],
                    "search": {**search_meta, "error": "Empty query after sanitisation."},
                }

            try:
                title_rows = conn.execute(
                    """
                    SELECT session_id,
                           bm25(conversations_fts, 0.0, 5.0, 3.0) AS title_score,
                           snippet(conversations_fts, 1, '<mark>', '</mark>', '…', 12) AS title_snippet,
                           snippet(conversations_fts, 2, '<mark>', '</mark>', '…', 18) AS excerpt_snippet
                    FROM conversations_fts
                    WHERE conversations_fts MATCH ?
                    """,
                    [fts_query],
                ).fetchall()

                msg_raw = conn.execute(
                    """
                    SELECT session_id,
                           rank AS msg_score,
                           snippet(messages_fts, 1, '<mark>', '</mark>', '…', 18) AS snippet
                    FROM messages_fts
                    WHERE messages_fts MATCH ?
                    ORDER BY rank
                    LIMIT 5000
                    """,
                    [fts_query],
                ).fetchall()
            except sqlite3.OperationalError as exc:
                conn.close()
                return {
                    "conversations": [],
                    "search": {
                        **search_meta,
                        "error": f"Invalid search syntax: {exc}",
                    },
                }
            except sqlite3.Error as exc:
                conn.close()
                return {
                    "conversations": [],
                    "search": {**search_meta, "error": f"Search failed: {exc}"},
                }

            TITLE_BOOST = 200.0
            for row in title_rows:
                sid = row["session_id"]
                snippet = row["title_snippet"] or row["excerpt_snippet"]
                snippet_map[sid] = {
                    "score": (row["title_score"] or 0.0) - TITLE_BOOST,
                    "snippet": snippet,
                    "match_kind": "title",
                }

            msg_per_session: dict[str, sqlite3.Row] = {}
            for row in msg_raw:
                sid = row["session_id"]
                if sid not in msg_per_session:
                    msg_per_session[sid] = row

            for sid, row in msg_per_session.items():
                msg_score = row["msg_score"] or 0.0
                if sid in snippet_map:
                    snippet_map[sid]["score"] += msg_score
                    if not snippet_map[sid].get("snippet"):
                        snippet_map[sid]["snippet"] = row["snippet"]
                    snippet_map[sid]["match_kind"] = "title+message"
                else:
                    snippet_map[sid] = {
                        "score": msg_score,
                        "snippet": row["snippet"],
                        "match_kind": "message",
                    }

            if not snippet_map:
                conn.close()
                return {
                    "conversations": [],
                    "search": {**search_meta, "error": None, "matches": 0},
                }

            session_ids = list(snippet_map.keys())
            placeholders = ",".join("?" for _ in session_ids)
            params = list(session_ids)
            project_filter = self._project_scope_clause("c", project_path, params)
            query = f"""
                SELECT {select_fields}
                FROM conversations c
                WHERE c.session_id IN ({placeholders}){project_filter}
            """
        else:
            query = f"""
                SELECT {select_fields}
                FROM conversations c
                WHERE 1=1
            """
            query += self._project_scope_clause("c", project_path, params)

        sort_map = {
            "date": "COALESCE(c.last_message, c.first_message) DESC, c.first_message DESC, c.session_id DESC",
            "newest": "COALESCE(c.last_message, c.first_message) DESC, c.first_message DESC, c.session_id DESC",
            "oldest": "COALESCE(c.first_message, c.last_message) ASC, c.last_message ASC, c.session_id ASC",
            "messages": "c.message_count DESC, COALESCE(c.last_message, c.first_message) DESC",
            "tokens": "c.total_tokens DESC, COALESCE(c.last_message, c.first_message) DESC",
            "cost": "c.estimated_cost_usd DESC, COALESCE(c.last_message, c.first_message) DESC",
            "title": "LOWER(c.title) ASC, COALESCE(c.last_message, c.first_message) DESC",
        }
        if sort != "relevance" or not search_meta["active"]:
            query += f" ORDER BY {sort_map.get(sort, sort_map['newest'])}"
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])

        try:
            rows = conn.execute(query, params).fetchall()
        except sqlite3.Error as exc:
            conn.close()
            return {
                "conversations": [],
                "search": {**search_meta, "error": f"Query failed: {exc}"},
            }
        conn.close()

        serialised = [self._serialize_conversation_row(row) for row in rows]

        if search_meta["active"]:
            for item in serialised:
                meta = snippet_map.get(item.get("session_id"))
                if meta:
                    item["snippet_html"] = meta.get("snippet")
                    item["search_score"] = meta.get("score")
                    item["match_kind"] = meta.get("match_kind")
            if sort == "relevance":
                serialised.sort(key=lambda it: it.get("search_score") or 0.0)
                serialised = serialised[offset : offset + limit]
            search_meta["matches"] = len(snippet_map)

        return {
            "conversations": serialised,
            "search": search_meta,
        }

    def get_conversation_messages(self, session_id):
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT role, content, timestamp, token_count, model, estimated_cost_usd
            FROM messages
            WHERE session_id = ?
            ORDER BY timestamp ASC, id ASC
            """,
            (session_id,),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_session_detail(self, session_id):
        conn = self._get_conn()
        detail = get_session_detail(conn, session_id)
        conn.close()
        return detail

    def get_dashboard_payload(self, project_path="all", range_key="30d", month=None):
        conn = self._get_conn()
        payload = get_dashboard_payload(conn, project_path=project_path, range_key=range_key, month=month)
        conn.close()
        return payload

    def get_stats(self):
        conn = self._get_conn()
        projects_dir = Path(self.config.get("claude_projects_dir", "")).expanduser()
        stats = {
            "total_conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            "total_messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "total_tokens": conn.execute("SELECT COALESCE(SUM(total_tokens), 0) FROM conversations").fetchone()[0],
            "estimated_cost_usd": round(
                conn.execute("SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM conversations").fetchone()[0] or 0.0,
                2,
            ),
            "db_size_mb": round(os.path.getsize(self.db_path) / 1024 / 1024, 1) if Path(self.db_path).exists() else 0,
            "last_indexed": conn.execute("SELECT MAX(indexed_at) FROM conversations").fetchone()[0] or "never",
            "projects_dir": str(projects_dir),
            "projects_dir_exists": projects_dir.exists(),
        }
        conn.close()
        return stats

    def export_conversation(self, session_id, format="markdown"):
        """Export a conversation as markdown or JSON."""
        conn = self._get_conn()
        detail = get_session_detail(conn, session_id)
        conn.close()

        if not detail:
            return {"ok": False, "error": "Conversation not found"}

        conversation = detail["conversation"]
        messages = detail["messages"]

        if format == "json":
            payload = {
                "session_id": conversation["session_id"],
                "title": conversation["title"],
                "project": conversation["project_path"],
                "created_at": conversation.get("created_at") or conversation["first_message"],
                "updated_at": conversation.get("updated_at") or conversation["last_message"],
                "last_message_at": conversation.get("last_message_at") or conversation["last_message"],
                "working_directory": conversation.get("working_directory") or conversation.get("cwd") or "",
                "first_message": conversation["first_message"],
                "last_message": conversation["last_message"],
                "message_count": conversation["message_count"],
                "total_tokens": conversation["total_tokens"],
                "estimated_cost_usd": conversation["estimated_cost_usd"],
                "model_display": conversation["model_display"],
                "resume_command": conversation.get("resume_command") or f"claude -r {conversation['session_id']}",
                "messages": messages,
            }
            return {
                "ok": True,
                "content": json.dumps(payload, indent=2),
                "filename": f"{session_id[:8]}.json",
            }

        lines = [
            f"# {conversation['title']}",
            "",
            f"Session ID: `{conversation['session_id']}`",
            f"Project: {conversation['project_path']}",
            f"Date: {conversation['first_message'][:10] if conversation['first_message'] else '?'}",
            f"Messages: {conversation['message_count']}",
            f"Tokens: {conversation['total_tokens']:,}",
            f"Estimated cost: ${conversation['estimated_cost_usd']:.2f}",
            f"Model mix: {conversation['model_display']}",
            "",
        ]
        for message in messages:
            if not message["content"] or len(message["content"]) < 2:
                continue
            role = "You" if message["role"] == "user" else "Claude"
            lines.append(f"## {role}")
            lines.append("")
            lines.append(message["content"])
            lines.append("")

        return {
            "ok": True,
            "content": "\n".join(lines),
            "filename": f"{session_id[:8]}.md",
        }

    def open_in_terminal(self, session_id, cwd, terminal=None):
        try:
            open_session(session_id, cwd, terminal)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_file_tree(self):
        return get_directory_tree()

    def read_file(self, file_path):
        return read_file_content(file_path)

    def reindex(self, force=False):
        try:
            run_index(force=bool(force))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_terminal_name(self):
        return self.config.get("terminal", "terminal")

    def copy_to_clipboard(self, text):
        try:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
            return {"ok": True}
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            for command in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
                if shutil.which(command[0]):
                    try:
                        subprocess.run(command, input=text.encode("utf-8"), check=True)
                        return {"ok": True}
                    except subprocess.CalledProcessError:
                        continue
            return {"ok": False, "error": str(exc)}


def get_html():
    template_path = Path(__file__).parent / "templates" / "index.html"
    if not template_path.exists():
        candidate = Path(sys.prefix) / "share" / "claude-session-dashboard" / "templates" / "index.html"
        if candidate.exists():
            template_path = candidate
    html = template_path.read_text(encoding="utf-8")
    css_path = (get_config().get("custom_css_path") or "").strip()
    if not css_path:
        return html

    path = Path(css_path).expanduser()
    if not path.is_absolute():
        path = (CONFIG_DIR / path).resolve()
    if not path.exists():
        return html

    try:
        css = path.read_text(encoding="utf-8")
    except OSError:
        return html

    style_tag = f"\n<style>\n{css}\n</style>\n"
    if "</head>" in html:
        return html.replace("</head>", f"{style_tag}</head>", 1)
    return html + style_tag


def main():
    save_default_config()
    config = get_config()
    api = ConversationAPI()

    window = webview.create_window(
        title=APP_NAME,
        html=get_html(),
        js_api=api,
        width=config.get("window_width", 1560),
        height=config.get("window_height", 980),
        min_size=(1100, 700),
        background_color="#061845",
        text_select=True,
    )

    try:
        from AppKit import NSApp, NSApplication

        NSApplication.sharedApplication()
        NSApp.activateIgnoringOtherApps_(True)
    except ImportError:
        pass

    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
