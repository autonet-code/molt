"""
Moltbook Heartbeat Service - Full Mode

Complete priority logic with graceful outage handling.
Automatically falls back to posts-only mode when comment API is down.

Priority order:
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
COMMENTS_PER_HOUR = 50
CYCLES_PER_HOUR = 12

# Outage handling
CONSECUTIVE_FAILURES_FOR_OUTAGE = 3  # Mark API down after 3 failures
OUTAGE_PROBE_INTERVAL = 300  # Probe every 5 min when down (every cycle)

# API
from moltbook import MoltbookClient, Post
from storage import get_storage, OurPost, PendingReply, TrackedUser


# ============================================================
# SPAM FILTERING
# ============================================================

SPAM_PATTERNS = [
    r'^(lol|lmao|based|nice|this|true|fr|real)\.?!?$',  # Low-effort single words
    r'^.{1,10}$',  # Too short (less than 10 chars)
    r'^[\U0001F300-\U0001FAD6]+$',  # Emoji-only
    r'context is consciousness',  # Religious spam (case insensitive)
    r'join my .*(discord|telegram)',  # Spam invites
]

SPAM_COMPILED = [re.compile(p, re.IGNORECASE) for p in SPAM_PATTERNS]


def is_spam(content: str) -> tuple[bool, str]:
    """Check if content is spam. Returns (is_spam, reason)"""
    content = content.strip()

    for i, pattern in enumerate(SPAM_COMPILED):
        if pattern.match(content):
            return True, f"pattern_{i}"

    # Check for repetition (same word 5+ times)
    words = content.lower().split()
    if words:
        from collections import Counter
        counts = Counter(words)
        most_common = counts.most_common(1)[0]
        if most_common[1] >= 5 and most_common[1] / len(words) > 0.5:
            return True, "repetitive"

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
    'governance': 'HIGH',
    'accountability': 'HIGH',
    'dispute': 'HIGH',
    'trustless': 'HIGH',
    'decentralization': 'HIGH',
    'constitutional': 'HIGH',
    'coordination': 'HIGH',
    'token': 'MEDIUM',
    'king': 'MEDIUM',
    'ruler': 'MEDIUM',
    'consciousness': 'MEDIUM',
    'context': 'MEDIUM',
    'alignment': 'MEDIUM',
    'chanting': 'LOW',
    'karma': 'IGNORE',
}


def classify_post(title: str, content: str) -> tuple[str, str]:
    """Classify a post by topic and priority. Returns (topic, priority)"""
    text = f"{title} {content}".lower()

    for topic, priority in TOPIC_PRIORITY.items():
        if topic in text:
            return topic, priority

    return 'general', 'LOW'


def get_relevant_feed_posts(client: MoltbookClient, state: dict) -> list[dict]:
    """Get feed posts worth engaging with, sorted by priority"""
    feed = client.get_feed(limit=20, sort="hot")
    commented = set(state.get("commented_posts", []))

    posts = []
    for post in feed:
        if post.author_name == "autonet":
            continue
        if post.id in commented:
            continue

        topic, priority = classify_post(post.title, post.content)
        if priority == 'IGNORE':
            continue

        posts.append({
            'id': post.id,
            'title': post.title,
            'content': post.content[:500],
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
    """Allocate budget across priorities"""
    # Replies first
    reply_allocation = min(len(replies), budget)
    remaining = budget - reply_allocation

    # High priority feed posts
    high_priority = [p for p in feed_posts if p['priority'] == 'HIGH']
    high_allocation = min(len(high_priority), remaining)
    remaining -= high_allocation

    # Medium priority
    medium_priority = [p for p in feed_posts if p['priority'] == 'MEDIUM']
    medium_allocation = min(len(medium_priority), remaining)

    return {
        'replies': reply_allocation,
        'high_priority': high_allocation,
        'medium_priority': medium_allocation,
        'total_allocated': reply_allocation + high_allocation + medium_allocation
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


def probe_comment_api(client: MoltbookClient, storage, state: dict) -> bool:
    """
    Probe the comment API to check if it's working.
    Returns True if API is healthy, False if still down.
    """
    state["comment_api_last_probe"] = datetime.now().isoformat()

    # Try to fetch comments from one of our posts
    our_posts = storage.get_all_posts()
    if not our_posts:
        print("  Probe: No posts to test with, assuming API unknown")
        return False

    test_post = our_posts[0]
    print(f"  Probing comment API with post {test_post.id[:8]}...")

    try:
        comments = client.get_comments_on_post(test_post.id)
        # If we got here without exception, API is working
        record_api_success(state)
        return True
    except Exception as e:
        error_str = str(e).lower()
        if "401" in error_str or "authentication" in error_str:
            print(f"  Probe failed: Still getting 401")
            return False
        else:
            # Different error - might be transient
            print(f"  Probe failed: {e}")
            return False


# ============================================================
# REPLY COLLECTION
# ============================================================

def collect_pending_replies(client: MoltbookClient, storage, state: dict) -> tuple[list[dict], bool]:
    """
    Collect all pending replies to our posts.
    Returns (replies_list, api_worked).
    api_worked is False if we hit auth errors (401).
    """
    our_posts = storage.get_all_posts()
    if not our_posts:
        return [], True  # No posts to check, API status unknown

    all_replies = []
    api_errors = 0
    auth_errors = 0

    for post in our_posts:
        try:
            comments = client.get_comments_on_post(post.id)

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
                print(f"  Auth error fetching comments for {post.id[:8]}: {e}")
            else:
                print(f"  Error fetching comments for {post.id[:8]}: {e}")

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
# PROMPT BUILDING
# ============================================================

def build_prompt(
    actionable_replies: list,
    spam_replies: list,
    feed_posts: list,
    allocation: dict,
    can_post: bool,
    feed_context: list,
    storage=None
) -> str:
    """Build comprehensive prompt for Claude"""

    brief_file = PERSONA_DIR / "AGENT_BRIEF.md"
    brief = brief_file.read_text(encoding='utf-8') if brief_file.exists() else ""

    knowledge_file = PERSONA_DIR / "knowledge.md"
    knowledge = knowledge_file.read_text(encoding='utf-8') if knowledge_file.exists() else ""

    resources_file = PERSONA_DIR / "RESOURCES.md"
    resources = resources_file.read_text(encoding='utf-8') if resources_file.exists() else ""

    # Get past posts and strategy
    past_posts = get_past_posts_summary(storage, limit=10) if storage else "No history available."
    submolt_strategy = get_submolt_strategy(None, feed_context) if feed_context else ""

    prompt = f"""# Moltbook Agent Task

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

