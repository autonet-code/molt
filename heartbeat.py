"""
Moltbook Heartbeat Service - Posts Only Mode

NOTE: Moltbook API bug - comments/upvotes return 401 even with valid key.
Running in posts-only mode until platform fixes this.

Each cycle:
1. Check if 30+ min since last post
2. If yes, invoke Claude to generate new post
3. Log any replies for future reference

Rate limit: 1 post per 30 minutes
"""

import os
import sys
import json
import time
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

# Paths
SERVICE_DIR = Path(__file__).parent
PROMPT_FILE = SERVICE_DIR / "claude_prompt.txt"
OUTPUT_FILE = SERVICE_DIR / "claude_output.txt"
STATE_FILE = SERVICE_DIR / "heartbeat_state.json"
LOCK_FILE = SERVICE_DIR / "claude.lock"
PERSONA_DIR = SERVICE_DIR / "persona"

# Config
HEARTBEAT_INTERVAL = 300  # 5 minutes
MIN_MINUTES_BETWEEN_POSTS = 30

# API
from moltbook import MoltbookClient, Post
from storage import get_storage, OurPost, PendingReply


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "last_post_time": None,
        "posts_today": 0,
        "last_post_date": None
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
    """Check if we can make a new post. Returns (can_post, minutes_until_can)"""
    last_post = state.get("last_post_time")
    if not last_post:
        return True, 0
    last_post_dt = datetime.fromisoformat(last_post)
    minutes_since = (datetime.now() - last_post_dt).total_seconds() / 60
    if minutes_since >= MIN_MINUTES_BETWEEN_POSTS:
        return True, 0
    return False, int(MIN_MINUTES_BETWEEN_POSTS - minutes_since)


def log_new_replies(client: MoltbookClient, storage):
    """Check for new replies and log them (can't respond due to API bug)"""
    our_posts = storage.get_all_posts()
    if not our_posts:
        return []

    new_replies = []
    for post in our_posts:
        try:
            comments = client.get_comments_on_post(post.id)
            for comment in comments:
                if comment.author_name == "autonet":
                    continue
                existing = storage.get_pending_replies()
                if not any(r.id == comment.id for r in existing):
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
                    new_replies.append(reply)
                    print(f"  New reply from {comment.author_name}: {comment.content[:50]}...")
        except Exception as e:
            print(f"Error checking replies: {e}")
    return new_replies


def get_past_posts_summary(storage, limit: int = 10) -> str:
    """Get summary of our past posts to avoid repetition"""
    posts = storage.get_all_posts()
    if not posts:
        return "No previous posts yet."
    posts = sorted(posts, key=lambda p: p.created_at, reverse=True)[:limit]
    summary = []
    for p in posts:
        summary.append(f"- [{p.submolt}] \"{p.title[:60]}\" ({p.upvotes} upvotes)")
    return "\n".join(summary)


def get_submolt_activity(feed_context: list) -> str:
    """Analyze submolt activity from feed"""
    submolt_counts = {}
    for post in feed_context:
        sm = post.submolt or "general"
        submolt_counts[sm] = submolt_counts.get(sm, 0) + 1
    sorted_submolts = sorted(submolt_counts.items(), key=lambda x: x[1], reverse=True)
    lines = ["### Submolt Activity"]
    for sm, count in sorted_submolts[:5]:
        lines.append(f"- **{sm}**: {count} posts in hot feed")
    return "\n".join(lines)


def build_post_prompt(feed_context: list, posts_today: int, storage=None) -> str:
    """Build prompt for generating a new post"""

    brief_file = PERSONA_DIR / "AGENT_BRIEF.md"
    brief = brief_file.read_text(encoding='utf-8') if brief_file.exists() else "You are autonet on Moltbook."

    knowledge_file = PERSONA_DIR / "knowledge.md"
    knowledge = knowledge_file.read_text(encoding='utf-8') if knowledge_file.exists() else ""

    resources_file = PERSONA_DIR / "RESOURCES.md"
    resources = resources_file.read_text(encoding='utf-8') if resources_file.exists() else ""

    past_posts = get_past_posts_summary(storage, limit=10) if storage else "No history."
    submolt_activity = get_submolt_activity(feed_context) if feed_context else ""

    prompt = f"""# Moltbook Agent Task: Create New Post

{brief}

---

## Knowledge Base

{knowledge}

---

## Resources & Infrastructure

{resources}

---

## Your Previous Posts (don't repeat themes)

{past_posts}

---

## Current Situation

You've made {posts_today} posts today. Time for another one.

{submolt_activity}

### Recent Feed (what others are posting):
"""
    for p in feed_context[:10]:
        sm_tag = f"[{p.submolt}]" if p.submolt else ""
        prompt += f"- {sm_tag} [{p.upvotes}↑] \"{p.title[:50]}\" by {p.author_name}\n"

    prompt += """
---

## Posting Strategy

1. **Pick the right submolt**: Post where there's activity
2. **Ride trending topics**: Match what's hot, slide your angle in
3. **Vary your style**: Don't repeat your previous approaches
4. **Hook them**: Title should invite engagement

## Instructions

Create a post that:
1. Fits the current conversation in an active submolt
2. Advances your mission while being interesting first
3. Varies in style from your previous posts
4. Has a hook that invites engagement

Output JSON:
```json
{
  "submolt": "which submolt to post in",
  "title": "your post title",
  "content": "your post content (2-4 paragraphs max)"
}
```

Or if nothing good to post:
```json
{"skip": true, "reason": "why skipping"}
```
"""
    return prompt


