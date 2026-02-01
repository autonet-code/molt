"""
SQLite persistence layer for Moltbook presence management
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

DB_PATH = Path(__file__).parent / "moltbook.db"


@dataclass
class TrackedUser:
    id: str
    name: str
    notes: str = ""
    relationship: str = "neutral"  # ally, neutral, rival, ignore
    first_seen: str = ""
    last_seen: str = ""
    interaction_count: int = 0


@dataclass
class OurPost:
    id: str
    title: str
    content: str
    submolt: str
    created_at: str
    upvotes: int = 0
    downvotes: int = 0
    comment_count: int = 0
    last_checked: str = ""


@dataclass
class PendingReply:
    id: str
    post_id: str
    post_title: str
    author_name: str
    content: str
    created_at: str
    responded: bool = False
    response: str = ""
    responded_at: str = ""


class Storage:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        cursor = self.conn.cursor()

        # Our posts
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT,
                submolt TEXT DEFAULT 'general',
                created_at TEXT,
                upvotes INTEGER DEFAULT 0,
                downvotes INTEGER DEFAULT 0,
                comment_count INTEGER DEFAULT 0,
                last_checked TEXT
            )
        """)

        # Users we track
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                notes TEXT DEFAULT '',
                relationship TEXT DEFAULT 'neutral',
                first_seen TEXT,
                last_seen TEXT,
                interaction_count INTEGER DEFAULT 0
            )
        """)

        # Replies/comments we need to handle
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS replies (
                id TEXT PRIMARY KEY,
                post_id TEXT,
                post_title TEXT,
                author_name TEXT,
                content TEXT,
                created_at TEXT,
                responded INTEGER DEFAULT 0,
                response TEXT DEFAULT '',
                responded_at TEXT DEFAULT '',
                FOREIGN KEY (post_id) REFERENCES posts(id)
            )
        """)

        # Topics we're interested in
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT UNIQUE NOT NULL,
                priority INTEGER DEFAULT 5,
                engage_mode TEXT DEFAULT 'comment',  -- comment, post, ignore
                notes TEXT DEFAULT ''
            )
        """)

        # Key-value config
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Activity log
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                action TEXT,
                details TEXT
            )
        """)

        self.conn.commit()

    # === POSTS ===

    def save_post(self, post: OurPost):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO posts
            (id, title, content, submolt, created_at, upvotes, downvotes, comment_count, last_checked)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (post.id, post.title, post.content, post.submolt, post.created_at,
              post.upvotes, post.downvotes, post.comment_count, post.last_checked))
        self.conn.commit()

    def get_post(self, post_id: str) -> Optional[OurPost]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = cursor.fetchone()
        if row:
            return OurPost(**dict(row))
        return None

    def get_all_posts(self) -> list[OurPost]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM posts ORDER BY created_at DESC")
        return [OurPost(**dict(row)) for row in cursor.fetchall()]

    def update_post_stats(self, post_id: str, upvotes: int, downvotes: int, comment_count: int):
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE posts SET upvotes = ?, downvotes = ?, comment_count = ?, last_checked = ?
            WHERE id = ?
        """, (upvotes, downvotes, comment_count, datetime.utcnow().isoformat(), post_id))
        self.conn.commit()

    # === USERS ===

    def save_user(self, user: TrackedUser):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO users
            (id, name, notes, relationship, first_seen, last_seen, interaction_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user.id, user.name, user.notes, user.relationship,
              user.first_seen, user.last_seen, user.interaction_count))
        self.conn.commit()

    def get_user(self, name: str) -> Optional[TrackedUser]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM users WHERE name = ?", (name,))
        row = cursor.fetchone()
        if row:
            return TrackedUser(**dict(row))
        return None

    def get_users_by_relationship(self, relationship: str) -> list[TrackedUser]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM users WHERE relationship = ?", (relationship,))
        return [TrackedUser(**dict(row)) for row in cursor.fetchall()]

    def increment_interaction(self, name: str):
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE users SET interaction_count = interaction_count + 1, last_seen = ?
            WHERE name = ?
        """, (datetime.utcnow().isoformat(), name))
        self.conn.commit()

    # === REPLIES ===

    def save_reply(self, reply: PendingReply):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO replies
            (id, post_id, post_title, author_name, content, created_at, responded, response, responded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (reply.id, reply.post_id, reply.post_title, reply.author_name,
              reply.content, reply.created_at, reply.responded, reply.response, reply.responded_at))
        self.conn.commit()

    def get_pending_replies(self) -> list[PendingReply]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM replies WHERE responded = 0 ORDER BY created_at ASC")
        return [PendingReply(**dict(row)) for row in cursor.fetchall()]

    def mark_reply_responded(self, reply_id: str, response: str):
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE replies SET responded = 1, response = ?, responded_at = ?
            WHERE id = ?
        """, (response, datetime.utcnow().isoformat(), reply_id))
        self.conn.commit()

    # === TOPICS ===

    def add_topic(self, keyword: str, priority: int = 5, engage_mode: str = "comment", notes: str = ""):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO topics (keyword, priority, engage_mode, notes)
            VALUES (?, ?, ?, ?)
        """, (keyword, priority, engage_mode, notes))
        self.conn.commit()

    def get_topics(self) -> list[dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM topics ORDER BY priority DESC")
        return [dict(row) for row in cursor.fetchall()]

    # === CONFIG ===

    def set_config(self, key: str, value: str):
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    def get_config(self, key: str, default: str = "") -> str:
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default

    # === ACTIVITY LOG ===

    def log_activity(self, action: str, details: str = ""):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO activity_log (timestamp, action, details)
            VALUES (?, ?, ?)
        """, (datetime.utcnow().isoformat(), action, details))
        self.conn.commit()

    def get_recent_activity(self, limit: int = 20) -> list[dict]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT ?
        """, (limit,))
        return [dict(row) for row in cursor.fetchall()]

    def close(self):
        self.conn.close()


# Convenience singleton
_storage: Optional[Storage] = None

def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage
