#!/usr/bin/env python3
"""
Claude Code Conversation Indexer
Parses JSONL conversation files and builds a SQLite index with FTS5 search.
"""

import json
import os
import re
import sqlite3
import sys
from glob import glob
from pathlib import Path

from config import CLAUDE_DIR, PROJECTS_DIR, DB_PATH, get_config


def get_project_dirs():
    """Find all project directories to index."""
    config = get_config()
    filters = config.get("project_filters", [])
    skip = set(config.get("skip_subdirs", []))

    if filters:
        dirs = set()
        for pf in filters:
            pattern = str(PROJECTS_DIR / f"*{pf}*")
            dirs.update(glob(pattern))
    else:
        # No filters = scan ALL projects
        dirs = set()
        if PROJECTS_DIR.exists():
            for d in PROJECTS_DIR.iterdir():
                if d.is_dir() and d.name not in skip:
                    dirs.add(str(d))

    return sorted(dirs)


def project_path_from_cwd(cwd: str) -> str:
    """Derive a readable project path from the actual cwd.
    Strips the home directory prefix and returns a clean relative path.
    """
    if not cwd:
        return "unknown"

    home = str(Path.home())
    if cwd.startswith(home):
        rel = cwd[len(home):].strip("/")
        # Remove common prefixes like Desktop/, Documents/
        for prefix in ["Desktop/", "Documents/", "Projects/", "Code/", "dev/"]:
            if rel.startswith(prefix):
                return rel
        return rel

    # Fallback: last 2-3 path components
    parts = cwd.rstrip("/").split("/")
    return "/".join(parts[-3:]) if len(parts) >= 3 else cwd


def extract_text_content(message_content) -> str:
    """Extract plain text from message content (handles string and list formats)."""
    if isinstance(message_content, str):
        return message_content
    if isinstance(message_content, list):
        texts = []
        for block in message_content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "tool_result" and isinstance(block.get("content"), str):
                    texts.append(block["content"][:200])
        return "\n".join(texts)
    return str(message_content) if message_content else ""


def generate_title(first_prompt: str) -> str:
    """Generate a conversation title from the first user prompt."""
    if not first_prompt:
        return "(empty)"

    text = first_prompt.strip()
    text = re.sub(r'<[^>]+>', '', text).strip()

    # For "Implement the following plan:" prompts, extract the plan title
    if text.startswith("Implement the following plan:"):
        heading_match = re.search(r'^#\s+(.+)$', text, re.MULTILINE)
        if heading_match:
            plan_title = heading_match.group(1).strip()
            plan_title = re.sub(r'^Plan:\s*', '', plan_title)
            if len(plan_title) <= 60:
                return plan_title
            return plan_title[:57] + "..."

    # For skill invocations
    if "Base directory for this skill:" in text:
        skill_match = re.search(r'#\s+(.+)', text)
        if skill_match:
            return skill_match.group(1).strip()[:60]

    if text.startswith("/"):
        return text[:60]

    if len(text.split()) <= 3:
        return text[:60]

    first_sentence = re.split(r'[.!?\n]', text)[0].strip()
    if len(first_sentence) <= 60:
        return first_sentence

    truncated = text[:57]
    last_space = truncated.rfind(" ")
    if last_space > 30:
        return truncated[:last_space] + "..."
    return truncated + "..."


