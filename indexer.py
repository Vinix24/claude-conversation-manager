#!/usr/bin/env python3
"""
Claude Code Session Indexer
Parses JSONL conversation files and builds a SQLite index with FTS5 search.
"""

import argparse
from collections import Counter
import json
import os
import re
import sqlite3
import sys
from glob import glob
from pathlib import Path

from claude_models import estimate_message_cost_usd, normalize_model_name, summarize_models
from config import DB_PATH, get_config, get_projects_dir

CURRENT_SCHEMA_VERSION = 2


def get_project_dirs():
    """Find all project directories to index."""
    config = get_config()
    filters = config.get("project_filters", [])
    skip = set(config.get("skip_subdirs", []))
    projects_dir = get_projects_dir(config)

    if filters:
        dirs = set()
        for pf in filters:
            pattern = str(projects_dir / f"*{pf}*")
            dirs.update(glob(pattern))
    else:
        # No filters = scan ALL projects
        dirs = set()
        if projects_dir.exists():
            for d in projects_dir.iterdir():
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

    raw_text = re.sub(r'<[^>]+>', '', first_prompt.strip()).strip()
    text = re.sub(r"\s+", " ", raw_text).strip()

    # For "Implement the following plan:" prompts, extract the plan title
    if text.startswith("Implement the following plan:"):
        heading_match = re.search(r'^#\s+(.+)$', raw_text, re.MULTILINE)
        if heading_match:
            plan_title = heading_match.group(1).strip()
            plan_title = re.sub(r'^Plan:\s*', '', plan_title)
            if len(plan_title) <= 60:
                return plan_title
            return plan_title[:57] + "..."

    # For skill invocations
    if "Base directory for this skill:" in raw_text:
        skill_match = re.search(r'^\s*#\s+(.+)$', raw_text, re.MULTILINE)
        if skill_match:
            return skill_match.group(1).strip()[:60]

    if text.lower().startswith("caveat:"):
        return "(session bootstrap)"

    if text.startswith("/"):
        command = text.split()[0]
        return command[:60]

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
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_creation_tokens = 0
    total_cache_read_tokens = 0
    estimated_cost_usd = 0.0
    priced_tokens = 0
    unpriced_tokens = 0
    first_timestamp = None
    last_timestamp = None
    slug = None
    cwd = None
    version = None
    model_totals: Counter[str] = Counter()

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
                        and "<local-command-caveat>" not in content
                        and not content.startswith("The user doesn't want to proceed")
                        and not content.startswith("Caveat:")
                        and not content.startswith("Note:")
                        and not content.startswith("[Request interrupted")
                        and not content.startswith("<command-name>/clear</command-name>")
                        and record.get("userType") != "internal"
                    )
                    if is_real_prompt:
                        user_prompts.append(content)
                    messages.append({
                        "role": "user",
                        "content": content,
                        "timestamp": timestamp,
                        "token_count": 0,
                        "model": "",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "estimated_cost_usd": 0.0,
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
                    model = normalize_model_name(msg.get("model"))
                    msg_cost_usd, was_priced = estimate_message_cost_usd(model, usage)

                    total_tokens += msg_tokens
                    total_input_tokens += input_tokens
                    total_output_tokens += output_tokens
                    total_cache_creation_tokens += cache_creation
                    total_cache_read_tokens += cache_read
                    estimated_cost_usd += msg_cost_usd
                    if was_priced:
                        priced_tokens += msg_tokens
                    else:
                        unpriced_tokens += msg_tokens
                    if model:
                        model_totals[model] += msg_tokens

                    if content:
                        messages.append({
                            "role": "assistant",
                            "content": content,
                            "timestamp": timestamp,
                            "token_count": msg_tokens,
                            "model": model,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "cache_creation_input_tokens": cache_creation,
                            "cache_read_input_tokens": cache_read,
                            "estimated_cost_usd": msg_cost_usd,
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
    model_summary = summarize_models(dict(model_totals))

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
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "cache_creation_input_tokens": total_cache_creation_tokens,
        "cache_read_input_tokens": total_cache_read_tokens,
        "estimated_cost_usd": round(estimated_cost_usd, 6),
        "priced_tokens": priced_tokens,
        "unpriced_tokens": unpriced_tokens,
        "cwd": cwd or "",
        "version": version or "",
        **model_summary,
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
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_creation_input_tokens INTEGER DEFAULT 0,
            cache_read_input_tokens INTEGER DEFAULT 0,
            estimated_cost_usd REAL DEFAULT 0,
            priced_tokens INTEGER DEFAULT 0,
            unpriced_tokens INTEGER DEFAULT 0,
            cwd TEXT,
            version TEXT,
            primary_model TEXT,
            model_count INTEGER DEFAULT 0,
            model_display TEXT,
            models_json TEXT,
            file_path TEXT,
            file_mtime REAL,
            schema_version INTEGER DEFAULT 1,
            indexed_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp TEXT,
            token_count INTEGER DEFAULT 0,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_creation_input_tokens INTEGER DEFAULT 0,
            cache_read_input_tokens INTEGER DEFAULT 0,
            estimated_cost_usd REAL DEFAULT 0,
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

    _ensure_columns(
        conn,
        "conversations",
        {
            "input_tokens": "INTEGER DEFAULT 0",
            "output_tokens": "INTEGER DEFAULT 0",
            "cache_creation_input_tokens": "INTEGER DEFAULT 0",
            "cache_read_input_tokens": "INTEGER DEFAULT 0",
            "estimated_cost_usd": "REAL DEFAULT 0",
            "priced_tokens": "INTEGER DEFAULT 0",
            "unpriced_tokens": "INTEGER DEFAULT 0",
            "primary_model": "TEXT",
            "model_count": "INTEGER DEFAULT 0",
            "model_display": "TEXT",
            "models_json": "TEXT",
            "schema_version": "INTEGER DEFAULT 1",
        },
    )
    _ensure_columns(
        conn,
        "messages",
        {
            "model": "TEXT",
            "input_tokens": "INTEGER DEFAULT 0",
            "output_tokens": "INTEGER DEFAULT 0",
            "cache_creation_input_tokens": "INTEGER DEFAULT 0",
            "cache_read_input_tokens": "INTEGER DEFAULT 0",
            "estimated_cost_usd": "REAL DEFAULT 0",
        },
    )
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
        CREATE INDEX IF NOT EXISTS idx_messages_model ON messages(model);
    """)

    conn.commit()
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def index_conversation(conn: sqlite3.Connection, fallback_project_path: str, filepath: str, force: bool = False) -> bool:
    """Index a single conversation file. Returns True if indexed."""
    session_id = Path(filepath).stem
    file_mtime = os.path.getmtime(filepath)

    if not force:
        row = conn.execute(
            "SELECT file_mtime, schema_version, model_display FROM conversations WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        if row and row[0] == file_mtime and int(row[1] or 1) >= CURRENT_SCHEMA_VERSION and row[2]:
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
            total_tokens, input_tokens, output_tokens, cache_creation_input_tokens,
            cache_read_input_tokens, estimated_cost_usd, priced_tokens, unpriced_tokens,
            cwd, version, primary_model, model_count, model_display, models_json,
            file_path, file_mtime, schema_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session_id, project_path, data["slug"], data["title"], data["excerpt"],
        data["first_message"], data["last_message"], data["message_count"],
        data["user_message_count"], data["total_tokens"], data["input_tokens"],
        data["output_tokens"], data["cache_creation_input_tokens"],
        data["cache_read_input_tokens"], data["estimated_cost_usd"],
        data["priced_tokens"], data["unpriced_tokens"], data["cwd"],
        data["version"], data["primary_model"], data["model_count"],
        data["model_display"], data["models_json"], filepath, file_mtime,
        CURRENT_SCHEMA_VERSION,
    ))

    msg_rows = [
        (
            session_id,
            m["role"],
            m["content"],
            m["timestamp"],
            m["token_count"],
            m["model"],
            m["input_tokens"],
            m["output_tokens"],
            m["cache_creation_input_tokens"],
            m["cache_read_input_tokens"],
            m["estimated_cost_usd"],
        )
        for m in data["messages"]
        if m["content"]
    ]
    conn.executemany("""
        INSERT INTO messages (
            session_id, role, content, timestamp, token_count, model,
            input_tokens, output_tokens, cache_creation_input_tokens,
            cache_read_input_tokens, estimated_cost_usd
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, msg_rows)

    return True


def run_index(force: bool = False):
    """Run the full indexing process."""
    config = get_config()
    filters = config.get("project_filters", [])
    projects_dir = get_projects_dir(config)

    print("Claude Code Session Indexer")
    print(f"Database: {DB_PATH}")
    print(f"Source:   {projects_dir}")
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


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Index Claude Code sessions into SQLite.")
    parser.add_argument("--force", action="store_true", help="Rebuild every session even if files are unchanged.")
    args = parser.parse_args(argv)
    run_index(force=args.force)


if __name__ == "__main__":
    main()
