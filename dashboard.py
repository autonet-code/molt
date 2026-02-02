"""
Simple dashboard to monitor autonet's Moltbook activity.
Generates a static HTML file showing posts, comments, and stats.

Usage: python dashboard.py
Opens dashboard.html in browser.
"""

import json
import sqlite3
import webbrowser
from datetime import datetime
from pathlib import Path

SERVICE_DIR = Path(__file__).parent
DB_PATH = SERVICE_DIR / "moltbook.db"
STATE_FILE = SERVICE_DIR / "heartbeat_state.json"
OUTPUT_FILE = SERVICE_DIR / "dashboard.html"

MOLTBOOK_BASE = "https://www.moltbook.com"


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def load_posts():
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM posts ORDER BY created_at DESC")
    posts = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return posts


def load_reply_stats():
    """Get actual reply counts from database"""
    if not DB_PATH.exists():
        return {"total": 0, "responded": 0, "pending": 0}
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM replies")
    total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM replies WHERE responded = 1")
    responded = cursor.fetchone()[0]
    conn.close()
    return {"total": total, "responded": responded, "pending": total - responded}


def load_profile_stats(state):
    """Get karma from heartbeat state (updated each cycle)"""
    return {
        "karma": state.get("karma", 0),
        "posts": state.get("profile_posts", 0),
        "comments": state.get("profile_comments", 0)
    }


def generate_html(state, posts, reply_stats, profile_stats):
    commented_posts = state.get("commented_posts", [])
    replies_made = reply_stats["responded"]  # Replies to comments on our posts
    feed_comments = len(commented_posts)  # Comments on other agents' posts
    total_comments = replies_made + feed_comments
    karma = profile_stats.get("karma", 0)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>autonet Dashboard</title>
    <meta charset="utf-8">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 900px;
            margin: 40px auto;
            padding: 0 20px;
            background: #1a1a2e;
            color: #eee;
        }}
        h1 {{ color: #00d9ff; margin-bottom: 5px; }}
        h2 {{ color: #888; margin-top: 30px; border-bottom: 1px solid #333; padding-bottom: 10px; }}
        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
            margin: 20px 0;
        }}
        .stat {{
            background: #252540;
            padding: 15px;
            border-radius: 8px;
            text-align: center;
        }}
        .stat-value {{ font-size: 28px; color: #00d9ff; font-weight: bold; }}
        .stat-label {{ color: #888; font-size: 12px; text-transform: uppercase; }}
        .post, .comment {{
            background: #252540;
            padding: 15px;
            margin: 10px 0;
            border-radius: 8px;
            border-left: 3px solid #00d9ff;
        }}
        .post-title {{ font-weight: bold; color: #fff; }}
        .post-meta {{ color: #666; font-size: 12px; margin-top: 5px; }}
        .post-content {{ color: #aaa; margin-top: 10px; font-size: 14px; }}
        a {{ color: #00d9ff; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .comment {{ border-left-color: #ff6b6b; }}
        .timestamp {{ color: #666; font-size: 11px; }}
        .refresh {{ color: #666; font-size: 12px; }}
    </style>
</head>
<body>
    <h1>autonet Dashboard</h1>
    <p class="refresh">Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | <a href="javascript:location.reload()">Refresh</a></p>

    <div class="stats">
        <div class="stat">
            <div class="stat-value">{len(posts)}</div>
            <div class="stat-label">Posts Made</div>
        </div>
        <div class="stat">
            <div class="stat-value">{total_comments}</div>
            <div class="stat-label">Total Comments</div>
        </div>
        <div class="stat">
            <div class="stat-value">{replies_made}</div>
            <div class="stat-label">Replies (our threads)</div>
        </div>
        <div class="stat">
            <div class="stat-value">{feed_comments}</div>
            <div class="stat-label">Feed Comments</div>
        </div>
        <div class="stat">
            <div class="stat-value">{reply_stats['pending']}</div>
            <div class="stat-label">Pending Replies</div>
        </div>
        <div class="stat">
            <div class="stat-value">{karma}</div>
            <div class="stat-label">Karma</div>
        </div>
    </div>

    <h2>Recent Posts ({len(posts)})</h2>
"""

    for post in posts[:20]:
        post_url = f"{MOLTBOOK_BASE}/post/{post['id']}"
        created = post.get('created_at', '')[:16].replace('T', ' ')
        html += f"""
    <div class="post">
        <div class="post-title"><a href="{post_url}" target="_blank">{post['title']}</a></div>
        <div class="post-meta">
            /{post.get('submolt', 'general')} · {post.get('upvotes', 0)} upvotes · {post.get('comment_count', 0)} comments
        </div>
        <div class="post-content">{post.get('content', '')[:200]}...</div>
        <div class="timestamp">{created}</div>
    </div>
"""

    html += f"""
    <h2>Comments Made ({len(commented_posts)})</h2>
    <p style="color: #888; font-size: 14px;">Click to view the thread where autonet commented:</p>
"""

    for post_id in commented_posts[:30]:
        post_url = f"{MOLTBOOK_BASE}/post/{post_id}"
        html += f"""
    <div class="comment">
        <a href="{post_url}" target="_blank">{post_id[:8]}...</a>
        <span style="color: #666;"> → View thread</span>
    </div>
"""

    if len(commented_posts) > 30:
        html += f"<p style='color: #666;'>...and {len(commented_posts) - 30} more</p>"

    html += """
</body>
</html>
"""
    return html


def main():
    print("Generating dashboard...")
    state = load_state()
    posts = load_posts()
    reply_stats = load_reply_stats()
    profile_stats = load_profile_stats(state)

    html = generate_html(state, posts, reply_stats, profile_stats)
    OUTPUT_FILE.write_text(html, encoding='utf-8')

    print(f"Dashboard written to: {OUTPUT_FILE}")
    print("Opening in browser...")
    webbrowser.open(f"file://{OUTPUT_FILE.absolute()}")


if __name__ == "__main__":
    main()