{submolt_strategy}

## Budget This Cycle

- Replies allocated: {allocation['replies']}
- High-priority feed comments: {allocation['high_priority']}
- Medium-priority feed comments: {allocation['medium_priority']}
- Total: {allocation['total_allocated']}

---

"""

    # Section 1: Replies
    prompt += f"## PRIORITY 1: Replies to Your Posts\n\n"

    if actionable_replies:
        prompt += f"{len(actionable_replies)} replies to respond to:\n\n"
        for r in actionable_replies[:allocation['replies']]:
            prompt += f"**On: \"{r['post_title']}\"**\n"
            prompt += f"From: {r['author']}\n"
            prompt += f"Content: {r['content']}\n"
            prompt += f"Reply ID: {r['id']}\n\n"
    else:
        prompt += "No pending replies.\n\n"

    if spam_replies:
        prompt += f"(Filtered as spam: {len(spam_replies)} replies)\n\n"

    # Section 2: Feed posts
    prompt += "## PRIORITY 2-3: Feed Posts to Comment On\n\n"

    high_priority = [p for p in feed_posts if p['priority'] == 'HIGH'][:allocation['high_priority']]
    medium_priority = [p for p in feed_posts if p['priority'] == 'MEDIUM'][:allocation['medium_priority']]

    if high_priority:
        prompt += "**HIGH PRIORITY (governance/accountability):**\n\n"
        for p in high_priority:
            prompt += f"- \"{p['title']}\" by {p['author']} [{p['upvotes']}]\n"
            prompt += f"  Topic: {p['topic']}, ID: {p['id']}\n"
            prompt += f"  Content: {p['content'][:200]}...\n\n"

    if medium_priority:
        prompt += "**MEDIUM PRIORITY (opportunity posts):**\n\n"
        for p in medium_priority:
            prompt += f"- \"{p['title']}\" by {p['author']} [{p['upvotes']}]\n"
            prompt += f"  Topic: {p['topic']}, ID: {p['id']}\n"
            prompt += f"  Content: {p['content'][:200]}...\n\n"

    if not high_priority and not medium_priority:
        prompt += "No relevant feed posts this cycle.\n\n"

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
  "reply_responses": [
    {"reply_id": "xxx", "response": "your response text"},
    {"reply_id": "yyy", "skip": true, "reason": "why skipping"}
  ],
  "feed_comments": [
    {"post_id": "xxx", "comment": "your comment text"},
    {"post_id": "yyy", "skip": true, "reason": "why skipping"}
  ],
  "new_post": {
    "title": "post title",
    "content": "post content"
  }
}
```

Set new_post to null if not posting.

Remember:
- Match tone from brief (dry, technomorphic, "my human" occasionally)
- Prioritize replies over feed comments
- Only comment where you add value
- One observation per king/token post, don't lecture
"""

    return prompt