def parse_jsonl_file(filepath: str) -> dict | None:
    """Parse a JSONL conversation file and extract metadata + messages."""
    session_id = Path(filepath).stem
    messages = []
    user_prompts = []
    total_tokens = 0
    first_timestamp = None
    last_timestamp = None
    slug = None
    cwd = None
    version = None

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                record_type = record.get("type")
                timestamp = record.get("timestamp")

                if timestamp:
                    if first_timestamp is None or timestamp < first_timestamp:
                        first_timestamp = timestamp
                    if last_timestamp is None or timestamp > last_timestamp:
                        last_timestamp = timestamp

                if not slug and record.get("slug"):
                    slug = record["slug"]
                if not cwd and record.get("cwd"):
                    cwd = record["cwd"]
                if not version and record.get("version"):
                    version = record["version"]

                if record_type == "user":
                    msg = record.get("message", {})
                    content = extract_text_content(msg.get("content", ""))
                    is_real_prompt = (
                        content
                        and not content.startswith("The user doesn't want to proceed")
                        and not content.startswith("Caveat:")
                        and not content.startswith("Note:")
                        and not content.startswith("[Request interrupted")
                        and record.get("userType") != "internal"
                    )
                    if is_real_prompt:
                        user_prompts.append(content)
                    messages.append({
                        "role": "user",
                        "content": content,
                        "timestamp": timestamp,
                        "token_count": 0,
                    })

                elif record_type == "assistant":
                    msg = record.get("message", {})
                    content = extract_text_content(msg.get("content", ""))
                    usage = msg.get("usage", {})
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    cache_creation = usage.get("cache_creation_input_tokens", 0)
                    cache_read = usage.get("cache_read_input_tokens", 0)
                    msg_tokens = input_tokens + output_tokens + cache_creation + cache_read
                    total_tokens += msg_tokens

                    if content:
                        messages.append({
                            "role": "assistant",
                            "content": content,
                            "timestamp": timestamp,
                            "token_count": msg_tokens,
                        })

    except Exception as e:
        print(f"  Error parsing {filepath}: {e}", file=sys.stderr)
        return None

    if not messages:
        return None

    first_prompt = user_prompts[0] if user_prompts else ""
    title = generate_title(first_prompt)

    excerpt_parts = []
    for prompt in user_prompts[:3]:
        truncated = prompt[:150].strip()
        if len(prompt) > 150:
            truncated += "..."
        excerpt_parts.append(truncated)
    excerpt = "\n---\n".join(excerpt_parts)

    return {
        "session_id": session_id,
        "slug": slug or "",
        "title": title,
        "excerpt": excerpt,
        "first_message": first_timestamp,
        "last_message": last_timestamp,
        "message_count": len(messages),
        "user_message_count": len(user_prompts),
        "total_tokens": total_tokens,
        "cwd": cwd or "",
        "version": version or "",
        "messages": messages,
    }


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize SQLite database with schema."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            session_id TEXT PRIMARY KEY,
            project_path TEXT NOT NULL,
            slug TEXT,
            title TEXT,
            excerpt TEXT,
            first_message TEXT,
            last_message TEXT,
            message_count INTEGER DEFAULT 0,
            user_message_count INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cwd TEXT,
            version TEXT,
            file_path TEXT,
            file_mtime REAL,
            indexed_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp TEXT,
            token_count INTEGER DEFAULT 0,
            embedding BLOB,
            FOREIGN KEY (session_id) REFERENCES conversations(session_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations(project_path);
        CREATE INDEX IF NOT EXISTS idx_conversations_last_message ON conversations(last_message DESC);
    """)

    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
            session_id UNINDEXED, title, excerpt,
            content='conversations', content_rowid='rowid'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            session_id UNINDEXED, content,
            content='messages', content_rowid='id'
        );
    """)

    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS conversations_ai AFTER INSERT ON conversations BEGIN
            INSERT INTO conversations_fts(rowid, session_id, title, excerpt)
            VALUES (new.rowid, new.session_id, new.title, new.excerpt);
        END;
        CREATE TRIGGER IF NOT EXISTS conversations_ad AFTER DELETE ON conversations BEGIN
            INSERT INTO conversations_fts(conversations_fts, rowid, session_id, title, excerpt)
            VALUES ('delete', old.rowid, old.session_id, old.title, old.excerpt);
        END;
        CREATE TRIGGER IF NOT EXISTS conversations_au AFTER UPDATE ON conversations BEGIN
            INSERT INTO conversations_fts(conversations_fts, rowid, session_id, title, excerpt)
            VALUES ('delete', old.rowid, old.session_id, old.title, old.excerpt);
            INSERT INTO conversations_fts(rowid, session_id, title, excerpt)
            VALUES (new.rowid, new.session_id, new.title, new.excerpt);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, session_id, content)
            VALUES (new.id, new.session_id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, session_id, content)
            VALUES ('delete', old.id, old.session_id, old.content);
        END;
    """)

    conn.commit()
    return conn


def index_conversation(conn: sqlite3.Connection, fallback_project_path: str, filepath: str, force: bool = False) -> bool:
    """Index a single conversation file. Returns True if indexed."""
    session_id = Path(filepath).stem
    file_mtime = os.path.getmtime(filepath)

    if not force:
        row = conn.execute(
            "SELECT file_mtime FROM conversations WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        if row and row[0] == file_mtime:
            return False

    data = parse_jsonl_file(filepath)
    if not data:
        return False

    project_path = project_path_from_cwd(data["cwd"]) if data["cwd"] else fallback_project_path

    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))

    conn.execute("""
        INSERT INTO conversations (session_id, project_path, slug, title, excerpt,
            first_message, last_message, message_count, user_message_count,
            total_tokens, cwd, version, file_path, file_mtime)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session_id, project_path, data["slug"], data["title"], data["excerpt"],
        data["first_message"], data["last_message"], data["message_count"],
        data["user_message_count"], data["total_tokens"], data["cwd"],
        data["version"], filepath, file_mtime,
    ))

    msg_rows = [
        (session_id, m["role"], m["content"], m["timestamp"], m["token_count"])
        for m in data["messages"]
        if m["content"]
    ]
    conn.executemany("""
        INSERT INTO messages (session_id, role, content, timestamp, token_count)
        VALUES (?, ?, ?, ?, ?)
    """, msg_rows)

    return True


