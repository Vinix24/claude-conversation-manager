#!/usr/bin/env python3
"""
File browser API — provides directory tree and file content for the desktop app.
"""

import base64
import mimetypes
from pathlib import Path

from config import get_config

TEXT_EXTENSIONS = {
    ".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".cfg", ".ini", ".sh", ".bash", ".zsh", ".css", ".html", ".htm",
    ".xml", ".sql", ".env", ".gitignore", ".dockerignore", ".dockerfile",
    ".rs", ".go", ".java", ".rb", ".php", ".vue", ".svelte", ".astro",
    ".csv", ".log", ".conf", ".nginx", ".plist",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".next", ".vercel", "dist",
    ".claude", "venv", ".venv", "env", ".env", ".DS_Store",
}


def get_directory_tree(root_path: str = None, max_depth: int = 4) -> dict:
    """Get directory tree as nested structure."""
    if root_path is None:
        root_path = get_config()["file_browser_root"]

    root = Path(root_path)
    if not root.exists():
        return {}

    def walk(path: Path, depth: int = 0) -> dict | None:
        if depth > max_depth or path.name in SKIP_DIRS:
            return None

        entry = {"name": path.name, "path": str(path), "type": "directory"}
        children = []
        try:
            items = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            for item in items:
                if item.name.startswith(".") and item.name not in (".env.example",):
                    continue
                if item.is_dir():
                    child = walk(item, depth + 1)
                    if child:
                        children.append(child)
                else:
                    ext = item.suffix.lower()
                    if ext in TEXT_EXTENSIONS or ext in IMAGE_EXTENSIONS:
                        children.append({
                            "name": item.name, "path": str(item),
                            "type": "file", "extension": ext,
                            "size": item.stat().st_size,
                        })
        except PermissionError:
            pass

        entry["children"] = children
        entry["count"] = len(children)
        return entry

    return walk(root) or {}


def read_file_content(file_path: str) -> dict:
    """Read file content for preview."""
    path = Path(file_path)
    if not path.exists():
        return {"type": "error", "content": "File not found"}

    # Security: only allow files under configured root
    root = Path(get_config()["file_browser_root"])
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return {"type": "error", "content": "Access denied: outside project directory"}

    ext = path.suffix.lower()
    size = path.stat().st_size

    if size > 1_000_000:
        return {"type": "error", "content": f"File too large ({size / 1024 / 1024:.1f} MB)"}

    if ext in IMAGE_EXTENSIONS:
        if ext == ".svg":
            try:
                return {"type": "svg", "content": path.read_text(encoding="utf-8")}
            except Exception:
                pass
        try:
            data = path.read_bytes()
            mime = mimetypes.guess_type(str(path))[0] or "image/png"
            b64 = base64.b64encode(data).decode("ascii")
            return {"type": "image", "content": f"data:{mime};base64,{b64}"}
        except Exception as e:
            return {"type": "error", "content": str(e)}

    if ext in TEXT_EXTENSIONS:
        try:
            content = path.read_text(encoding="utf-8")
            lang = _get_language(ext)
            return {"type": "text", "content": content, "language": lang, "extension": ext}
        except UnicodeDecodeError:
            return {"type": "error", "content": "Binary file — cannot preview"}

    return {"type": "error", "content": f"Unsupported file type: {ext}"}


def _get_language(ext: str) -> str:
    """Map file extension to highlight.js language name."""
    mapping = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".tsx": "typescript", ".jsx": "javascript", ".json": "json",
        ".yaml": "yaml", ".yml": "yaml", ".md": "markdown",
        ".html": "html", ".css": "css", ".sql": "sql",
        ".sh": "bash", ".bash": "bash", ".zsh": "bash",
        ".xml": "xml", ".rs": "rust", ".go": "go", ".java": "java",
        ".rb": "ruby", ".php": "php", ".toml": "toml",
    }
    return mapping.get(ext, "plaintext")