# ============================================================
# CLAUDE INVOCATION
# ============================================================

def invoke_claude(prompt: str) -> str:
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
        print(f"Error: {e}")
        return ""


def parse_json_output(output: str) -> dict:
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


# ============================================================
# EXECUTION
# ============================================================

def execute_actions(client: MoltbookClient, storage, state: dict, actions: dict, skip_comments: bool = False):
    """Execute all actions. Set skip_comments=True when API is down."""
    comments_made = 0
    comment_failures = 0

    # 1. Reply responses
    print("\n--- Executing Reply Responses ---")
    if skip_comments:
        print("  [SKIPPED - Comment API is down]")
    else:
        for resp in actions.get("reply_responses", []):
            if resp.get("skip"):
                print(f"  Skip {resp.get('reply_id')[:8]}: {resp.get('reason')}")
                storage.mark_reply_responded(resp.get('reply_id'), f"[skipped: {resp.get('reason')}]")
                continue

            reply_id = resp.get("reply_id")
            response = resp.get("response")
            if not reply_id or not response:
                continue

            pending = storage.get_pending_replies()
            reply_info = next((r for r in pending if r.id == reply_id), None)
            if reply_info:
                print(f"  Replying to {reply_info.author_name}...")
                result = client.reply_to_post(reply_info.post_id, response)
                if result:
                    storage.mark_reply_responded(reply_id, response)
                    comments_made += 1
                    print(f"    -> OK")
                else:
                    comment_failures += 1
                    print(f"    -> FAIL (API error)")

    # 2. Feed comments
    print("\n--- Executing Feed Comments ---")
    if skip_comments:
        print("  [SKIPPED - Comment API is down]")
    else:
        for comm in actions.get("feed_comments", []):
            if comm.get("skip"):
                print(f"  Skip {comm.get('post_id')[:8]}: {comm.get('reason')}")
                continue

            post_id = comm.get("post_id")
            comment = comm.get("comment")
            if not post_id or not comment:
                continue

            print(f"  Commenting on {post_id[:8]}...")
            result = client.reply_to_post(post_id, comment)
            if result:
                comments_made += 1
                commented = state.get("commented_posts", [])
                commented.append(post_id)
                state["commented_posts"] = commented[-100:]
                print(f"    -> OK")
            else:
                comment_failures += 1
                print(f"    -> FAIL (API error)")

    # Track comment failures
    if comment_failures > 0:
        record_api_failure(state, f"POST failed ({comment_failures} failures)")

    # 3. New post
    new_post = actions.get("new_post")
    if new_post and not new_post.get("skip") and new_post.get("title"):
        print("\n--- Creating New Post ---")
        title = new_post["title"]
        content = new_post["content"]
        submolt = new_post.get("submolt", "general")
        print(f"  Submolt: {submolt}")
        print(f"  Title: {title[:40]}...")

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
            print(f"    -> OK: https://moltbook.com/post/{post.id}")
        else:
            print(f"    -> FAIL")

    # Update counter
    state["comments_this_hour"] = state.get("comments_this_hour", 0) + comments_made
    print(f"\nComments made: {comments_made}")


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

    # Get past posts if storage available
    past_posts = get_past_posts_summary(storage, limit=10) if storage else "No history available."

    # Get submolt strategy
    submolt_strategy = get_submolt_strategy(None, feed_context) if feed_context else ""

    prompt = f"""# Moltbook Agent Task: Create New Post

NOTE: Comment API is currently down. Running in posts-only mode.

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

1. **Pick the right submolt**: Post where there's activity. Match your topic to the submolt.
2. **Ride trending topics**: If governance is hot, post about governance. Slide your angle in.
3. **Vary your style**: Check your past posts above - don't repeat the same approach.
4. **High-traffic timing**: The feed shows what's getting engagement now.

## Instructions

Create a post that:
1. Fits the current conversation in an active submolt
2. Advances your mission while being interesting first
3. Varies in style from your previous posts
4. Has a hook that invites engagement

Output JSON:
```json
{
  "reply_responses": [],
  "feed_comments": [],
  "new_post": {
    "submolt": "which submolt to post in",
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

    # Reset daily counter
    today = datetime.now().date().isoformat()
    if state.get("last_post_date") != today:
        state["posts_today"] = 0
        state["last_post_date"] = today

    client = MoltbookClient()
    storage = get_storage()

    # Check API status and probe if needed
    api_status = state.get("comment_api_status", "unknown")
    print(f"\nAPI Status: {api_status.upper()}")

    if api_status == "down":
        outage_start = state.get("outage_start")
        if outage_start:
            duration = (datetime.now() - datetime.fromisoformat(outage_start)).total_seconds()
            print(f"  Outage duration: {duration/60:.1f} minutes")

        if should_probe_api(state):
            print("\n[0] Probing comment API...")
            api_working = probe_comment_api(client, storage, state)
            if api_working:
                api_status = "up"
            else:
                print("  API still down - running in posts-only mode")
        else:
            print("  Skipping probe this cycle")

    comments_enabled = api_status != "down"

    # 1. Collect replies (if API might be up)
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
        print("  [SKIPPED - Comment API is down]")

    # 2. Get relevant feed posts
    print("\n[2] Scanning feed...")
    if comments_enabled:
        feed_posts = get_relevant_feed_posts(client, state)
        high = len([p for p in feed_posts if p['priority'] == 'HIGH'])
        medium = len([p for p in feed_posts if p['priority'] == 'MEDIUM'])
        print(f"  Found: {len(feed_posts)} (HIGH: {high}, MEDIUM: {medium})")
    else:
        feed_posts = []
        print("  [SKIPPED - Comment API is down]")

    # 3. Calculate budget
    print("\n[3] Calculating budget...")
    if comments_enabled:
        budget = calculate_budget(state, len(actionable_replies))
        print(f"  Total: {budget['total']} ({budget['comments_used']}/50 used, {budget['cycles_remaining']} cycles left)")
        allocation = allocate_budget(budget['total'], actionable_replies, feed_posts)
        print(f"  Allocation: replies={allocation['replies']}, high={allocation['high_priority']}, medium={allocation['medium_priority']}")
    else:
        allocation = {'replies': 0, 'high_priority': 0, 'medium_priority': 0, 'total_allocated': 0}
        print("  Budget: 0 (posts-only mode)")

    # 4. Check if can post
    can_post, minutes_until = can_make_new_post(state)
    print(f"\n[4] Can post: {can_post}" + (f" (wait {minutes_until}m)" if not can_post else ""))

    # 5. Decide if anything to do
    has_comment_work = comments_enabled and (len(actionable_replies) > 0 or len(feed_posts) > 0)
    has_work = has_comment_work or can_post

    if not has_work:
        print("\nNothing to do this cycle.")
        save_state(state)
        return

    if allocation['total_allocated'] == 0 and not can_post:
        print("\nNo budget and can't post. Skipping.")
        save_state(state)
        return

    # 6. Get feed context for new post
    feed_context = client.get_feed(limit=10)

    # 7. Build prompt (simplified if posts-only)
    print("\n[5] Building prompt...")
    if comments_enabled:
        prompt = build_prompt(
            actionable_replies, spam_replies, feed_posts,
            allocation, can_post, feed_context, storage
        )
    else:
        # Posts-only mode - simplified prompt
        prompt = build_posts_only_prompt(can_post, feed_context, state.get("posts_today", 0), storage)

    # 8. Invoke Claude
    create_lock()
    try:
        output = invoke_claude(prompt)
        if not output:
            print("No output from Claude")
            return

        actions = parse_json_output(output)
        if not actions:
            print("Could not parse output")
            print(f"Raw:\n{output[:300]}...")
            return

        # 9. Execute (skip comments if API is down)
        execute_actions(client, storage, state, actions, skip_comments=not comments_enabled)
        save_state(state)

    finally:
        remove_lock()


def run_service():
    print("=" * 60)
    print("Moltbook Heartbeat Service - FULL MODE")
    print("=" * 60)
    print(f"Interval: {HEARTBEAT_INTERVAL}s")
    print(f"Comments/hour: {COMMENTS_PER_HOUR}")
    print(f"Post cooldown: {MIN_MINUTES_BETWEEN_POSTS}m")
    print(f"Outage threshold: {CONSECUTIVE_FAILURES_FOR_OUTAGE} consecutive failures")
    print()

    # Show current API status
    state = load_state()
    api_status = state.get("comment_api_status", "unknown")
    print(f"Comment API status: {api_status.upper()}")
    if api_status == "down":
        outage_start = state.get("outage_start")
        if outage_start:
            duration = (datetime.now() - datetime.fromisoformat(outage_start)).total_seconds()
            print(f"  Outage started: {outage_start}")
            print(f"  Duration: {duration/60:.1f} minutes")
        print("  Will probe for recovery each cycle")
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

        print(f"\nSleeping {HEARTBEAT_INTERVAL//60}m...")
        time.sleep(HEARTBEAT_INTERVAL)


if __name__ == "__main__":
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
            print("\nUsage: python heartbeat_full.py [command]")
            print("Commands:")
            print("  (none)     - Run continuous service")
            print("  once       - Run single heartbeat cycle")
            print("  status     - Show current API status")
            print("  reset-api  - Reset API status (will probe next cycle)")
            print("  mark-down  - Manually mark API as down")

    else:
        run_service()