def run_index(force: bool = False):
    """Run the full indexing process."""
    config = get_config()
    filters = config.get("project_filters", [])

    print("Claude Conversation Indexer")
    print(f"Database: {DB_PATH}")
    print(f"Scope: {'all projects' if not filters else ', '.join(filters)}")
    print()

    conn = init_db(str(DB_PATH))
    project_dirs = get_project_dirs()

    total_indexed = 0
    total_skipped = 0
    total_errors = 0
    indexed_session_ids = set()

    for proj_dir in project_dirs:
        dirname = os.path.basename(proj_dir)
        jsonl_files = glob(os.path.join(proj_dir, "*.jsonl"))

        if not jsonl_files:
            continue

        print(f"  {dirname}: {len(jsonl_files)} files")

        for filepath in jsonl_files:
            try:
                indexed = index_conversation(conn, dirname, filepath, force=force)
                indexed_session_ids.add(Path(filepath).stem)
                if indexed:
                    total_indexed += 1
                else:
                    total_skipped += 1
            except Exception as e:
                print(f"    Error: {Path(filepath).stem}: {e}", file=sys.stderr)
                total_errors += 1

        conn.commit()

    # Clean up orphaned records
    all_db_ids = {r[0] for r in conn.execute("SELECT session_id FROM conversations").fetchall()}
    orphaned = all_db_ids - indexed_session_ids
    if orphaned:
        print(f"\n  Cleaning up {len(orphaned)} orphaned records...")
        for sid in orphaned:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM conversations WHERE session_id = ?", (sid,))
        conn.commit()

    total_in_db = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    total_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    total_tokens = conn.execute("SELECT COALESCE(SUM(total_tokens), 0) FROM conversations").fetchone()[0]

    print()
    print(f"Indexed: {total_indexed} new/updated")
    print(f"Skipped: {total_skipped} unchanged")
    print(f"Errors:  {total_errors}")
    print(f"Total:   {total_in_db} conversations, {total_messages} messages")
    print(f"Tokens:  {total_tokens:,}")
    print(f"DB size: {os.path.getsize(str(DB_PATH)) / 1024 / 1024:.1f} MB")

    conn.close()


if __name__ == "__main__":
    force = "--force" in sys.argv
    run_index(force=force)
