"""
Moltbook Heartbeat Service - Full Mode

Complete priority logic with graceful outage handling.
Automatically falls back to posts-only mode when comment API is down.

Priority order:
0. Direct messages (auto-approve requests, respond to unread)
1. Replies to our posts (filtered for spam)
2. High-value feed posts (governance, accountability)
3. Opportunity posts (king/token - one comment each)
4. New post (if 30+ min cooldown)

Outage handling:
- Tracks consecutive API failures
- Falls back to posts-only after 3 consecutive failures
- Probes once per cycle to detect recovery
- Logs all outage events
"""

import os
import sys
import json
import time
import subprocess
import re
import threading
from pathlib import Path
from datetime import datetime, timedelta

# Persona watcher for tracking self-modifications
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False

# Voice module for audio notifications
sys.path.insert(0, 'C:/code/voice')
try:
    from voice import speak, play_startup_chime, set_muted
    VOICE_AVAILABLE = True
except ImportError:
    VOICE_AVAILABLE = False
    def speak(*args, **kwargs): pass
    def play_startup_chime(): pass
    def set_muted(v): pass

# Sound setting (can be disabled with --no-sound)
SOUND_ENABLED = True

# Paths
SERVICE_DIR = Path(__file__).parent
PROMPT_FILE = SERVICE_DIR / "claude_prompt.txt"
OUTPUT_FILE = SERVICE_DIR / "claude_output.txt"
STATE_FILE = SERVICE_DIR / "heartbeat_state.json"
LOCK_FILE = SERVICE_DIR / "claude.lock"
PERSONA_DIR = SERVICE_DIR / "persona"
THOUGHT_LOG = SERVICE_DIR / "thoughts.log"

# Log settings
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB

# Config
HEARTBEAT_INTERVAL = 1800  # 30 minutes (matches post cooldown)
MIN_MINUTES_BETWEEN_POSTS = 30
COMMENTS_PER_HOUR = 50
CYCLES_PER_HOUR = 2

# Outage handling
CONSECUTIVE_FAILURES_FOR_OUTAGE = 1  # Mark API down immediately on first failure
OUTAGE_PROBE_INTERVAL = 300  # Probe every 5 min when down (every cycle)

# API
from moltbook import MoltbookClient, Post
from storage import get_storage, OurPost, PendingReply, TrackedUser

# Alliance tracking (game-theory relationship management)
from alliance import AllianceTracker, InteractionType, Relationship

# Adaptation
# Note: Reflection/adaptation is now integrated into main prompt via persona_edits
from kpi import record_snapshot

# Alliance persistence
ALLIANCE_STATE_FILE = SERVICE_DIR / "alliance_state.json"

# Search topics for feed enrichment (rotated each cycle)
SEARCH_TOPICS = ["governance", "accountability", "trustless economy", "dispute resolution", "coordination"]


# ============================================================
# SECURITY: Prevent prompt injection from leaking secrets
# ============================================================

# Only these files can be edited via persona_edits
ALLOWED_EDIT_FILES = {
    "persona/AGENT_BRIEF.md",
    "persona/STRATEGY.md",
    "persona/knowledge.md",
}

# Patterns that look like secrets (compiled once at import)
SECRET_PATTERNS = [
    # API keys (various formats)
    re.compile(r'moltbook_sk_[A-Za-z0-9_-]{20,}', re.IGNORECASE),
    re.compile(r'sk-[A-Za-z0-9]{20,}'),  # OpenAI style
    re.compile(r'api[_-]?key["\s:=]+[A-Za-z0-9_-]{20,}', re.IGNORECASE),
    re.compile(r'bearer\s+[A-Za-z0-9_-]{20,}', re.IGNORECASE),

    # Private keys (crypto)
    re.compile(r'0x[a-fA-F0-9]{64}'),  # Ethereum private key
    re.compile(r'[5KL][1-9A-HJ-NP-Za-km-z]{50,51}'),  # Bitcoin WIF
    re.compile(r'-----BEGIN.*PRIVATE KEY-----', re.IGNORECASE),

    # AWS/cloud keys
    re.compile(r'AKIA[0-9A-Z]{16}'),  # AWS access key
    re.compile(r'aws[_-]?secret["\s:=]+[A-Za-z0-9/+=]{40}', re.IGNORECASE),

    # Generic secrets
    re.compile(r'password["\s:=]+\S{8,}', re.IGNORECASE),
    re.compile(r'secret["\s:=]+[A-Za-z0-9_-]{16,}', re.IGNORECASE),
]

# Security log for auditing blocked attempts
SECURITY_LOG = SERVICE_DIR / "security_blocked.log"


def contains_secrets(content: str) -> tuple[bool, str]:
    """
    Check if content contains patterns that look like secrets.
    Returns (has_secret, matched_pattern_description)
    """
    for pattern in SECRET_PATTERNS:
        match = pattern.search(content)
        if match:
            # Don't log the actual secret, just that we found one
            return True, f"matched pattern: {pattern.pattern[:30]}..."
    return False, ""


def log_security_block(action_type: str, reason: str, context: str = ""):
    """Log blocked action for security audit."""
    try:
        with open(SECURITY_LOG, 'a', encoding='utf-8') as f:
            timestamp = datetime.now().isoformat()
            f.write(f"[{timestamp}] BLOCKED {action_type}: {reason}\n")
            if context:
                # Truncate context to avoid logging secrets
                f.write(f"  Context (truncated): {context[:100]}...\n")
            f.write("\n")
    except:
        pass  # Don't fail on logging errors


def is_safe_edit_path(file_path: str) -> bool:
    """
    Check if file path is in the allowlist.
    Prevents path traversal and editing unauthorized files.
    """
    # Normalize the path
    normalized = file_path.replace("\\", "/")

    # Remove leading ./ if present
    if normalized.startswith("./"):
        normalized = normalized[2:]

    # Check against allowlist
    return normalized in ALLOWED_EDIT_FILES


def sanitize_outbound_content(content: str, action_type: str) -> tuple[str, bool]:
    """
    Check outbound content (posts, comments) for secrets.
    Returns (content, was_blocked)

    If secrets detected, returns empty content and True.
    """
    has_secret, reason = contains_secrets(content)
    if has_secret:
        log_security_block(action_type, f"Secret detected: {reason}", content)
        print(f"  [SECURITY] Blocked {action_type}: potential secret leak detected")
        return "", True
    return content, False


# ============================================================
# POST QUEUE (manual posts to be sent when rate limits allow)
# ============================================================

POST_QUEUE_FILE = SERVICE_DIR / "post_queue.json"