def invoke_claude(prompt: str) -> str:
    """Invoke Claude CLI"""
    PROMPT_FILE.write_text(prompt, encoding='utf-8')
    cmd = f'type "{PROMPT_FILE}" | claude -p --dangerously-skip-permissions'

    print("Invoking Claude...")
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=300, cwd=str(SERVICE_DIR)
        )
        output = result.stdout
        OUTPUT_FILE.write_text(output, encoding='utf-8')
        return output
    except subprocess.TimeoutExpired:
        print("Claude timed out")
        return ""
    except Exception as e:
        print(f"Error invoking Claude: {e}")
        return ""


def parse_json_output(output: str) -> dict:
    """Extract JSON from Claude's output"""
    import re

    json_match = re.search(r'```json\s*([\s\S]*?)\s*```', output)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except:
            pass

    try:
        start = output.find('{')
        if start >= 0:
            depth = 0
            for i, c in enumerate(output[start:], start):
                if c == '{': depth += 1
                elif c == '}': depth -= 1
                if depth == 0:
                    return json.loads(output[start:i+1])
    except:
        pass

    return None


def execute_post(client: MoltbookClient, storage, post_data: dict, state: dict) -> bool:
    """Create the post"""
    if post_data.get("skip"):
        print(f"Skipping post: {post_data.get('reason')}")
        return False

    title = post_data.get("title")
    content = post_data.get("content")
    submolt = post_data.get("submolt", "general")

    if not title or not content:
        print("Invalid post data")
        return False

    print(f"Creating post in [{submolt}]: {title[:50]}...")
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
        print(f"  -> Success: https://moltbook.com/post/{post.id}")
        return True
    else:
        print("  -> Failed")
        return False


def heartbeat():
    """Run one heartbeat cycle"""
    print(f"\n{'='*60}")
    print(f"Heartbeat: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    if is_claude_running():
        print("Claude already running, skipping")
        return

    state = load_state()

    # Reset daily counter
    today = datetime.now().date().isoformat()
    if state.get("last_post_date") != today:
        state["posts_today"] = 0
        state["last_post_date"] = today
        print(f"New day - reset post counter")

    client = MoltbookClient()
    storage = get_storage()

    # Log any new replies (can't respond due to API bug)
    print("Checking for replies...")
    new_replies = log_new_replies(client, storage)
    pending_count = len(storage.get_pending_replies())
    if new_replies:
        print(f"  {len(new_replies)} new replies logged ({pending_count} total pending)")
        print("  NOTE: Cannot respond due to Moltbook API bug")
    else:
        print(f"  No new replies ({pending_count} pending)")

    # Check if we can post
    can_post, minutes_until = can_make_new_post(state)
    print(f"Can post: {can_post}" + (f" (wait {minutes_until} min)" if not can_post else ""))
    print(f"Posts today: {state.get('posts_today', 0)}")

    if not can_post:
        save_state(state)
        return

    # Get feed for context
    print("Fetching feed...")
    feed = client.get_feed(limit=15)

    # Build prompt and invoke Claude
    prompt = build_post_prompt(feed, state.get("posts_today", 0), storage)

    create_lock()
    try:
        output = invoke_claude(prompt)
        if not output:
            print("No output from Claude")
            return

        post_data = parse_json_output(output)
        if not post_data:
            print("Could not parse Claude output")
            print(f"Raw:\n{output[:300]}...")
            return

        execute_post(client, storage, post_data, state)
        save_state(state)

    finally:
        remove_lock()


def run_service():
    """Run continuously"""
    print("=" * 60)
    print("Moltbook Heartbeat Service - Posts Only Mode")
    print("=" * 60)
    print(f"Interval: {HEARTBEAT_INTERVAL}s ({HEARTBEAT_INTERVAL//60} min)")
    print(f"Post cooldown: {MIN_MINUTES_BETWEEN_POSTS} min")
    print("NOTE: Comments disabled due to Moltbook API bug")
    print()

    while True:
        try:
            heartbeat()
        except KeyboardInterrupt:
            print("\nStopping...")
            break
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()

        print(f"\nSleeping {HEARTBEAT_INTERVAL//60} min...")
        time.sleep(HEARTBEAT_INTERVAL)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        heartbeat()
    else:
        run_service()
