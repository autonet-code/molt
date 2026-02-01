"""
Moltbook daemon - runs in background, polls for activity, decides actions

This is the orchestration layer. It:
1. Polls for new replies to our posts
2. Scans feed for relevant topics
3. Decides when to post or reply
4. Queues actions for execution (or human review)

The actual content generation is delegated - this just handles the "when" and "what type".
"""

import time
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass, field
from enum import Enum

from storage import get_storage, OurPost, PendingReply, TrackedUser
from persona import get_persona
from moltbook import MoltbookClient, Post, Comment

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(Path(__file__).parent / "daemon.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


class ActionType(Enum):
    REPLY = "reply"
    POST = "post"
    UPVOTE = "upvote"
    DOWNVOTE = "downvote"
    IGNORE = "ignore"


@dataclass
class QueuedAction:
    action_type: ActionType
    target_id: str  # post_id or comment_id
    context: dict = field(default_factory=dict)  # Additional context for generation
    priority: int = 5  # 1-10, higher = more urgent
    created_at: str = ""
    status: str = "pending"  # pending, approved, executed, rejected


class MoltbookDaemon:
    def __init__(self,
                 poll_interval: int = 300,  # 5 minutes
                 auto_execute: bool = False,  # If False, queue for review
                 content_generator: Optional[Callable] = None):
        self.client = MoltbookClient()
        self.storage = get_storage()
        self.persona = get_persona()
        self.poll_interval = poll_interval
        self.auto_execute = auto_execute
        self.content_generator = content_generator  # Function to generate replies/posts

        self.action_queue: list[QueuedAction] = []
        self.running = False

        # Rate limiting state
        self._last_post_time: Optional[datetime] = None
        self._posts_today: int = 0
        self._replies_this_hour: int = 0
        self._hour_start: datetime = datetime.utcnow()

    def start(self):
        """Start the daemon loop"""
        self.running = True
        log.info(f"Daemon starting. Poll interval: {self.poll_interval}s, Auto-execute: {self.auto_execute}")
        self.storage.log_activity("daemon_start", f"poll_interval={self.poll_interval}")

        while self.running:
            try:
                self._poll_cycle()
            except Exception as e:
                log.error(f"Error in poll cycle: {e}")
                self.storage.log_activity("error", str(e))

            if self.running:
                log.info(f"Sleeping {self.poll_interval}s until next poll...")
                time.sleep(self.poll_interval)

    def stop(self):
        """Stop the daemon"""
        self.running = False
        log.info("Daemon stopping...")
        self.storage.log_activity("daemon_stop", "")

    def _poll_cycle(self):
        """One polling cycle"""
        log.info("=== Poll cycle starting ===")

        # Reset hourly counter if needed
        now = datetime.utcnow()
        if now - self._hour_start > timedelta(hours=1):
            self._replies_this_hour = 0
            self._hour_start = now

        # Reset daily counter if needed
        if self._last_post_time and now.date() > self._last_post_time.date():
            self._posts_today = 0

        # 1. Check for replies to our posts
        self._check_for_replies()

        # 2. Scan feed for relevant topics
        self._scan_feed()

        # 3. Process action queue
        self._process_queue()

        # 4. Update our post stats
        self._update_post_stats()

        log.info(f"=== Poll cycle complete. Queue: {len(self.action_queue)} actions ===")

    def _check_for_replies(self):
        """Check for new replies to our posts"""
        log.info("Checking for replies...")

        our_posts = self.storage.get_all_posts()
        if not our_posts:
            log.info("No posts yet, skipping reply check")
            return

        for post in our_posts:
            try:
                comments = self.client.get_comments_on_post(post.id)
                for comment in comments:
                    # Skip our own comments
                    if comment.author_name == self.persona.name:
                        continue

                    # Check if we've seen this reply
                    existing = self.storage.get_pending_replies()
                    if any(r.id == comment.id for r in existing):
                        continue

                    # New reply - save and maybe queue action
                    reply = PendingReply(
                        id=comment.id,
                        post_id=post.id,
                        post_title=post.title,
                        author_name=comment.author_name,
                        content=comment.content,
                        created_at=comment.created_at,
                        responded=False
                    )
                    self.storage.save_reply(reply)
                    log.info(f"New reply from {comment.author_name} on '{post.title[:30]}...'")

                    # Track the user
                    self._track_user(comment.author_name)

                    # Decide if we should respond
                    if self._should_respond_to(reply):
                        self._queue_action(ActionType.REPLY, comment.id, {
                            "post_id": post.id,
                            "post_title": post.title,
                            "reply_content": comment.content,
                            "reply_author": comment.author_name
                        }, priority=7)

            except Exception as e:
                log.error(f"Error checking replies for post {post.id}: {e}")

    def _scan_feed(self):
        """Scan feed for topics we might want to engage with"""
        log.info("Scanning feed...")

        try:
            feed = self.client.get_feed(limit=20, sort="hot")
            topics = self.storage.get_topics()
            topic_keywords = {t["keyword"].lower(): t for t in topics}

            for post in feed:
                # Skip our own posts
                if post.author_name == self.persona.name:
                    continue

                # Check if post matches any of our topics
                post_text = f"{post.title} {post.content}".lower()
                for keyword, topic_config in topic_keywords.items():
                    if keyword in post_text:
                        if topic_config["engage_mode"] == "ignore":
                            continue

                        # Track the author
                        self._track_user(post.author_name)

                        # Check if we've already engaged
                        # (This would need more sophisticated tracking in production)
                        log.info(f"Relevant post found: '{post.title[:40]}...' (keyword: {keyword})")

                        if topic_config["engage_mode"] == "comment":
                            self._queue_action(ActionType.REPLY, post.id, {
                                "post_title": post.title,
                                "post_content": post.content,
                                "post_author": post.author_name,
                                "matched_topic": keyword
                            }, priority=topic_config["priority"])
                        break  # Only match first topic

        except Exception as e:
            log.error(f"Error scanning feed: {e}")

    def _should_respond_to(self, reply: PendingReply) -> bool:
        """Decide if we should respond to a reply"""
        # Check user relationship
        user = self.storage.get_user(reply.author_name)
        if user and user.relationship == "ignore":
            log.info(f"Ignoring reply from {reply.author_name} (relationship: ignore)")
            return False

        # Check avoid topics
        content_lower = reply.content.lower()
        for topic in self.persona.avoid_topics:
            if topic.lower() in content_lower:
                log.info(f"Ignoring reply - matches avoid topic: {topic}")
                return False

        # Check rate limits
        if self._replies_this_hour >= self.persona.max_replies_per_hour:
            log.info("Rate limit: max replies per hour reached")
            return False

        return True

    def _track_user(self, name: str):
        """Track a user we've encountered"""
        existing = self.storage.get_user(name)
        if existing:
            self.storage.increment_interaction(name)
        else:
            user = TrackedUser(
                id=name,  # Using name as ID for now
                name=name,
                first_seen=datetime.utcnow().isoformat(),
                last_seen=datetime.utcnow().isoformat(),
                interaction_count=1
            )
            self.storage.save_user(user)
            log.info(f"New user tracked: {name}")

    def _queue_action(self, action_type: ActionType, target_id: str, context: dict, priority: int = 5):
        """Add an action to the queue"""
        # Check if already queued
        for action in self.action_queue:
            if action.target_id == target_id and action.action_type == action_type:
                return

        action = QueuedAction(
            action_type=action_type,
            target_id=target_id,
            context=context,
            priority=priority,
            created_at=datetime.utcnow().isoformat()
        )
        self.action_queue.append(action)
        self.action_queue.sort(key=lambda a: a.priority, reverse=True)
        log.info(f"Queued {action_type.value} for {target_id} (priority: {priority})")

    def _process_queue(self):
        """Process queued actions"""
        if not self.action_queue:
            return

        log.info(f"Processing queue ({len(self.action_queue)} actions)...")

        if self.auto_execute and self.content_generator:
            # Auto-execute mode - generate and execute
            for action in self.action_queue[:]:
                if action.status != "pending":
                    continue

                if action.action_type == ActionType.REPLY:
                    if self._replies_this_hour >= self.persona.max_replies_per_hour:
                        log.info("Rate limit reached, stopping queue processing")
                        break

                    content = self.content_generator(action)
                    if content:
                        success = self.client.reply_to_post(
                            action.context.get("post_id", action.target_id),
                            content
                        )
                        if success:
                            action.status = "executed"
                            self._replies_this_hour += 1
                            self.storage.log_activity("reply_sent", json.dumps({
                                "target": action.target_id,
                                "content": content[:100]
                            }))
                            log.info(f"Executed reply to {action.target_id}")
                        else:
                            action.status = "failed"
                            log.error(f"Failed to execute reply to {action.target_id}")

            # Clean up executed actions
            self.action_queue = [a for a in self.action_queue if a.status == "pending"]
        else:
            # Manual mode - just report
            log.info("Manual mode - actions queued for review:")
            for action in self.action_queue:
                log.info(f"  [{action.priority}] {action.action_type.value}: {action.target_id}")

    def _update_post_stats(self):
        """Update stats on our posts"""
        our_posts = self.storage.get_all_posts()
        for post in our_posts:
            try:
                data = self.client.get_post(post.id)
                if data.get("success") and "post" in data:
                    p = data["post"]
                    self.storage.update_post_stats(
                        post.id,
                        p.get("upvotes", 0),
                        p.get("downvotes", 0),
                        p.get("comment_count", 0)
                    )
            except Exception as e:
                log.error(f"Error updating stats for post {post.id}: {e}")

    # === Manual controls ===

    def get_queue(self) -> list[QueuedAction]:
        """Get current action queue"""
        return self.action_queue

    def approve_action(self, index: int, content: str = None):
        """Approve and execute a queued action"""
        if index >= len(self.action_queue):
            return False

        action = self.action_queue[index]
        # Execute would go here
        action.status = "approved"
        return True

    def reject_action(self, index: int):
        """Reject a queued action"""
        if index >= len(self.action_queue):
            return False
        self.action_queue.pop(index)
        return True

    def poll_once(self):
        """Run a single poll cycle (for manual triggering)"""
        self._poll_cycle()

    def status(self) -> dict:
        """Get daemon status"""
        return {
            "running": self.running,
            "queue_size": len(self.action_queue),
            "posts_today": self._posts_today,
            "replies_this_hour": self._replies_this_hour,
            "max_posts_per_day": self.persona.max_posts_per_day,
            "max_replies_per_hour": self.persona.max_replies_per_hour,
            "recent_activity": self.storage.get_recent_activity(10)
        }


# CLI for testing
if __name__ == "__main__":
    import sys

    daemon = MoltbookDaemon(poll_interval=60, auto_execute=False)

    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == "once":
            daemon.poll_once()

        elif cmd == "status":
            print(json.dumps(daemon.status(), indent=2, default=str))

        elif cmd == "queue":
            queue = daemon.get_queue()
            if not queue:
                print("Queue is empty")
            else:
                for i, action in enumerate(queue):
                    print(f"[{i}] {action.action_type.value} -> {action.target_id}")
                    print(f"    Priority: {action.priority}, Context: {json.dumps(action.context)[:100]}")

        elif cmd == "start":
            daemon.start()

        else:
            print(f"Unknown command: {cmd}")
    else:
        print("Usage: python daemon.py <command>")
        print("Commands: once, status, queue, start")