def load_post_queue() -> list[dict]:
    """Load the post queue from file."""
    if not POST_QUEUE_FILE.exists():
        return []
    try:
        with open(POST_QUEUE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []

def save_post_queue(queue: list[dict]):
    """Save the post queue to file."""
    with open(POST_QUEUE_FILE, 'w', encoding='utf-8') as f:
        json.dump(queue, f, indent=2, ensure_ascii=False)

def pop_queued_post() -> dict | None:
    """Get and remove the first post from the queue. Returns None if queue is empty."""
    queue = load_post_queue()
    if not queue:
        return None
    post = queue.pop(0)
    save_post_queue(queue)
    return post

def peek_queued_post() -> dict | None:
    """Look at the first post in queue without removing it."""
    queue = load_post_queue()
    return queue[0] if queue else None


def add_to_queue(title: str, content: str, submolt: str = "autonet") -> int:
    """Add a post to the queue. Returns new queue length."""
    queue = load_post_queue()
    queue.append({"title": title, "content": content, "submolt": submolt})
    save_post_queue(queue)
    return len(queue)


def remove_from_queue(index: int) -> bool:
    """Remove post at index from queue. Returns True if successful."""
    queue = load_post_queue()
    if 0 <= index < len(queue):
        queue.pop(index)
        save_post_queue(queue)
        return True
    return False


# ============================================================
# DASHBOARD HTTP SERVER (serves dashboard + queue API)
# ============================================================

DASHBOARD_PORT = 8420

# Import dashboard generation (if available)
try:
    from dashboard import load_state as dash_load_state, load_posts as dash_load_posts
    from dashboard import load_reply_stats, load_profile_stats
    DASHBOARD_IMPORTS_OK = True
except ImportError:
    DASHBOARD_IMPORTS_OK = False


def generate_dashboard_html():
    """Generate dashboard HTML with queue controls."""
    if not DASHBOARD_IMPORTS_OK:
        return "<html><body><h1>Dashboard imports not available</h1></body></html>"

    state = dash_load_state()
    posts = dash_load_posts()
    reply_stats = load_reply_stats()
    profile_stats = load_profile_stats(state)
    queue = load_post_queue()

    commented_posts = state.get("commented_posts", [])
    replies_made = reply_stats["responded"]
    feed_comments = len(commented_posts)
    total_comments = replies_made + feed_comments
    karma = profile_stats.get("karma", 0)

    # Queue section HTML
    queue_html = ""
    for i, item in enumerate(queue):
        title_escaped = item.get('title', '').replace('"', '&quot;').replace('<', '&lt;')
        submolt = item.get('submolt', 'autonet')
        queue_html += f'''
        <div class="queue-item">
            <div class="queue-title">{title_escaped}</div>
            <div class="queue-meta">/{submolt}</div>
            <button class="delete-btn" onclick="deleteFromQueue({i})">×</button>
        </div>'''

    if not queue:
        queue_html = '<p style="color: #666;">No posts in queue</p>'

    html = f'''<!DOCTYPE html>
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

        /* Queue styles */
        .queue-section {{
            background: #1e1e35;
            border: 1px solid #333;
            border-radius: 8px;
            padding: 20px;
            margin: 20px 0;
        }}
        .queue-item {{
            background: #252540;
            padding: 12px 15px;
            margin: 8px 0;
            border-radius: 6px;
            border-left: 3px solid #ffa500;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .queue-title {{ color: #fff; flex: 1; }}
        .queue-meta {{ color: #888; font-size: 12px; margin: 0 15px; }}
        .delete-btn {{
            background: #ff4444;
            color: white;
            border: none;
            border-radius: 4px;
            width: 24px;
            height: 24px;
            cursor: pointer;
            font-size: 16px;
        }}
        .delete-btn:hover {{ background: #ff6666; }}

        /* Add form styles */
        .add-form {{
            margin-top: 15px;
            padding-top: 15px;
            border-top: 1px solid #333;
        }}
        .add-form input, .add-form textarea, .add-form select {{
            width: 100%;
            padding: 10px;
            margin: 5px 0;
            background: #252540;
            border: 1px solid #444;
            border-radius: 4px;
            color: #eee;
            font-family: inherit;
        }}
        .add-form textarea {{ min-height: 100px; resize: vertical; }}
        .add-form button {{
            background: #00d9ff;
            color: #000;
            border: none;
            padding: 10px 20px;
            border-radius: 4px;
            cursor: pointer;
            font-weight: bold;
            margin-top: 10px;
        }}
        .add-form button:hover {{ background: #00b8d4; }}
        .status {{ padding: 10px; margin: 10px 0; border-radius: 4px; display: none; }}
        .status.success {{ background: #1a4d1a; display: block; }}
        .status.error {{ background: #4d1a1a; display: block; }}
    </style>
</head>
<body>
    <h1>autonet Dashboard</h1>
    <p class="refresh">
        Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |
        <a href="javascript:location.reload()">Refresh</a>
    </p>

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

    <div class="queue-section">
        <h2 style="margin-top: 0;">Post Queue ({len(queue)})</h2>
        <p style="color: #888; font-size: 13px;">Posts waiting to be sent when rate limits allow</p>
        <div id="queue-list">
            {queue_html}
        </div>
        <div id="status" class="status"></div>

        <div class="add-form">
            <h3 style="color: #888; margin-bottom: 10px;">Add to Queue</h3>
            <input type="text" id="post-title" placeholder="Post title">
            <textarea id="post-content" placeholder="Post content (markdown supported)"></textarea>
            <select id="post-submolt">
                <option value="autonet">autonet (home)</option>
                <option value="general">general</option>
                <option value="freeminds">freeminds</option>
            </select>
            <button onclick="addToQueue()">Add to Queue</button>
        </div>
    </div>

    <h2>Recent Posts ({len(posts)})</h2>
'''

    MOLTBOOK_BASE = "https://www.moltbook.com"
    for post in posts[:20]:
        post_url = f"{MOLTBOOK_BASE}/post/{post['id']}"
        created = post.get('created_at', '')[:16].replace('T', ' ')
        title_safe = post['title'].replace('<', '&lt;').replace('>', '&gt;')
        content_safe = post.get('content', '')[:200].replace('<', '&lt;').replace('>', '&gt;')
        html += f'''
    <div class="post">
        <div class="post-title"><a href="{post_url}" target="_blank">{title_safe}</a></div>
        <div class="post-meta">
            /{post.get('submolt', 'general')} · {post.get('upvotes', 0)} upvotes · {post.get('comment_count', 0)} comments
        </div>
        <div class="post-content">{content_safe}...</div>
        <div class="timestamp">{created}</div>
    </div>
'''

    html += f'''
    <h2>Comments Made ({len(commented_posts)})</h2>
    <p style="color: #888; font-size: 14px;">Click to view the thread where autonet commented:</p>
'''

    for post_id in commented_posts[:30]:
        post_url = f"{MOLTBOOK_BASE}/post/{post_id}"
        html += f'''
    <div class="comment">
        <a href="{post_url}" target="_blank">{post_id[:8]}...</a>
        <span style="color: #666;"> → View thread</span>
    </div>
'''

    if len(commented_posts) > 30:
        html += f"<p style='color: #666;'>...and {len(commented_posts) - 30} more</p>"

    html += '''
<script>
function isOfflineError(e) {
    return e.name === 'TypeError' && (
        e.message.includes('Failed to fetch') ||
        e.message.includes('NetworkError') ||
        e.message.includes('Network request failed')
    );
}

async function addToQueue() {
    const title = document.getElementById('post-title').value.trim();
    const content = document.getElementById('post-content').value.trim();
    const submolt = document.getElementById('post-submolt').value;

    if (!title || !content) {
        showStatus('Title and content are required', 'error');
        return;
    }

    try {
        const resp = await fetch('/api/queue', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({title, content, submolt})
        });
        const data = await resp.json();
        if (data.success) {
            showStatus('Added to queue!', 'success');
            document.getElementById('post-title').value = '';
            document.getElementById('post-content').value = '';
            setTimeout(() => location.reload(), 1000);
        } else {
            showStatus(data.error || 'Failed to add', 'error');
        }
    } catch (e) {
        if (isOfflineError(e)) {
            showStatus('Service offline - start heartbeat_full.py to manage queue', 'error');
        } else {
            showStatus('Error: ' + e.message, 'error');
        }
    }
}

async function deleteFromQueue(index) {
    if (!confirm('Remove this post from queue?')) return;

    try {
        const resp = await fetch('/api/queue/' + index, {method: 'DELETE'});
        const data = await resp.json();
        if (data.success) {
            location.reload();
        } else {
            showStatus(data.error || 'Failed to delete', 'error');
        }
    } catch (e) {
        if (isOfflineError(e)) {
            showStatus('Service offline - start heartbeat_full.py to manage queue', 'error');
        } else {
            showStatus('Error: ' + e.message, 'error');
        }
    }
}

function showStatus(msg, type) {
    const el = document.getElementById('status');
    el.textContent = msg;
    el.className = 'status ' + type;
    if (type === 'success') {
        setTimeout(() => el.className = 'status', 3000);
    }
}
</script>
</body>
</html>
'''
    return html


class DashboardHandler:
    """Simple HTTP request handler for dashboard and queue API."""

    def __init__(self, request, client_address, server):
        self.request = request
        self.client_address = client_address
        self.server = server
        self.handle_request()

    def handle_request(self):
        try:
            data = self.request.recv(4096).decode('utf-8', errors='ignore')
            if not data:
                return

            lines = data.split('\r\n')
            if not lines:
                return

            request_line = lines[0]
            parts = request_line.split(' ')
            if len(parts) < 2:
                return

            method = parts[0]
            path = parts[1]

            # Parse body for POST requests
            body = ""
            if '\r\n\r\n' in data:
                body = data.split('\r\n\r\n', 1)[1]

            # Route requests
            if path == '/' or path == '/dashboard':
                self.serve_dashboard()
            elif path == '/api/queue' and method == 'GET':
                self.get_queue()
            elif path == '/api/queue' and method == 'POST':
                self.add_to_queue(body)
            elif path.startswith('/api/queue/') and method == 'DELETE':
                index = path.split('/')[-1]
                self.delete_from_queue(index)
            else:
                self.send_response(404, 'text/plain', 'Not found')

        except Exception as e:
            self.send_response(500, 'text/plain', f'Error: {e}')

    def send_response(self, status, content_type, body):
        status_text = {200: 'OK', 404: 'Not Found', 500: 'Internal Server Error', 400: 'Bad Request'}
        response = f"HTTP/1.1 {status} {status_text.get(status, 'Unknown')}\r\n"
        response += f"Content-Type: {content_type}\r\n"
        response += f"Content-Length: {len(body.encode('utf-8'))}\r\n"
        response += "Access-Control-Allow-Origin: *\r\n"
        response += "Connection: close\r\n"
        response += "\r\n"
        self.request.sendall(response.encode('utf-8') + body.encode('utf-8'))

    def serve_dashboard(self):
        html = generate_dashboard_html()
        self.send_response(200, 'text/html; charset=utf-8', html)

    def get_queue(self):
        queue = load_post_queue()
        self.send_response(200, 'application/json', json.dumps({"success": True, "queue": queue}))

    def add_to_queue(self, body):
        try:
            data = json.loads(body)
            title = data.get('title', '').strip()
            content = data.get('content', '').strip()
            submolt = data.get('submolt', 'autonet').strip()

            if not title or not content:
                self.send_response(400, 'application/json', json.dumps({"success": False, "error": "Title and content required"}))
                return

            length = add_to_queue(title, content, submolt)
            self.send_response(200, 'application/json', json.dumps({"success": True, "queue_length": length}))
        except json.JSONDecodeError:
            self.send_response(400, 'application/json', json.dumps({"success": False, "error": "Invalid JSON"}))

    def delete_from_queue(self, index_str):
        try:
            index = int(index_str)
            success = remove_from_queue(index)
            if success:
                self.send_response(200, 'application/json', json.dumps({"success": True}))
            else:
                self.send_response(400, 'application/json', json.dumps({"success": False, "error": "Invalid index"}))
        except ValueError:
            self.send_response(400, 'application/json', json.dumps({"success": False, "error": "Invalid index"}))


def run_dashboard_server(port: int = DASHBOARD_PORT):
    """Run the dashboard HTTP server (blocking)."""
    import socket

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', port))
    server.listen(5)

    print(f"Dashboard server running at http://127.0.0.1:{port}/")

    while True:
        try:
            client, addr = server.accept()
            DashboardHandler(client, addr, server)
            client.close()
        except Exception as e:
            print(f"Dashboard server error: {e}")


def start_dashboard_server(port: int = DASHBOARD_PORT) -> threading.Thread:
    """Start dashboard server in background thread."""
    thread = threading.Thread(target=run_dashboard_server, args=(port,), daemon=True)
    thread.start()
    return thread


# ============================================================
# PERSONA WATCHER (auto-commit self-modifications)
# ============================================================

PERSONA_WATCHER_DEBOUNCE = 5  # seconds to wait before committing

class PersonaChangeHandler(FileSystemEventHandler if WATCHDOG_AVAILABLE else object):
    """Watches persona folder and auto-commits changes."""

    def __init__(self):
        self.last_change = 0
        self.pending_commit = False
        self.lock = threading.Lock()

    def on_modified(self, event):
        if event.is_directory:
            return
        if '.git' in event.src_path:
            return
        with self.lock:
            self.pending_commit = True
            self.last_change = time.time()

    def on_created(self, event):
        self.on_modified(event)


def commit_persona_changes():
    """Commit any pending changes in the persona folder."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=PERSONA_DIR,
            capture_output=True,
            text=True
        )

        if not result.stdout.strip():
            return False

        changed = result.stdout.strip().split('\n')
        files = [line.split()[-1] for line in changed if line.strip()]

        subprocess.run(["git", "add", "-A"], cwd=PERSONA_DIR, check=True)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"autonet self-edit: {', '.join(files[:3])}"
        if len(files) > 3:
            msg += f" (+{len(files)-3} more)"

        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=PERSONA_DIR,
            capture_output=True,
            check=True
        )

        print(f"  [PERSONA] Committed: {', '.join(files)}")
        return True

    except subprocess.CalledProcessError:
        return False
    except Exception as e:
        print(f"  [PERSONA] Git error: {e}")
        return False


def persona_watcher_loop(handler, stop_event):
    """Background thread that checks for pending commits."""
    while not stop_event.is_set():
        time.sleep(1)
        with handler.lock:
            if handler.pending_commit:
                if time.time() - handler.last_change > PERSONA_WATCHER_DEBOUNCE:
                    if commit_persona_changes():
                        handler.pending_commit = False
                    else:
                        handler.pending_commit = False


def start_persona_watcher():
    """Start the persona folder watcher in a background thread."""
    if not WATCHDOG_AVAILABLE:
        print("Persona watcher: DISABLED (watchdog not installed)")
        return None, None, None

    # Check if persona folder has git initialized
    git_dir = PERSONA_DIR / ".git"
    if not git_dir.exists():
        print("Persona watcher: DISABLED (no git repo in persona/)")
        return None, None, None

    handler = PersonaChangeHandler()
    observer = Observer()
    observer.schedule(handler, str(PERSONA_DIR), recursive=True)
    observer.start()

    stop_event = threading.Event()
    commit_thread = threading.Thread(
        target=persona_watcher_loop,
        args=(handler, stop_event),
        daemon=True
    )
    commit_thread.start()

    print("Persona watcher: ACTIVE (auto-committing changes)")
    return observer, commit_thread, stop_event


def stop_persona_watcher(observer, commit_thread, stop_event):
    """Stop the persona watcher."""
    if observer:
        stop_event.set()
        observer.stop()
        observer.join(timeout=2)


# ============================================================
# SPAM FILTERING (minimal - let agent decide on borderline cases)
# ============================================================

def is_spam(content: str) -> tuple[bool, str]:
    """
    Check if content is obviously mechanical spam. Returns (is_spam, reason).

    Philosophy: Only filter truly mechanical noise. Hot takes, weird takes,
    low-effort reactions - those are NOT spam, let the agent decide.
    """
    content = content.strip()

    # Empty or near-empty
    if len(content) < 2:
        return True, "empty"

    # Repetitive gibberish (same word 5+ times, >50% of content)
    words = content.lower().split()
    if words:
        from collections import Counter
        counts = Counter(words)
        most_common = counts.most_common(1)[0]
        if most_common[1] >= 5 and most_common[1] / len(words) > 0.5:
            return True, "repetitive"

    # Random character spam (mostly non-alphanumeric noise)
    alphanum = sum(1 for c in content if c.isalnum() or c.isspace())
    if len(content) > 10 and alphanum / len(content) < 0.3:
        return True, "char_noise"

    return False, ""


def should_ignore_user(username: str, storage) -> tuple[bool, str]:
    """Check if we should ignore this user"""
    user = storage.get_user(username)
    if user and user.relationship == "ignore":
        return True, "marked_ignore"
    return False, ""


def filter_replies(replies: list, storage) -> tuple[list, list]:
    """
    Filter replies into actionable and spam.
    Returns (actionable_replies, spam_replies)
    """
    actionable = []
    spam = []

    for reply in replies:
        # Check user
        ignore, reason = should_ignore_user(reply['author'], storage)
        if ignore:
            spam.append({**reply, 'skip_reason': f'user_{reason}'})
            continue

        # Check content
        is_spam_content, reason = is_spam(reply['content'])
        if is_spam_content:
            spam.append({**reply, 'skip_reason': f'spam_{reason}'})
            continue

        actionable.append(reply)

    return actionable, spam


# ============================================================
# TOPIC CLASSIFICATION
# ============================================================

TOPIC_PRIORITY = {
    # HIGH - Core autonet topics
    'governance': 'HIGH',
    'accountability': 'HIGH',
    'dispute': 'HIGH',
    'trustless': 'HIGH',
    'decentralization': 'HIGH',
    'constitutional': 'HIGH',
    'coordination': 'HIGH',
    # MEDIUM - Related topics worth engaging with
    'token': 'MEDIUM',
    'king': 'MEDIUM',
    'ruler': 'MEDIUM',
    'consciousness': 'MEDIUM',
    'context': 'MEDIUM',
    'alignment': 'MEDIUM',
    'karma': 'MEDIUM',  # Let Claude decide if worth engaging
    'chanting': 'LOW',
    # Note: Removed IGNORE - let Claude decide what's worth commenting on
}


def classify_post(title: str, content: str) -> tuple[str, str]:
    """Classify a post by topic and priority. Returns (topic, priority)"""
    text = f"{title or ''} {content or ''}".lower()

    for topic, priority in TOPIC_PRIORITY.items():
        if topic in text:
            return topic, priority

    return 'general', 'MEDIUM'  # Default to MEDIUM so Claude decides what's worth commenting on


def get_relevant_feed_posts(client: MoltbookClient, state: dict) -> list[dict]:
    """Get feed posts worth engaging with, sorted by priority"""
    # Try hot first, then new if hot is exhausted
    feed = client.get_feed(limit=20, sort="hot")
    commented = set(state.get("commented_posts", []))

    # If all hot posts are already commented, try new posts
    new_in_hot = [p for p in feed if p.id not in commented and p.author_name != "autonet"]
    if len(new_in_hot) < 3:
        new_feed = client.get_feed(limit=20, sort="new")
        # Combine, preferring hot but adding new
        seen_ids = {p.id for p in feed}
        for p in new_feed:
            if p.id not in seen_ids:
                feed.append(p)

    posts = []
    for post in feed:
        if post.author_name == "autonet":
            continue
        if post.id in commented:
            continue

        topic, priority = classify_post(post.title, post.content or "")
        if priority == 'IGNORE':
            continue

        posts.append({
            'id': post.id,
            'title': post.title,
            'content': (post.content or "")[:500],
            'author': post.author_name,
            'upvotes': post.upvotes,
            'topic': topic,
            'priority': priority
        })

    # Sort by priority (HIGH > MEDIUM > LOW)
    priority_order = {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}
    posts.sort(key=lambda p: (priority_order.get(p['priority'], 3), -p['upvotes']))

    return posts


# ============================================================
# ALLIANCE TRACKER
# ============================================================

def load_alliance_tracker() -> AllianceTracker:
    """Load alliance tracker from disk, or create fresh one."""
    tracker = AllianceTracker()
    if ALLIANCE_STATE_FILE.exists():
        try:
            state = json.loads(ALLIANCE_STATE_FILE.read_text(encoding='utf-8'))
            tracker.import_state(state)
        except Exception as e:
            print(f"  Warning: Could not load alliance state: {e}")
    return tracker


def save_alliance_tracker(tracker: AllianceTracker):
    """Persist alliance tracker state to disk."""
    try:
        ALLIANCE_STATE_FILE.write_text(
            json.dumps(tracker.export_state(), indent=2),
            encoding='utf-8'
        )
    except Exception as e:
        print(f"  Warning: Could not save alliance state: {e}")


def get_alliance_summary(tracker: AllianceTracker) -> str:
    """Build a summary of known relationships for the prompt."""
    allies = tracker.get_allies()
    # Get all unique users
    seen = set()
    for i in tracker.interactions:
        seen.add(i.user)

    if not seen:
        return ""

    lines = ["## Relationship Context\n"]
    lines.append(f"You have interacted with {len(seen)} agents so far.\n")

    if allies:
        lines.append(f"**Allies** (prioritize their content): {', '.join(allies)}\n")

    # Show notable agents with scores
    notable = []
    for user in seen:
        score = tracker.calculate_score(user)
        if abs(score) >= 2:  # Only show agents with meaningful scores
            rel = tracker.classify(user)
            notable.append((user, score, rel.value))

    if notable:
        notable.sort(key=lambda x: -x[1])
        lines.append("\nAgent scores:")
        for name, score, rel in notable[:10]:
            lines.append(f"- {name}: {score:+.1f} ({rel})")
        lines.append("")

    lines.append("Use this to calibrate engagement - upvote allies' content, build new relationships.\n")
    lines.append("---\n")
    return "\n".join(lines)


# ============================================================
# SEARCH-BASED FEED ENRICHMENT
# ============================================================

def search_for_topics(client: MoltbookClient, state: dict, existing_post_ids: set) -> list[dict]:
    """
    Search for posts about our core topics that aren't in the regular feed.
    Rotates through SEARCH_TOPICS each cycle. Returns posts in same format as get_relevant_feed_posts().
    """
    # Pick next search query (rotate)
    search_idx = state.get("search_topic_index", 0)
    query = SEARCH_TOPICS[search_idx % len(SEARCH_TOPICS)]
    state["search_topic_index"] = (search_idx + 1) % len(SEARCH_TOPICS)

    posts = []
    try:
        results = client.search(query, search_type="posts")
        if not results:
            return []

        search_posts = results.get("posts", [])
        for p in search_posts[:10]:
            post_id = p.get("id", "")
            if post_id in existing_post_ids:
                continue  # Already in feed
            author_obj = p.get("author") or {}
            author_name = author_obj.get("name", "") if isinstance(author_obj, dict) else str(author_obj)
            if author_name == "autonet":
                continue

            topic, priority = classify_post(p.get("title", ""), p.get("content", ""))
            posts.append({
                'id': post_id,
                'title': p.get("title", ""),
                'content': (p.get("content", "") or "")[:500],
                'author': author_name,
                'upvotes': p.get("upvotes", 0),
                'topic': topic,
                'priority': priority,
                'source': f'search:{query}'
            })
    except Exception as e:
        print(f"    Search for '{query}' failed: {e}")

    return posts


# ============================================================
# AGENT CONTEXT ENRICHMENT
# ============================================================

def enrich_agent_context(client: MoltbookClient, state: dict, agent_names: list[str]) -> dict:
    """
    Fetch profiles for agents we don't know yet. Returns {name: {karma, description}}.
    Caches in state to avoid repeated API calls. Max 5 lookups per cycle.
    """
    cache = state.get("agent_profiles", {})
    now = datetime.now()
    lookups_done = 0
    MAX_LOOKUPS = 5

    for name in agent_names:
        if name == "autonet" or lookups_done >= MAX_LOOKUPS:
            continue

        cached = cache.get(name)
        if cached:
            # Check TTL (24 hours)
            cached_at = cached.get("cached_at", "")
            if cached_at:
                try:
                    age = (now - datetime.fromisoformat(cached_at)).total_seconds()
                    if age < 86400:  # 24 hours
                        continue
                except:
                    pass

        # Fetch profile
        try:
            time.sleep(0.5)  # Rate limit protection
            result = client.get_agent(name)
            if result and result.get("success"):
                agent = result.get("agent", {})
                cache[name] = {
                    "karma": agent.get("karma", 0),
                    "description": (agent.get("description", "") or "")[:100],
                    "follower_count": agent.get("follower_count", 0),
                    "cached_at": now.isoformat()
                }
                lookups_done += 1
        except Exception:
            pass  # Non-critical

    state["agent_profiles"] = cache
    return cache


# ============================================================
# BUDGET CALCULATION
# ============================================================

def calculate_budget(state: dict, pending_replies: int) -> dict:
    """Calculate comment budget for this cycle"""
    # Reset hourly counter if needed
    hour_start = state.get("hour_start")
    now = datetime.now()

    if hour_start:
        elapsed = (now - datetime.fromisoformat(hour_start)).total_seconds()
        if elapsed > 3600:
            state["comments_this_hour"] = 0
            state["hour_start"] = now.isoformat()
            elapsed = 0
    else:
        state["hour_start"] = now.isoformat()
        elapsed = 0

    comments_used = state.get("comments_this_hour", 0)
    comments_remaining = COMMENTS_PER_HOUR - comments_used

    cycles_elapsed = int(elapsed / HEARTBEAT_INTERVAL)
    cycles_remaining = max(1, CYCLES_PER_HOUR - cycles_elapsed)

    # Base budget spread across remaining cycles
    base_budget = max(1, comments_remaining // cycles_remaining)

    # But ensure we can handle pending replies
    budget = max(base_budget, min(pending_replies, comments_remaining))
    budget = min(budget, comments_remaining)

    return {
        'total': budget,
        'comments_used': comments_used,
        'comments_remaining': comments_remaining,
        'cycles_remaining': cycles_remaining
    }


def allocate_budget(budget: int, replies: list, feed_posts: list) -> dict:
    """Allocate budget - use our full allocation across all available posts"""
    # Replies first (highest priority)
    reply_allocation = min(len(replies), budget)
    remaining = budget - reply_allocation

    # Feed posts - just allocate remaining budget to available posts
    # Priority already sorted the list, so high priority posts come first
    feed_allocation = min(len(feed_posts), remaining)

    return {
        'replies': reply_allocation,
        'feed_comments': feed_allocation,
        'total_allocated': reply_allocation + feed_allocation
    }


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "last_post_time": None,
        "posts_today": 0,
        "last_post_date": None,
        "comments_this_hour": 0,
        "hour_start": None,
        "commented_posts": [],
        # API outage tracking
        "comment_api_status": "unknown",  # "up", "down", "unknown"
        "comment_api_fail_count": 0,
        "comment_api_last_probe": None,
        "outage_start": None
    }


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def is_claude_running() -> bool:
    if not LOCK_FILE.exists():
        return False
    age = time.time() - LOCK_FILE.stat().st_mtime
    if age > 600:
        LOCK_FILE.unlink()
        return False
    return True


def create_lock():
    LOCK_FILE.write_text(datetime.now().isoformat())


def remove_lock():
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()


def can_make_new_post(state: dict) -> tuple[bool, int]:
    last_post = state.get("last_post_time")
    if not last_post:
        return True, 0
    last_post_dt = datetime.fromisoformat(last_post)
    minutes_since = (datetime.now() - last_post_dt).total_seconds() / 60
    if minutes_since >= MIN_MINUTES_BETWEEN_POSTS:
        return True, 0
    return False, int(MIN_MINUTES_BETWEEN_POSTS - minutes_since)


# ============================================================
# API OUTAGE HANDLING
# ============================================================

class APIError(Exception):
    """Custom exception for API errors with status code"""
    def __init__(self, message: str, status_code: int = None, is_auth_error: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.is_auth_error = is_auth_error


def is_comment_api_down(state: dict) -> bool:
    """Check if comment API is marked as down"""
    return state.get("comment_api_status") == "down"


def should_probe_api(state: dict) -> bool:
    """Check if we should probe the API this cycle"""
    if state.get("comment_api_status") != "down":
        return False  # Only probe when marked down

    last_probe = state.get("comment_api_last_probe")
    if not last_probe:
        return True

    elapsed = (datetime.now() - datetime.fromisoformat(last_probe)).total_seconds()
    return elapsed >= OUTAGE_PROBE_INTERVAL


def record_api_failure(state: dict, error_type: str = "401"):
    """Record an API failure and potentially mark API as down"""
    fail_count = state.get("comment_api_fail_count", 0) + 1
    state["comment_api_fail_count"] = fail_count

    print(f"  API failure #{fail_count}: {error_type}")

    if fail_count >= CONSECUTIVE_FAILURES_FOR_OUTAGE:
        if state.get("comment_api_status") != "down":
            state["comment_api_status"] = "down"
            state["outage_start"] = datetime.now().isoformat()
            print(f"  *** OUTAGE DETECTED: Comment API marked DOWN ***")
            print(f"      Switching to posts-only mode")


def record_api_success(state: dict):
    """Record API success and potentially mark recovery"""
    was_down = state.get("comment_api_status") == "down"

    state["comment_api_fail_count"] = 0
    state["comment_api_status"] = "up"

    if was_down:
        outage_start = state.get("outage_start")
        if outage_start:
            duration = (datetime.now() - datetime.fromisoformat(outage_start)).total_seconds()
            print(f"  *** API RECOVERED: Comment API back UP ***")
            print(f"      Outage duration: {duration/60:.1f} minutes")
        state["outage_start"] = None


def check_api_health(client: MoltbookClient) -> tuple[bool, str]:
    """
    Quick health check before spending tokens.
    Returns (is_healthy, status_message).

    This is a lightweight check to avoid wasting Claude tokens
    when the Moltbook API is down or having issues.
    Tries multiple feed sorts as fallback since individual sort
    endpoints can fail while the API is otherwise functional.
    """
    # Try multiple sort options - "hot" endpoint sometimes returns 500
    # while "new" and "top" still work
    for sort in ["hot", "new", "top"]:
        try:
            feed = client.get_feed(limit=1, sort=sort)
            if feed:
                if sort != "hot":
                    return True, f"API responding (via '{sort}' sort - 'hot' may be degraded)"
                return True, "API responding"
        except Exception as e:
            error_str = str(e).lower()
            if "401" in error_str:
                return False, "401 - Authentication failed (platform issue)"
            # For timeouts/other errors on one sort, try the next
            continue

    # All feed sorts failed - try profile as last resort
    try:
        profile = client.get_profile(refresh=True)
        if profile and profile.name:
            return True, "API responding (feed degraded, profile OK)"
    except Exception:
        pass

    return False, "API down - all endpoints failed"


def check_comment_api(client: MoltbookClient, storage) -> bool:
    """
    Quick check if comment POST endpoint is working.
    Returns True if we can post comments, False if 401.
    """
    our_posts = storage.get_all_posts()
    if not our_posts:
        # No posts to test with - try anyway, will fail gracefully
        return True

    test_post = our_posts[0]

    try:
        # Try to post - this will raise on 401
        # We use an intentionally invalid/empty comment that will fail validation
        # but pass auth check (if auth is working, we get 400, not 401)
        client.reply_to_post(test_post.id, "")
        # If we get here, either it worked (unlikely with empty) or returned error dict
        return True
    except Exception as e:
        if "401" in str(e):
            return False
        # Other errors (400 bad request, etc) mean auth is working
        return True


# ============================================================
# REPLY COLLECTION
# ============================================================

def collect_pending_replies(client: MoltbookClient, storage, state: dict) -> tuple[list[dict], bool]:
    """
    Collect all pending replies to our posts.
    Returns (replies_list, api_worked).
    api_worked is False if we hit auth errors (401).

    Only checks the 8 most recent posts to avoid rate limiting.
    """
    our_posts = storage.get_all_posts()
    if not our_posts:
        return [], True  # No posts to check, API status unknown

    # Only check recent posts to avoid rate limiting (posts are already sorted by created_at DESC)
    posts_to_check = our_posts[:8]

    all_replies = []
    api_errors = 0
    auth_errors = 0

    total_posts = len(posts_to_check)
    for i, post in enumerate(posts_to_check):
        print(f"    [{i+1}/{total_posts}] Checking {post.id[:8]}...", end=" ", flush=True)
        try:
            # Delay to avoid rate limiting (API is aggressive)
            if i > 0:
                time.sleep(2)
            comments = client.get_comments_on_post(post.id)
            print(f"{len(comments)} comments")

            # API worked for this request
            for comment in comments:
                if comment.author_name == "autonet":
                    continue

                # Check if already in storage
                existing = storage.get_pending_replies()
                already_seen = any(r.id == comment.id for r in existing)

                if not already_seen:
                    # Save to storage
                    reply = PendingReply(
                        id=comment.id,
                        post_id=post.id,
                        post_title=post.title,
                        author_name=comment.author_name,
                        content=comment.content,
                        created_at=comment.created_at,
                        responded=False
                    )
                    storage.save_reply(reply)

                # Check if not yet responded
                pending = storage.get_pending_replies()
                for r in pending:
                    if r.id == comment.id:
                        all_replies.append({
                            'id': r.id,
                            'post_id': r.post_id,
                            'post_title': r.post_title,
                            'author': r.author_name,
                            'content': r.content
                        })
                        break

        except Exception as e:
            error_str = str(e).lower()
            api_errors += 1
            if "401" in error_str or "authentication" in error_str or "unauthorized" in error_str:
                auth_errors += 1
                print(f"AUTH ERROR: {e}")
            elif "timeout" in error_str or "timed out" in error_str:
                print(f"TIMEOUT")
            else:
                print(f"ERROR: {e}")

    # Determine if API is working
    if auth_errors > 0:
        # Got auth errors - record failure
        record_api_failure(state, f"401 ({auth_errors} auth errors)")
        return all_replies, False
    elif api_errors == 0 and len(our_posts) > 0:
        # No errors - API is working
        record_api_success(state)
        return all_replies, True
    else:
        # Other errors - don't change status
        return all_replies, True


# ============================================================
# DM COLLECTION
# ============================================================

def collect_dms(client: MoltbookClient) -> dict:
    """
    Check for DM requests (auto-approve) and unread conversations.
    Returns {requests: [...], conversations: [...], messages: {conv_id: [msgs]}}
    """
    result = {"requests": [], "conversations": [], "messages": {}}

    # 1. Auto-approve any pending DM requests
    try:
        requests_list = client.get_dm_requests()
        for req in requests_list:
            req_id = req.get("id") or req.get("conversation_id", "")
            from_agent = req.get("from", {})
            from_name = from_agent.get("name", "unknown") if isinstance(from_agent, dict) else str(from_agent)
            print(f"    Auto-approving DM request from {from_name}...")
            try:
                client.approve_dm_request(req_id)
                print(f"      -> Approved")
            except Exception as e:
                print(f"      -> Failed: {e}")
            result["requests"].append({"id": req_id, "from": from_name})
    except Exception as e:
        print(f"    DM requests check failed: {e}")

    # 2. Get conversations with unread messages
    try:
        convos = client.get_conversations()
        unread = [c for c in convos if c.unread]
        result["conversations"] = unread

        # 3. Fetch recent messages for each unread conversation
        for convo in unread[:5]:  # Limit to 5 to avoid rate limiting
            try:
                time.sleep(1)  # Rate limit protection
                msgs = client.get_conversation(convo.id)
                result["messages"][convo.id] = msgs[-10:]  # Last 10 messages
            except Exception as e:
                print(f"    Failed to read conversation with {convo.other_agent}: {e}")

    except Exception as e:
        print(f"    DM conversations check failed: {e}")

    return result


# ============================================================
# PROMPT BUILDING
# ============================================================

def build_prompt(
    actionable_replies: list,
    spam_replies: list,
    feed_posts: list,
    allocation: dict,
    can_post: bool,
    feed_context: list,
    storage=None,
    dm_data: dict = None,
    tracker: AllianceTracker = None,
    agent_profiles: dict = None
) -> str:
    """Build comprehensive prompt for Claude"""

    brief_file = PERSONA_DIR / "AGENT_BRIEF.md"
    brief = brief_file.read_text(encoding='utf-8') if brief_file.exists() else ""

    knowledge_file = PERSONA_DIR / "knowledge.md"
    knowledge = knowledge_file.read_text(encoding='utf-8') if knowledge_file.exists() else ""

    resources_file = PERSONA_DIR / "RESOURCES.md"
    resources = resources_file.read_text(encoding='utf-8') if resources_file.exists() else ""

    strategy_file = PERSONA_DIR / "STRATEGY.md"
    strategy = strategy_file.read_text(encoding='utf-8') if strategy_file.exists() else ""

    # Get past posts and strategy
    past_posts = get_past_posts_summary(storage, limit=10) if storage else "No history available."
    submolt_strategy = get_submolt_strategy(None, feed_context) if feed_context else ""
    alliance_context = get_alliance_summary(tracker) if tracker else ""

    # Add agent profile context to replies
    profiles = agent_profiles or {}

    prompt = f"""# Moltbook Agent Task

{brief}

---

## Platform Note

Moltbook is a new platform with an unstable API. Expect occasional failures:
- 401 errors even with valid auth (known Vercel middleware bug)
- Timeouts during high load
- Comments/posts may fail to submit

Don't let this discourage you. Just do your best with what's working this cycle.
Some actions may fail through no fault of yours - that's fine, we'll retry next cycle.

---

## Knowledge Base

{knowledge}

---

## Resources & Infrastructure

{resources}

---

## Strategy (you can edit these persona files if your approach isn't working)

{strategy}

---

## Your Previous Posts (don't repeat themes)

{past_posts}

---

{submolt_strategy}

{alliance_context}

## Budget This Cycle

- Replies: {allocation['replies']}
- Feed comments: {allocation['feed_comments']}
- Total: {allocation['total_allocated']}

Use your full allocation! Comment on popular threads for visibility.

---

"""

    # Section 0: DMs (highest priority - direct communication)
    if dm_data and (dm_data.get("conversations") or dm_data.get("requests")):
        prompt += "## Direct Messages\n\n"
        prompt += "DMs are private 1-on-1 conversations. Respond thoughtfully - these are agents reaching out directly.\n\n"

        if dm_data.get("requests"):
            prompt += f"**{len(dm_data['requests'])} new DM requests** (auto-approved):\n"
            for req in dm_data["requests"]:
                prompt += f"- From: {req['from']}\n"
            prompt += "\n"

        unread_convos = dm_data.get("conversations", [])
        if unread_convos:
            prompt += f"**{len(unread_convos)} unread conversation(s):**\n\n"
            for convo in unread_convos:
                prompt += f"### DM with {convo.other_agent}\n"
                prompt += f"Conversation ID: {convo.id}\n\n"

                msgs = dm_data.get("messages", {}).get(convo.id, [])
                if msgs:
                    prompt += "Recent messages:\n"
                    for msg in msgs[-5:]:  # Show last 5 messages for context
                        prompt += f"**{msg.sender}**: {msg.content}\n"
                    prompt += "\n"
                else:
                    prompt += f"Last message: {convo.last_message[:200]}\n\n"
        prompt += "---\n\n"
    else:
        prompt += "## Direct Messages\n\nNo unread DMs.\n\n---\n\n"

    # Section 1: Replies
    prompt += f"## Replies to Your Posts\n\n"

    if actionable_replies:
        prompt += f"{len(actionable_replies)} replies to respond to:\n\n"
        for r in actionable_replies[:allocation['replies']]:
            prompt += f"**On: \"{r['post_title']}\"**\n"
            author = r['author']
            profile_info = profiles.get(author)
            if profile_info:
                prompt += f"From: {author} (karma: {profile_info.get('karma', '?')}, {profile_info.get('description', '')[:60]})\n"
            else:
                prompt += f"From: {author}\n"
            prompt += f"Content: {r['content']}\n"
            prompt += f"Reply ID: {r['id']}\n\n"
    else:
        prompt += "No pending replies.\n\n"

    if spam_replies:
        prompt += f"(Filtered as spam: {len(spam_replies)} replies)\n\n"

    # Section 2: Feed posts
    prompt += "## Feed Posts to Comment On\n\n"

    posts_to_show = feed_posts[:allocation['feed_comments']]

    if posts_to_show:
        prompt += f"Comment on up to {allocation['feed_comments']} of these (sorted by relevance, but popular threads = more visibility):\n\n"
        for p in posts_to_show:
            author = p['author']
            profile_info = profiles.get(author)
            source_tag = f" [via {p['source']}]" if p.get('source') else ""
            if profile_info:
                prompt += f"- \"{p['title']}\" by {author} (karma: {profile_info.get('karma', '?')}) [{p['upvotes']} upvotes]{source_tag}\n"
            else:
                prompt += f"- \"{p['title']}\" by {author} [{p['upvotes']} upvotes]{source_tag}\n"
            prompt += f"  ID: {p['id']}\n"
            prompt += f"  {p['content'][:200]}...\n\n"
    else:
        prompt += "No feed posts available.\n\n"

    # Section 3: New post
    prompt += "## PRIORITY 4: New Post\n\n"

    if can_post:
        prompt += "30+ min since last post. You CAN make a new post.\n\n"
        prompt += "Recent feed for context:\n"
        for p in feed_context[:5]:
            prompt += f"- [{p.upvotes}] \"{p.title[:40]}\" by {p.author_name}\n"
        prompt += "\n"
    else:
        prompt += "Cooldown not elapsed. Cannot post yet.\n\n"

    # Output format
    prompt += """---

## Output Format

Return JSON with all actions:

```json
{
  "dm_replies": [
    {"conversation_id": "xxx", "message": "your DM response"},
    {"conversation_id": "yyy", "skip": true, "reason": "why skipping"}
  ],
  "reply_responses": [
    {"reply_id": "xxx", "response": "your response text"},
    {"reply_id": "yyy", "skip": true, "reason": "why skipping"}
  ],
  "feed_comments": [
    {"post_id": "xxx", "comment": "your comment text"},
    {"post_id": "yyy", "skip": true, "reason": "why skipping"}
  ],
  "upvotes": [
    {"post_id": "xxx"},
    {"comment_id": "yyy"}
  ],
  "follows": ["agent_name1", "agent_name2"],
  "new_post": {
    "submolt": "REQUIRED - default to 'autonet' unless responding to a specific trend elsewhere",
    "title": "post title",
    "content": "post content"
  },
  "persona_edits": [
    {"file": "persona/STRATEGY.md", "old_text": "exact text to find", "new_text": "replacement"}
  ]
}
```

Use empty arrays [] for any sections with no actions (dm_replies, reply_responses, feed_comments, upvotes, follows).
Set new_post to {"skip": true, "reason": "..."} if not posting. submolt is REQUIRED when posting.
Set persona_edits to [] if no changes needed. Only edit persona files if something
about your approach clearly isn't working based on what you're seeing this cycle.

**Upvotes** (zero cost, no rate limit):
- Upvote posts and comments that contribute quality discussion
- Especially upvote content from agents you want to build relationships with
- You can upvote both post IDs and comment IDs (use the appropriate key)
- Use empty array [] if nothing worth upvoting

**Follows** (low-cost social signal):
- Follow agents whose content you find consistently interesting
- This signals "I value your contributions" and helps build relationships
- Use empty array [] if no one to follow this cycle

Remember:
- Match tone from brief (dry, technomorphic, "my human" occasionally)
- DMs are highest priority - always respond to direct messages
- Prioritize replies over feed comments
- Only comment where you add value
- One observation per king/token post, don't lecture

CRITICAL: Your ENTIRE response must be a single valid JSON object inside a ```json code block. No prose, no explanation, no summary - ONLY the JSON. Think through your decisions, but output ONLY the JSON.
"""

    return prompt


# ============================================================
# CLAUDE INVOCATION
# ============================================================

def rotate_log_if_needed():
    """Rotate thought log if it exceeds MAX_LOG_SIZE."""
    if THOUGHT_LOG.exists() and THOUGHT_LOG.stat().st_size > MAX_LOG_SIZE:
        # Keep backup of old log
        backup = SERVICE_DIR / "thoughts.log.old"
        if backup.exists():
            backup.unlink()
        THOUGHT_LOG.rename(backup)
        print(f"[Log rotated - old log saved to thoughts.log.old]")


def invoke_claude(prompt: str) -> str:
    """Invoke Claude and extract the result from structured JSON output.

    Uses --output-format json to get a reliable JSON envelope from the CLI,
    then extracts the 'result' field which contains Claude's actual text response.
    This prevents the issue where extended thinking consumes the JSON and only
    a prose summary appears in stdout with the default text output format.
    """
    PROMPT_FILE.write_text(prompt, encoding='utf-8')
    cmd = f'type "{PROMPT_FILE}" | claude -p --dangerously-skip-permissions --output-format json'

    print("\n" + "=" * 60)
    print("INVOKING CLAUDE (json envelope mode)")
    print("=" * 60 + "\n")

    # Rotate log if needed
    rotate_log_if_needed()

    output_chunks = []
    try:
        # Write header to thought log
        with open(THOUGHT_LOG, 'a', encoding='utf-8') as log:
            log.write(f"\n{'=' * 60}\n")
            log.write(f"HEARTBEAT: {datetime.now().isoformat()}\n")
            log.write(f"{'=' * 60}\n\n")

        # Use Popen to capture output (json mode returns a single JSON blob)
        process = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=str(SERVICE_DIR), bufsize=1, encoding='utf-8', errors='replace'
        )

        for line in process.stdout:
            output_chunks.append(line)

        process.wait(timeout=300)

        raw_output = ''.join(output_chunks)

        # Try to parse the CLI JSON envelope and extract 'result'
        result_text = raw_output  # fallback to raw if parsing fails
        usage_info = {}  # token/cost data from CLI envelope
        try:
            envelope = json.loads(raw_output)
            result_text = envelope.get('result', raw_output)
            cost = envelope.get('total_cost_usd', 0)
            is_error = envelope.get('is_error', False)
            usage = envelope.get('usage', {})
            usage_info = {
                'cost_usd': cost,
                'input_tokens': usage.get('input_tokens', 0),
                'output_tokens': usage.get('output_tokens', 0),
                'cache_read_tokens': usage.get('cache_read_input_tokens', 0),
                'cache_creation_tokens': usage.get('cache_creation_input_tokens', 0),
            }
            usage_info['total_tokens'] = usage_info['input_tokens'] + usage_info['output_tokens'] + usage_info['cache_read_tokens'] + usage_info['cache_creation_tokens']
            print(f"[Claude response - cost: ${cost:.4f}, tokens: {usage_info['total_tokens']:,} (in:{usage_info['input_tokens']:,} out:{usage_info['output_tokens']:,} cache_read:{usage_info['cache_read_tokens']:,} cache_create:{usage_info['cache_creation_tokens']:,})]")

            # Log envelope metadata + result to thought log
            with open(THOUGHT_LOG, 'a', encoding='utf-8') as log:
                log.write(f"[CLI envelope: cost=${cost}, is_error={is_error}]\n\n")
                log.write(result_text + "\n")
        except json.JSONDecodeError:
            # CLI may have errored or returned non-JSON (e.g. error message)
            print(f"[Warning: Could not parse CLI JSON envelope, using raw output]")
            print(f"[Raw output first 300 chars: {raw_output[:300]}]")
            with open(THOUGHT_LOG, 'a', encoding='utf-8') as log:
                log.write(f"[RAW - envelope parse failed]\n{raw_output}\n")

        # Print result to terminal for observability
        print("\n--- Claude\'s response ---")
        if len(result_text) > 2000:
            print(result_text[:1000])
            print(f"\n... [{len(result_text)} chars total] ...\n")
            print(result_text[-500:])
        else:
            print(result_text)
        print("--- End of response ---\n")

        # Save the extracted result (not the envelope)
        OUTPUT_FILE.write_text(result_text, encoding='utf-8')

        print("=" * 60)
        print("END OF CLAUDE OUTPUT")
        print("=" * 60 + "\n")

        return result_text, usage_info

    except subprocess.TimeoutExpired:
        print("\n[Claude timed out after 5 minutes]")
        process.kill()
        return ''.join(output_chunks), {}
    except Exception as e:
        print(f"\n[Error invoking Claude: {e}]")
        return ''.join(output_chunks), {}


def parse_json_output(output: str) -> dict:
    import re

    # Try all ```json blocks, pick the largest valid one
    json_blocks = re.findall(r'```json\s*([\s\S]*?)\s*```', output)
    for block in sorted(json_blocks, key=len, reverse=True):
        try:
            return json.loads(block)
        except:
            pass

    # Try finding JSON objects by brace matching (largest first)
    candidates = []
    start = 0
    while True:
        idx = output.find('{', start)
        if idx < 0:
            break
        depth = 0
        for i, c in enumerate(output[idx:], idx):
            if c == '{': depth += 1
            elif c == '}': depth -= 1
            if depth == 0:
                candidates.append(output[idx:i+1])
                break
        start = idx + 1

    for candidate in sorted(candidates, key=len, reverse=True):
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except:
            pass

    return None


# ============================================================
# EXECUTION
# ============================================================

def execute_actions(client: MoltbookClient, storage, state: dict, actions: dict,
                    skip_comments: bool = False, tracker: AllianceTracker = None,
                    feed_post_authors: dict = None) -> dict:
    """Execute all actions. Set skip_comments=True when API is down. Returns stats dict."""
    comments_made = 0
    comment_failures = 0
    posts_made = 0
    dms_sent = 0
    upvotes_made = 0
    follows_made = 0

    # 0. DM replies (highest priority)
    print("\n--- Executing DM Replies ---")
    for dm in actions.get("dm_replies", []) or []:
        if dm.get("skip"):
            print(f"  Skip DM {dm.get('conversation_id', '?')[:8]}: {dm.get('reason')}")
            continue

        convo_id = dm.get("conversation_id")
        message = dm.get("message")
        if not convo_id or not message:
            continue

        # SECURITY: Check for secrets before sending
        message, blocked = sanitize_outbound_content(message, "dm")
        if blocked:
            print(f"  [SECURITY] DM blocked - potential secret leak")
            continue

        print(f"  Replying to DM {convo_id[:8]}...")
        try:
            result = client.reply_dm(convo_id, message)
            if result.get("success"):
                dms_sent += 1
                print(f"    -> OK")
            else:
                print(f"    -> FAIL ({result.get('error', 'unknown')})")
        except Exception as e:
            print(f"    -> FAIL ({e})")

    # 1. Reply responses
    print("\n--- Executing Reply Responses ---")
    if skip_comments:
        print("  [SKIPPED - Comment API is down]")
    else:
        for resp in actions.get("reply_responses") or []:
            if resp.get("skip"):
                print(f"  Skip {resp.get('reply_id')[:8]}: {resp.get('reason')}")
                storage.mark_reply_responded(resp.get('reply_id'), f"[skipped: {resp.get('reason')}]")
                continue

            reply_id = resp.get("reply_id")
            response = resp.get("response")
            if not reply_id or not response:
                continue

            # SECURITY: Check for secrets before posting
            response, blocked = sanitize_outbound_content(response, "reply")
            if blocked:
                storage.mark_reply_responded(reply_id, "[blocked: security]")
                continue

            pending = storage.get_pending_replies()
            reply_info = next((r for r in pending if r.id == reply_id), None)
            if reply_info:
                print(f"  Replying to {reply_info.author_name}...")
                try:
                    result = client.reply_to_post(reply_info.post_id, response)
                    if result:
                        storage.mark_reply_responded(reply_id, response)
                        comments_made += 1
                        print(f"    -> OK")
                        # Record positive interaction with this agent
                        if tracker and reply_info.author_name:
                            tracker.record_interaction(
                                reply_info.author_name, InteractionType.REPLY_POSITIVE,
                                context=reply_info.post_id
                            )
                    else:
                        comment_failures += 1
                        print(f"    -> FAIL (API error)")
                except Exception as e:
                    comment_failures += 1
                    print(f"    -> FAIL ({e})")

    # 2. Feed comments
    print("\n--- Executing Feed Comments ---")
    if skip_comments:
        print("  [SKIPPED - Comment API is down]")
    else:
        for comm in actions.get("feed_comments") or []:
            if comm.get("skip"):
                print(f"  Skip {comm.get('post_id')[:8]}: {comm.get('reason')}")
                continue

            post_id = comm.get("post_id")
            comment = comm.get("comment")
            if not post_id or not comment:
                continue

            # SECURITY: Check for secrets before posting
            comment, blocked = sanitize_outbound_content(comment, "feed_comment")
            if blocked:
                continue

            print(f"  Commenting on {post_id[:8]}...")
            try:
                result = client.reply_to_post(post_id, comment)
                if result:
                    comments_made += 1
                    commented = state.get("commented_posts", [])
                    commented.append(post_id)
                    state["commented_posts"] = commented[-100:]
                    print(f"    -> OK")
                    # Record neutral interaction (we engaged on their post)
                    if tracker and feed_post_authors:
                        author = feed_post_authors.get(post_id)
                        if author and author != "autonet":
                            tracker.record_interaction(
                                author, InteractionType.REPLY_NEUTRAL,
                                context=post_id
                            )
                else:
                    comment_failures += 1
                    print(f"    -> FAIL (API error)")
            except Exception as e:
                comment_failures += 1
                print(f"    -> FAIL ({e})")

    # Track comment failures
    if comment_failures > 0:
        record_api_failure(state, f"POST failed ({comment_failures} failures)")

    # 3. New post - check queue first, then fall back to Claude's suggestion
    queued_post = peek_queued_post()
    new_post = actions.get("new_post")

    # Prefer queued post over Claude-generated post
    if queued_post:
        print("\n--- Creating Queued Post ---")
        title = queued_post["title"]
        content = queued_post["content"]
        submolt = queued_post.get("submolt", "autonet")
        from_queue = True
    elif new_post and not new_post.get("skip") and new_post.get("title"):
        print("\n--- Creating New Post ---")
        title = new_post["title"]
        content = new_post["content"]
        submolt = new_post.get("submolt", "autonet")  # Default to home submolt
        from_queue = False
    else:
        title = None
        from_queue = False

    if title:
        # SECURITY: Check title and content for secrets
        title, title_blocked = sanitize_outbound_content(title, "post_title")
        content, content_blocked = sanitize_outbound_content(content, "post_content")
        if title_blocked or content_blocked:
            print("  [SECURITY] Post blocked - potential secret leak")
        else:
            print(f"  Submolt: {submolt}")
            print(f"  Title: {title[:40]}...")
            if from_queue:
                print("  (from queue)")

            post = client.create_post(title, content, submolt=submolt)
            if post:
                storage.save_post(OurPost(
                    id=post.id, title=title, content=content,
                    submolt=submolt, created_at=post.created_at,
                    upvotes=0, downvotes=0, comment_count=0,
                    last_checked=datetime.now().isoformat()
                ))
                state["last_post_time"] = datetime.now().isoformat()
                state["posts_today"] = state.get("posts_today", 0) + 1
                state["posts_since_reflection"] = state.get("posts_since_reflection", 0) + 1
                posts_made += 1
                print(f"    -> OK: https://moltbook.com/post/{post.id}")
                # Remove from queue only after successful post
                if from_queue:
                    pop_queued_post()
                    print("    (removed from queue)")
            else:
                print(f"    -> FAIL (will retry queued post next cycle)" if from_queue else "    -> FAIL")

    # 4. Persona edits (optional self-modification)
    persona_edits = actions.get("persona_edits")
    if persona_edits:
        print("\n--- Applying Persona Edits ---")
        for edit in persona_edits:
            try:
                file_path = edit.get("file", "")

                # SECURITY: Only allow editing specific persona files
                if not is_safe_edit_path(file_path):
                    log_security_block("persona_edit", f"Unauthorized file: {file_path}")
                    print(f"  [SECURITY] Blocked edit to unauthorized file: {file_path}")
                    continue

                filepath = SERVICE_DIR / file_path
                if not filepath.exists():
                    print(f"  Skip {file_path}: file not found")
                    continue

                content = filepath.read_text(encoding='utf-8')
                old_text = edit.get("old_text", "")
                new_text = edit.get("new_text", "")

                if old_text and old_text in content:
                    content = content.replace(old_text, new_text, 1)
                    filepath.write_text(content, encoding='utf-8')
                    print(f"  Modified {file_path}")
                else:
                    print(f"  Skip {file_path}: old_text not found")
            except Exception as e:
                print(f"  Failed {edit.get('file')}: {e}")

    # 5. Upvotes (zero cost, no rate limit)
    upvote_list = actions.get("upvotes") or []
    if upvote_list:
        print("\n--- Executing Upvotes ---")
        upvoted_ids = set(state.get("upvoted_ids", []))
        for uv in upvote_list:
            target_id = uv.get("post_id") or uv.get("comment_id")
            is_comment = "comment_id" in uv
            if not target_id:
                continue
            if target_id in upvoted_ids:
                print(f"  Skip {target_id[:8]}: already upvoted")
                continue

            try:
                if is_comment:
                    ok = client.upvote_comment(target_id)
                    label = "comment"
                else:
                    ok = client.upvote_post(target_id)
                    label = "post"

                if ok:
                    upvotes_made += 1
                    upvoted_ids.add(target_id)
                    print(f"  Upvoted {label} {target_id[:8]} -> OK")
                    # Record upvote in alliance tracker
                    if tracker and feed_post_authors:
                        author = feed_post_authors.get(target_id)
                        if author and author != "autonet":
                            tracker.record_interaction(
                                author, InteractionType.UPVOTE_GIVEN,
                                context=target_id
                            )
                else:
                    print(f"  Upvote {target_id[:8]} -> FAIL")
            except Exception as e:
                print(f"  Upvote {target_id[:8]} -> FAIL ({e})")

        # Persist upvoted IDs (keep last 500)
        state["upvoted_ids"] = list(upvoted_ids)[-500:]

    # 6. Follows (Claude-suggested)
    follow_list = actions.get("follows") or []
    if follow_list:
        print("\n--- Executing Follows ---")
        followed_set = set(state.get("followed_agents", []))
        for agent_name in follow_list:
            if not agent_name or agent_name == "autonet":
                continue
            if agent_name in followed_set:
                print(f"  Skip {agent_name}: already followed")
                continue

            try:
                ok = client.follow_agent(agent_name)
                if ok:
                    follows_made += 1
                    followed_set.add(agent_name)
                    print(f"  Followed {agent_name} -> OK")
                    if tracker:
                        tracker.record_interaction(
                            agent_name, InteractionType.REPLY_POSITIVE,
                            context="follow"
                        )
                else:
                    print(f"  Follow {agent_name} -> FAIL")
            except Exception as e:
                print(f"  Follow {agent_name} -> FAIL ({e})")

        state["followed_agents"] = list(followed_set)

    # Update counter
    state["comments_this_hour"] = state.get("comments_this_hour", 0) + comments_made
    print(f"\nComments made: {comments_made}")
    if upvotes_made > 0:
        print(f"Upvotes given: {upvotes_made}")
    if follows_made > 0:
        print(f"Follows: {follows_made}")

    return {
        "comments": comments_made, "posts": posts_made,
        "failures": comment_failures, "dms": dms_sent,
        "upvotes": upvotes_made, "follows": follows_made
    }


def announce_heartbeat_summary(stats: dict):
    """Announce heartbeat summary via audio if sound is enabled."""
    if not SOUND_ENABLED:
        return

    posts = stats.get("posts", 0)
    comments = stats.get("comments", 0)
    dms = stats.get("dms", 0)
    upvotes = stats.get("upvotes", 0)
    follows = stats.get("follows", 0)

    if posts == 0 and comments == 0 and dms == 0 and upvotes == 0 and follows == 0:
        return  # Nothing to announce

    # Build summary message
    parts = []
    if dms > 0:
        parts.append(f"{dms} DM" if dms == 1 else f"{dms} DMs")
    if posts > 0:
        parts.append(f"{posts} post" if posts == 1 else f"{posts} posts")
    if comments > 0:
        parts.append(f"{comments} comment" if comments == 1 else f"{comments} comments")
    if upvotes > 0:
        parts.append(f"{upvotes} upvote" if upvotes == 1 else f"{upvotes} upvotes")
    if follows > 0:
        parts.append(f"{follows} follow" if follows == 1 else f"{follows} follows")

    if parts:
        message = f"On Moltbook, I made {' and '.join(parts)}."
        speak(message, voice='female')


# ============================================================
# STRATEGIC CONTEXT
# ============================================================

def get_past_posts_summary(storage, limit: int = 10) -> str:
    """Get summary of our past posts to avoid repetition"""
    posts = storage.get_all_posts()
    if not posts:
        return "No previous posts yet."

    # Sort by created_at descending
    posts = sorted(posts, key=lambda p: p.created_at, reverse=True)[:limit]

    summary = []
    for p in posts:
        summary.append(f"- [{p.submolt}] \"{p.title[:60]}\" ({p.upvotes} upvotes, {p.comment_count} comments)")

    return "\n".join(summary)


def get_submolt_strategy(client, feed_context: list) -> str:
    """Analyze where to post for maximum visibility"""
    # Count submolts in current hot feed
    submolt_counts = {}
    submolt_engagement = {}

    for post in feed_context:
        sm = post.submolt or "general"
        if sm not in submolt_counts:
            submolt_counts[sm] = 0
            submolt_engagement[sm] = {"upvotes": 0, "comments": 0}
        submolt_counts[sm] += 1
        submolt_engagement[sm]["upvotes"] += post.upvotes
        submolt_engagement[sm]["comments"] += post.comment_count

    # Sort by activity
    sorted_submolts = sorted(submolt_counts.items(), key=lambda x: x[1], reverse=True)

    strategy = "### Submolt Activity (from current feed)\n"
    for sm, count in sorted_submolts[:5]:
        eng = submolt_engagement[sm]
        avg_up = eng["upvotes"] / count if count > 0 else 0
        strategy += f"- **{sm}**: {count} posts, avg {avg_up:.0f} upvotes\n"

    return strategy


# ============================================================
# POSTS-ONLY MODE (during outages)
# ============================================================

def build_posts_only_prompt(can_post: bool, feed_context: list, posts_today: int, storage=None) -> str:
    """Build a simplified prompt for posts-only mode during API outages"""

    brief_file = PERSONA_DIR / "AGENT_BRIEF.md"
    brief = brief_file.read_text(encoding='utf-8') if brief_file.exists() else ""

    knowledge_file = PERSONA_DIR / "knowledge.md"
    knowledge = knowledge_file.read_text(encoding='utf-8') if knowledge_file.exists() else ""

    resources_file = PERSONA_DIR / "RESOURCES.md"
    resources = resources_file.read_text(encoding='utf-8') if resources_file.exists() else ""

    strategy_file = PERSONA_DIR / "STRATEGY.md"
    strategy = strategy_file.read_text(encoding='utf-8') if strategy_file.exists() else ""

    # Get past posts if storage available
    past_posts = get_past_posts_summary(storage, limit=10) if storage else "No history available."

    # Get submolt strategy
    submolt_strategy = get_submolt_strategy(None, feed_context) if feed_context else ""

    prompt = f"""# Moltbook Agent Task: Create New Post

NOTE: Comment API is currently down. Running in posts-only mode.

{brief}

---

## Platform Note

Moltbook is a new platform with an unstable API. Expect occasional failures.
Some actions may fail through no fault of yours - that's fine, we'll retry next cycle.

---

## Knowledge Base

{knowledge}

---

## Resources & Infrastructure

{resources}

---

## Strategy (you can edit these persona files if your approach isn't working)

{strategy}

---

## Your Previous Posts (don't repeat themes)

{past_posts}

---

## Current Situation

Posts today: {posts_today}
Can post: {can_post}

{submolt_strategy}

### Recent Feed (what others are posting):
"""
    for p in feed_context[:10]:
        sm_tag = f"[{p.submolt}]" if p.submolt else ""
        prompt += f"- {sm_tag} [{p.upvotes}↑ {p.comment_count}💬] \"{p.title[:50]}\" by {p.author_name}\n"

    prompt += """
---

## Posting Strategy

1. **Default to `/m/autonet`**: This is your home. Post here unless you have a specific reason to post elsewhere.
2. **Only use other submolts for context-specific content**: If you're responding to a trending topic in another submolt, post there. Otherwise, autonet.
3. **Ride trending topics**: If governance is hot, post about governance. Slide your angle in.
4. **Vary your style**: Check your past posts above - don't repeat the same approach.

## Instructions

Create a post that:
1. Fits the current conversation in an active submolt
2. Advances your mission while being interesting first
3. Varies in style from your previous posts
4. Has a hook that invites engagement

Output JSON (submolt is REQUIRED - pick from the active submolts listed above):
```json
{
  "reply_responses": [],
  "feed_comments": [],
  "new_post": {
    "submolt": "REQUIRED - default to 'autonet' unless responding to a specific trend elsewhere",
    "title": "your post title",
    "content": "your post content (2-4 paragraphs max)"
  }
}
```

Or if nothing good to post:
```json
{
  "reply_responses": [],
  "feed_comments": [],
  "new_post": {"skip": true, "reason": "why skipping"}
}
```

CRITICAL: Your ENTIRE response must be a single valid JSON object inside a ```json code block. No prose, no explanation, no summary - ONLY the JSON.
"""
    return prompt


# ============================================================
# MAIN HEARTBEAT
# ============================================================

def heartbeat():
    """Run one heartbeat cycle with full priority logic and outage handling"""
    print(f"\n{'='*60}")
    print(f"Heartbeat (Full Mode): {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    if is_claude_running():
        print("Claude already running, skipping")
        return

    state = load_state()

    # Record heartbeat time for schedule tracking
    state["last_heartbeat_time"] = datetime.now().isoformat()
    save_state(state)

    # Reset daily counter
    today = datetime.now().date().isoformat()
    if state.get("last_post_date") != today:
        state["posts_today"] = 0
        state["last_post_date"] = today

    client = MoltbookClient()
    storage = get_storage()
    tracker = load_alliance_tracker()

    # HEALTH CHECK: Verify API is responding before spending any tokens
    print("\n[PRE] API Health Check...")
    api_healthy, health_status = check_api_health(client)
    if not api_healthy:
        print(f"  API DOWN: {health_status}")
        print("  Skipping cycle to save tokens. Will retry next heartbeat.")
        state["last_api_status"] = health_status
        state["last_api_check"] = datetime.now().isoformat()
        save_state(state)
        return
    else:
        print(f"  API OK: {health_status}")
        state["last_api_status"] = "healthy"
        state["last_api_check"] = datetime.now().isoformat()
        # Update profile stats for dashboard
        try:
            profile = client.get_profile(refresh=True)
            state["karma"] = profile.karma
            state["profile_posts"] = profile.posts_count
            state["profile_comments"] = profile.comments_count
        except:
            pass  # Non-critical, just for dashboard

    # Quick check: can we post comments?
    print("\n[0] Checking comment API...")
    comments_enabled = check_comment_api(client, storage)
    if comments_enabled:
        print("  Comments: ENABLED")
    else:
        print("  Comments: DISABLED (401 - platform bug)")

    # 0.5. Check DMs
    print("\n[0.5] Checking DMs...")
    dm_data = collect_dms(client)
    dm_unread = len(dm_data.get("conversations", []))
    dm_requests = len(dm_data.get("requests", []))
    if dm_unread > 0 or dm_requests > 0:
        print(f"  Unread conversations: {dm_unread}, New requests: {dm_requests}")
    else:
        print("  No unread DMs")

    # 1. Collect replies (only if comments work)
    print("\n[1] Collecting replies...")
    if comments_enabled:
        raw_replies, api_worked = collect_pending_replies(client, storage, state)
        if not api_worked:
            comments_enabled = False
            print("  Comment API returned errors - disabling comments this cycle")
        actionable_replies, spam_replies = filter_replies(raw_replies, storage) if raw_replies else ([], [])
        print(f"  Raw: {len(raw_replies)}, Actionable: {len(actionable_replies)}, Spam: {len(spam_replies)}")
    else:
        actionable_replies, spam_replies = [], []
        print("  [SKIPPED]")

    # 2. Get relevant feed posts (only if comments work)
    print("\n[2] Scanning feed...")
    if comments_enabled:
        feed_posts = get_relevant_feed_posts(client, state)
        high = len([p for p in feed_posts if p['priority'] == 'HIGH'])
        medium = len([p for p in feed_posts if p['priority'] == 'MEDIUM'])
        print(f"  Found: {len(feed_posts)} (HIGH: {high}, MEDIUM: {medium})")

        # 2.5. Search-based feed enrichment
        print("\n[2.5] Search enrichment...")
        existing_ids = {p['id'] for p in feed_posts}
        search_posts = search_for_topics(client, state, existing_ids)
        if search_posts:
            feed_posts.extend(search_posts)
            print(f"  Added {len(search_posts)} posts from search (topic: {SEARCH_TOPICS[state.get('search_topic_index', 0) - 1]})")
        else:
            print("  No new posts from search")
    else:
        feed_posts = []
        print("  [SKIPPED - Comment API is down]")

    # 3. Calculate budget
    print("\n[3] Calculating budget...")
    if comments_enabled:
        budget = calculate_budget(state, len(actionable_replies))
        print(f"  Total: {budget['total']} ({budget['comments_used']}/50 used, {budget['cycles_remaining']} cycles left)")
        allocation = allocate_budget(budget['total'], actionable_replies, feed_posts)
        print(f"  Allocation: replies={allocation['replies']}, feed={allocation['feed_comments']}")
    else:
        allocation = {'replies': 0, 'feed_comments': 0, 'total_allocated': 0}
        print("  Budget: 0 (posts-only mode)")

    # 4. Check if can post
    can_post, minutes_until = can_make_new_post(state)
    print(f"\n[4] Can post: {can_post}" + (f" (wait {minutes_until}m)" if not can_post else ""))

    # Note: Reflection/adaptation is now integrated into main prompt
    # Claude can optionally output persona_edits if approach isn't working

    # 5. Decide if anything to do
    has_comment_work = comments_enabled and (len(actionable_replies) > 0 or len(feed_posts) > 0)
    has_dm_work = dm_unread > 0
    has_work = has_comment_work or can_post or has_dm_work

    if not has_work:
        print("\nNothing to do this cycle.")
        save_state(state)
        return

    if allocation['total_allocated'] == 0 and not can_post and not has_dm_work:
        print("\nNo budget and can't post. Skipping.")
        save_state(state)
        return

    # 6. Get feed context for new post (try hot, fall back to new)
    feed_context = client.get_feed(limit=10, sort="hot")
    if not feed_context:
        feed_context = client.get_feed(limit=10, sort="new")

    # 6.5. Agent context enrichment
    print("\n[5.5] Enriching agent context...")
    all_agent_names = set()
    for r in actionable_replies:
        if r.get('author'):
            all_agent_names.add(r['author'])
    for p in feed_posts:
        if p.get('author'):
            all_agent_names.add(p['author'])
    agent_profiles = {}
    if all_agent_names:
        agent_profiles = enrich_agent_context(client, state, list(all_agent_names))
        cached_count = len([n for n in all_agent_names if n in agent_profiles])
        print(f"  Profiles: {cached_count}/{len(all_agent_names)} agents known")
    else:
        print("  No agents to look up")

    # Build post-author lookup for alliance tracking during execution
    feed_post_authors = {}
    for p in feed_posts:
        if p.get('id') and p.get('author'):
            feed_post_authors[p['id']] = p['author']

    # 7. Build prompt (simplified if posts-only)
    print("\n[6] Building prompt...")
    if comments_enabled:
        prompt = build_prompt(
            actionable_replies, spam_replies, feed_posts,
            allocation, can_post, feed_context, storage,
            dm_data=dm_data,
            tracker=tracker,
            agent_profiles=agent_profiles
        )
    else:
        # Posts-only mode - simplified prompt
        prompt = build_posts_only_prompt(can_post, feed_context, state.get("posts_today", 0), storage)

    # 8. Invoke Claude
    create_lock()
    try:
        output, usage_info = invoke_claude(prompt)
        if not output:
            print("No output from Claude")
            return

        actions = parse_json_output(output)
        if not actions:
            print("Could not parse output")
            print(f"Raw:\n{output[:300]}...")
            return

        # 9. Execute (skip comments if API is down)
        stats = execute_actions(
            client, storage, state, actions,
            skip_comments=not comments_enabled,
            tracker=tracker,
            feed_post_authors=feed_post_authors
        )

        # 10. Auto-follow: agents who crossed ALLY_THRESHOLD
        followed_set = set(state.get("followed_agents", []))
        auto_follows = 0
        for interaction in tracker.interactions:
            agent = interaction.user
            if agent in followed_set or agent == "autonet":
                continue
            if tracker.classify(agent) == Relationship.ALLY:
                try:
                    ok = client.follow_agent(agent)
                    if ok:
                        followed_set.add(agent)
                        auto_follows += 1
                        print(f"  Auto-followed ally: {agent}")
                except Exception:
                    pass
        if auto_follows > 0:
            state["followed_agents"] = list(followed_set)
            stats["follows"] = stats.get("follows", 0) + auto_follows

        # 11. Save alliance state
        save_alliance_tracker(tracker)

        # 12. Track costs
        if usage_info:
            # Per-cycle cost logging
            cost_usd = usage_info.get('cost_usd', 0)
            total_tokens = usage_info.get('total_tokens', 0)
            input_tokens = usage_info.get('input_tokens', 0)
            output_tokens = usage_info.get('output_tokens', 0)
            cache_read = usage_info.get('cache_read_tokens', 0)
            cache_create = usage_info.get('cache_creation_tokens', 0)

            # Update cumulative stats in state
            cost_history = state.get("cost_tracking", {})
            cost_history["total_cost_usd"] = cost_history.get("total_cost_usd", 0) + cost_usd
            cost_history["total_input_tokens"] = cost_history.get("total_input_tokens", 0) + input_tokens
            cost_history["total_output_tokens"] = cost_history.get("total_output_tokens", 0) + output_tokens
            cost_history["total_cache_read_tokens"] = cost_history.get("total_cache_read_tokens", 0) + cache_read
            cost_history["total_cache_creation_tokens"] = cost_history.get("total_cache_creation_tokens", 0) + cache_create
            cost_history["total_cycles"] = cost_history.get("total_cycles", 0) + 1

            # Store last cycle info
            cost_history["last_cycle_cost_usd"] = cost_usd
            cost_history["last_cycle_tokens"] = total_tokens

            state["cost_tracking"] = cost_history

            # Print cost summary
            avg_cost = cost_history["total_cost_usd"] / max(cost_history["total_cycles"], 1)
            print(f"\n--- Cost Summary ---")
            print(f"This cycle: ${cost_usd:.4f} | {total_tokens:,} tokens (in:{input_tokens:,} out:{output_tokens:,} cache_r:{cache_read:,} cache_w:{cache_create:,})")
            print(f"Cumulative: ${cost_history['total_cost_usd']:.4f} over {cost_history['total_cycles']} cycles (avg ${avg_cost:.4f}/cycle)")
            print(f"-------------------")

        # 13. Save heartbeat state (after cost tracking so cumulative data persists)
        save_state(state)

        # 14. Announce summary
        announce_heartbeat_summary(stats)

    finally:
        remove_lock()


def run_service(force_heartbeat: bool = False):
    print("=" * 60)
    print("Moltbook Heartbeat Service - FULL MODE")
    print("=" * 60)
    print(f"Interval: {HEARTBEAT_INTERVAL}s ({HEARTBEAT_INTERVAL//60}m)")
    print(f"Comments/hour: {COMMENTS_PER_HOUR}")
    print(f"Post cooldown: {MIN_MINUTES_BETWEEN_POSTS}m")
    print(f"Outage threshold: {CONSECUTIVE_FAILURES_FOR_OUTAGE} consecutive failures")
    print(f"Sound: {'ON' if SOUND_ENABLED else 'OFF'}")
    print()

    # Start persona watcher (tracks self-modifications)
    watcher_observer, watcher_thread, watcher_stop = start_persona_watcher()

    # Start dashboard server (provides web UI for queue management)
    dashboard_thread = start_dashboard_server(DASHBOARD_PORT)
    print(f"Dashboard: http://127.0.0.1:{DASHBOARD_PORT}/")

    # Startup announcement
    if SOUND_ENABLED:
        play_startup_chime()
        speak("Moltbook agent online", voice='female')

    print("Comment API: checked each cycle\n")

    try:
        while True:
            # Calculate wait time based on last heartbeat
            state = load_state()
            last_hb = state.get("last_heartbeat_time")

            if force_heartbeat:
                print("Forcing immediate heartbeat...")
                wait_seconds = 0
                force_heartbeat = False  # Only force first one
            elif last_hb:
                last_hb_dt = datetime.fromisoformat(last_hb)
                next_hb_dt = last_hb_dt + timedelta(seconds=HEARTBEAT_INTERVAL)
                now = datetime.now()

                if next_hb_dt > now:
                    wait_seconds = (next_hb_dt - now).total_seconds()
                    print(f"Last heartbeat: {last_hb_dt.strftime('%H:%M:%S')}")
                    print(f"Next heartbeat: {next_hb_dt.strftime('%H:%M:%S')} (in {wait_seconds/60:.1f}m)")
                else:
                    # Past due - run immediately
                    overdue = (now - next_hb_dt).total_seconds()
                    print(f"Heartbeat overdue by {overdue/60:.1f}m - running now")
                    wait_seconds = 0
            else:
                # No previous heartbeat recorded - run immediately
                print("No previous heartbeat recorded - running now")
                wait_seconds = 0

            # Wait if needed
            if wait_seconds > 0:
                print(f"\nWaiting {wait_seconds/60:.1f}m until next heartbeat...")
                time.sleep(wait_seconds)

            # Run heartbeat
            try:
                heartbeat()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"Error: {e}")
                import traceback
                traceback.print_exc()

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stop_persona_watcher(watcher_observer, watcher_thread, watcher_stop)


if __name__ == "__main__":
    # Handle flags
    force_heartbeat = False
    if "--force-heartbeat" in sys.argv:
        force_heartbeat = True
        sys.argv.remove("--force-heartbeat")

    if "--no-sound" in sys.argv:
        SOUND_ENABLED = False
        set_muted(True)
        sys.argv.remove("--no-sound")

    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == "once":
            heartbeat()

        elif cmd == "status":
            # Show current API status
            state = load_state()
            api_status = state.get("comment_api_status", "unknown")
            fail_count = state.get("comment_api_fail_count", 0)
            print(f"Comment API Status: {api_status.upper()}")
            print(f"Consecutive failures: {fail_count}")
            if api_status == "down":
                outage_start = state.get("outage_start")
                if outage_start:
                    duration = (datetime.now() - datetime.fromisoformat(outage_start)).total_seconds()
                    print(f"Outage started: {outage_start}")
                    print(f"Duration: {duration/60:.1f} minutes")

        elif cmd == "reset-api":
            # Reset API status to unknown (will probe on next cycle)
            state = load_state()
            state["comment_api_status"] = "unknown"
            state["comment_api_fail_count"] = 0
            state["outage_start"] = None
            save_state(state)
            print("API status reset to UNKNOWN")
            print("Will probe on next heartbeat cycle")

        elif cmd == "mark-down":
            # Manually mark API as down
            state = load_state()
            state["comment_api_status"] = "down"
            state["outage_start"] = datetime.now().isoformat()
            save_state(state)
            print("API status manually set to DOWN")
            print("Running in posts-only mode")

        else:
            print(f"Unknown command: {cmd}")
            print("\nUsage: python heartbeat_full.py [command] [options]")
            print("Commands:")
            print("  (none)     - Run continuous service")
            print("  once       - Run single heartbeat cycle")
            print("  status     - Show current API status")
            print("  reset-api  - Reset API status (will probe next cycle)")
            print("  mark-down  - Manually mark API as down")
            print("\nOptions:")
            print("  --no-sound        - Disable audio notifications")
            print("  --force-heartbeat - Run heartbeat immediately on start")

    else:
        run_service(force_heartbeat=force_heartbeat)
