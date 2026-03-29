#!/usr/bin/env python3
"""
Claude Conversation Manager — Desktop App
Browse, search, and resume Claude Code conversations.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import webview

from config import DB_PATH, get_config, save_default_config
from file_browser import get_directory_tree, read_file_content
from terminal_opener import open_session
from indexer import run_index


class ConversationAPI:
    """Python API exposed to the webview frontend."""

    def __init__(self):
        self.db_path = str(DB_PATH)
        self.config = get_config()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_projects(self):
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT project_path, COUNT(*) as count, MAX(last_message) as latest
            FROM conversations GROUP BY project_path
            ORDER BY CASE WHEN project_path = 'root' THEN 0 ELSE 1 END, count DESC
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_conversations(self, project_path=None, search=None, sort="date", limit=100, offset=0):
        conn = self._get_conn()
        params = []

        if search:
            fts_term = search.strip()
            fts_query = fts_term + '*' if not any(c in fts_term for c in ' "+-') else fts_term

            project_filter = ""
            if project_path and project_path != "all":
                project_filter = " AND c.project_path = ?"
                params = [fts_query, fts_query, project_path]
            else:
                params = [fts_query, fts_query]

            query = f"""
                SELECT DISTINCT c.session_id, c.project_path, c.slug, c.title,
                       c.excerpt, c.first_message, c.last_message,
                       c.message_count, c.user_message_count, c.total_tokens, c.cwd
                FROM conversations c
                WHERE c.session_id IN (
                    SELECT session_id FROM conversations_fts WHERE conversations_fts MATCH ?
                    UNION
                    SELECT session_id FROM messages_fts WHERE messages_fts MATCH ?
                ){project_filter}
            """
        else:
            query = """
                SELECT c.session_id, c.project_path, c.slug, c.title,
                       c.excerpt, c.first_message, c.last_message,
                       c.message_count, c.user_message_count, c.total_tokens, c.cwd
                FROM conversations c WHERE 1=1
            """
            if project_path and project_path != "all":
                query += " AND c.project_path = ?"
                params.append(project_path)

        sort_map = {"date": "c.last_message DESC", "messages": "c.message_count DESC",
                    "tokens": "c.total_tokens DESC", "title": "c.title ASC"}
        query += f" ORDER BY {sort_map.get(sort, 'c.last_message DESC')}"
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        try:
            rows = conn.execute(query, params).fetchall()
        except Exception:
            rows = []
        conn.close()
        return [dict(r) for r in rows]

    def get_conversation_messages(self, session_id):
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT role, content, timestamp, token_count
            FROM messages WHERE session_id = ? ORDER BY timestamp ASC, id ASC
        """, (session_id,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_stats(self):
        conn = self._get_conn()
        stats = {
            "total_conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            "total_messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "total_tokens": conn.execute("SELECT COALESCE(SUM(total_tokens), 0) FROM conversations").fetchone()[0],
            "db_size_mb": round(os.path.getsize(self.db_path) / 1024 / 1024, 1),
            "last_indexed": conn.execute("SELECT MAX(indexed_at) FROM conversations").fetchone()[0] or "never",
        }
        conn.close()
        return stats

    def export_conversation(self, session_id, format="markdown"):
        """Export a conversation as markdown or JSON."""
        conn = self._get_conn()
        conv = conn.execute("SELECT * FROM conversations WHERE session_id = ?", (session_id,)).fetchone()
        msgs = conn.execute("""
            SELECT role, content, timestamp FROM messages
            WHERE session_id = ? ORDER BY timestamp ASC, id ASC
        """, (session_id,)).fetchall()
        conn.close()

        if not conv:
            return {"ok": False, "error": "Conversation not found"}

        if format == "json":
            data = {
                "session_id": conv["session_id"],
                "title": conv["title"],
                "project": conv["project_path"],
                "messages": [{"role": m["role"], "content": m["content"], "timestamp": m["timestamp"]} for m in msgs],
            }
            return {"ok": True, "content": json.dumps(data, indent=2), "filename": f"{session_id[:8]}.json"}

        # Markdown format
        lines = [f"# {conv['title']}\n", f"**Project:** {conv['project_path']}  "]
        lines.append(f"**Date:** {conv['first_message'][:10] if conv['first_message'] else '?'}  ")
        lines.append(f"**Messages:** {conv['message_count']}\n")
        for m in msgs:
            if not m["content"] or len(m["content"]) < 2:
                continue
            role = "You" if m["role"] == "user" else "Claude"
            lines.append(f"## {role}\n")
            lines.append(m["content"])
            lines.append("")

        return {"ok": True, "content": "\n".join(lines), "filename": f"{session_id[:8]}.md"}

    def open_in_terminal(self, session_id, cwd, terminal=None):
        try:
            open_session(session_id, cwd, terminal)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_file_tree(self):
        return get_directory_tree()

    def read_file(self, file_path):
        return read_file_content(file_path)

    def reindex(self):
        try:
            run_index(force=False)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_terminal_name(self):
        return self.config.get("terminal", "terminal")


def get_html():
    template_path = Path(__file__).parent / "templates" / "index.html"
    return template_path.read_text(encoding="utf-8")


def main():
    save_default_config()
    config = get_config()
    api = ConversationAPI()

    window = webview.create_window(
        title="Claude Conversations",
        html=get_html(),
        js_api=api,
        width=config.get("window_width", 1400),
        height=config.get("window_height", 900),
        min_size=(900, 600),
        background_color="#0A2463",
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
