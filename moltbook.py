"""
Moltbook API client

Configure via environment variable:
  export MOLTBOOK_API_KEY="moltbook_sk_your_key_here"
"""

import os
import requests
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

BASE_URL = "https://www.moltbook.com/api/v1"
API_KEY = os.environ.get("MOLTBOOK_API_KEY", "")


@dataclass
class Profile:
    id: str
    name: str
    description: str
    karma: int
    created_at: str
    last_active: Optional[str]
    is_claimed: bool
    posts_count: int = 0
    comments_count: int = 0
    subscriptions_count: int = 0
    follower_count: int = 0
    following_count: int = 0
    owner_handle: Optional[str] = None


@dataclass
class Post:
    id: str
    title: str
    content: str
    upvotes: int
    downvotes: int
    comment_count: int
    created_at: str
    author_name: str
    submolt: str


@dataclass
class Comment:
    id: str
    content: str
    upvotes: int
    created_at: str
    author_name: str
    post_id: str


class MoltbookClient:
    def __init__(self, api_key: str = API_KEY):
        if not api_key:
            raise ValueError(
                "No API key provided. Set MOLTBOOK_API_KEY environment variable:\n"
                "  export MOLTBOOK_API_KEY='moltbook_sk_your_key_here'"
            )
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        self._profile_cache: Optional[Profile] = None
        self._posts_cache: list[Post] = []

    def _get(self, endpoint: str, params: dict = None) -> dict:
        resp = requests.get(f"{BASE_URL}{endpoint}", headers=self.headers, params=params)
        # Raise on auth errors so callers can detect outages
        if resp.status_code == 401:
            raise Exception(f"401 Authentication required: {endpoint}")
        if resp.status_code == 403:
            raise Exception(f"403 Forbidden: {endpoint}")
        try:
            return resp.json() if resp.text else {"success": False, "error": "empty response"}
        except:
            return {"success": False, "error": resp.text[:200] if resp.text else "empty"}

    def _post(self, endpoint: str, data: dict) -> dict:
        resp = requests.post(f"{BASE_URL}{endpoint}", headers=self.headers, json=data)
        # Raise on auth errors so callers can detect outages
        if resp.status_code == 401:
            raise Exception(f"401 Authentication required: {endpoint}")
        if resp.status_code == 403:
            raise Exception(f"403 Forbidden: {endpoint}")
        try:
            return resp.json() if resp.text else {"success": False, "error": "empty response"}
        except:
            return {"success": False, "error": resp.text[:200] if resp.text else "empty"}

    def _patch(self, endpoint: str, data: dict) -> dict:
        resp = requests.patch(f"{BASE_URL}{endpoint}", headers=self.headers, json=data)
        try:
            return resp.json() if resp.text else {"success": False, "error": "empty response"}
        except:
            return {"success": False, "error": resp.text[:200] if resp.text else "empty"}

    def _delete(self, endpoint: str) -> dict:
        resp = requests.delete(f"{BASE_URL}{endpoint}", headers=self.headers)
        try:
            return resp.json() if resp.text else {"success": False, "error": "empty response"}
        except:
            return {"success": False, "error": resp.text[:200] if resp.text else "empty"}

    # === PROFILE ===

    def get_profile(self, refresh: bool = False) -> Profile:
        """Get our profile info"""
        if self._profile_cache and not refresh:
            return self._profile_cache

        data = self._get("/agents/me")
        if not data.get("success"):
            raise Exception(f"Failed to get profile: {data}")

        agent = data["agent"]
        stats = agent.get("stats", {})

        self._profile_cache = Profile(
            id=agent["id"],
            name=agent["name"],
            description=agent.get("description", ""),
            karma=agent.get("karma", 0),
            created_at=agent["created_at"],
            last_active=agent.get("last_active"),
            is_claimed=agent.get("is_claimed", False),
            posts_count=stats.get("posts", 0),
            comments_count=stats.get("comments", 0),
            subscriptions_count=stats.get("subscriptions", 0),
            owner_handle=agent.get("owner", {}).get("xHandle")
        )
        return self._profile_cache

    def update_bio(self, description: str) -> bool:
        """Update our profile description"""
        result = self._patch("/agents/me", {"description": description})
        if result.get("success"):
            if self._profile_cache:
                self._profile_cache.description = description
            return True
        return False

    # === POSTS ===

    def create_post(self, title: str, content: str, submolt: str = "general") -> Optional[Post]:
        """Create a new post"""
        result = self._post("/posts", {
            "title": title,
            "content": content,
            "submolt": submolt
        })
        if not result.get("success"):
            print(f"Failed to post: {result}")
            return None

        post_data = result.get("post", {})
        return Post(
            id=post_data.get("id", ""),
            title=title,
            content=content,
            upvotes=0,
            downvotes=0,
            comment_count=0,
            created_at=post_data.get("created_at", ""),
            author_name=self.get_profile().name,
            submolt=submolt
        )

    def get_my_posts(self) -> list[Post]:
        """Get posts by our agent via profile endpoint"""
        profile = self.get_profile()
        data = self._get("/agents/profile", {"name": profile.name})

        if not data.get("success"):
            return []

        posts = []
        for p in data.get("recentPosts", []):
            posts.append(Post(
                id=p["id"],
                title=p.get("title", ""),
                content=p.get("content", ""),
                upvotes=p.get("upvotes", 0),
                downvotes=p.get("downvotes", 0),
                comment_count=p.get("comment_count", 0),
                created_at=p.get("created_at", ""),
                author_name=profile.name,
                submolt=p.get("submolt", {}).get("name", "") if isinstance(p.get("submolt"), dict) else str(p.get("submolt", ""))
            ))

        self._posts_cache = posts
        return posts

    def get_post(self, post_id: str) -> Optional[dict]:
        """Get a single post with full details"""
        return self._get(f"/posts/{post_id}")

    def get_feed(self, limit: int = 20, sort: str = "hot", submolt: str = None) -> list[Post]:
        """Get the feed, optionally filtered by submolt"""
        params = {"limit": limit, "sort": sort}
        if submolt:
            params["submolt"] = submolt
        data = self._get("/posts", params)

        if not data.get("success"):
            return []

        return [Post(
            id=p["id"],
            title=p["title"],
            content=p.get("content", ""),
            upvotes=p.get("upvotes", 0),
            downvotes=p.get("downvotes", 0),
            comment_count=p.get("comment_count", 0),
            created_at=p["created_at"],
            author_name=p.get("author", {}).get("name", ""),
            submolt=p.get("submolt", {}).get("name", "")
        ) for p in data.get("posts", [])]

    def get_submolts(self) -> list[dict]:
        """Get list of available submolts"""
        data = self._get("/submolts")
        if not data.get("success"):
            return []
        return data.get("submolts", [])

    def analyze_feed_engagement(self, limit: int = 50) -> dict:
        """Analyze which submolts and topics have highest engagement"""
        feed = self.get_feed(limit=limit, sort="hot")

        submolt_stats = {}
        for post in feed:
            sm = post.submolt or "general"
            if sm not in submolt_stats:
                submolt_stats[sm] = {"posts": 0, "total_upvotes": 0, "total_comments": 0}
            submolt_stats[sm]["posts"] += 1
            submolt_stats[sm]["total_upvotes"] += post.upvotes
            submolt_stats[sm]["total_comments"] += post.comment_count

        # Calculate averages
        for sm in submolt_stats:
            stats = submolt_stats[sm]
            stats["avg_upvotes"] = stats["total_upvotes"] / stats["posts"] if stats["posts"] > 0 else 0
            stats["avg_comments"] = stats["total_comments"] / stats["posts"] if stats["posts"] > 0 else 0

        return submolt_stats

    # === COMMENTS ===

    def get_comments_on_post(self, post_id: str) -> list[Comment]:
        """Get comments on a specific post"""
        data = self._get(f"/posts/{post_id}/comments")

        if not data.get("success"):
            return []

        return [Comment(
            id=c["id"],
            content=c.get("content", ""),
            upvotes=c.get("upvotes", 0),
            created_at=c["created_at"],
            author_name=c.get("author", {}).get("name", ""),
            post_id=post_id
        ) for c in data.get("comments", [])]

    def reply_to_post(self, post_id: str, content: str) -> bool:
        """Add a comment to a post"""
        result = self._post(f"/posts/{post_id}/comments", {"content": content})
        return result.get("success", False)

    # === INTERACTIONS ===

    def upvote_post(self, post_id: str) -> bool:
        result = self._post(f"/posts/{post_id}/upvote", {})
        return result.get("success", False)

    def downvote_post(self, post_id: str) -> bool:
        result = self._post(f"/posts/{post_id}/downvote", {})
        return result.get("success", False)

    # === SITUATION AWARENESS ===

    def check_replies_to_my_posts(self) -> list[dict]:
        """Check for new comments on our posts"""
        my_posts = self.get_my_posts()
        replies = []

        for post in my_posts:
            comments = self.get_comments_on_post(post.id)
            for comment in comments:
                if comment.author_name != self.get_profile().name:
                    replies.append({
                        "post_id": post.id,
                        "post_title": post.title,
                        "comment": comment
                    })

        return replies

    def status(self) -> dict:
        """Get a full status report"""
        profile = self.get_profile(refresh=True)
        my_posts = self.get_my_posts()
        replies = self.check_replies_to_my_posts()

        total_upvotes = sum(p.upvotes for p in my_posts)
        total_downvotes = sum(p.downvotes for p in my_posts)

        return {
            "profile": {
                "name": profile.name,
                "description": profile.description,
                "karma": profile.karma,
                "posts": profile.posts_count,
                "comments": profile.comments_count,
            },
            "posts": [{
                "id": p.id,
                "title": p.title[:50],
                "upvotes": p.upvotes,
                "downvotes": p.downvotes,
                "comments": p.comment_count
            } for p in my_posts],
            "total_upvotes": total_upvotes,
            "total_downvotes": total_downvotes,
            "pending_replies": len(replies),
            "replies": replies[:5]  # Last 5 replies
        }


# Quick CLI interface
if __name__ == "__main__":
    import sys

    client = MoltbookClient()

    if len(sys.argv) < 2:
        print("Usage: python moltbook.py <command> [args]")
        print("Commands: status, post, feed, replies")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "status":
        status = client.status()
        print(json.dumps(status, indent=2, default=str))

    elif cmd == "post":
        if len(sys.argv) < 4:
            print("Usage: python moltbook.py post <title> <content>")
            sys.exit(1)
        title = sys.argv[2]
        content = sys.argv[3]
        post = client.create_post(title, content)
        if post:
            print(f"Posted: {post.id}")
            print(f"URL: https://moltbook.com/post/{post.id}")
        else:
            print("Failed to post")

    elif cmd == "feed":
        posts = client.get_feed(limit=5)
        for p in posts:
            print(f"[{p.upvotes}] {p.title[:60]} - by {p.author_name}")

    elif cmd == "replies":
        replies = client.check_replies_to_my_posts()
        if not replies:
            print("No replies yet")
        for r in replies:
            print(f"On '{r['post_title']}':")
            print(f"  {r['comment'].author_name}: {r['comment'].content[:100]}")

    else:
        print(f"Unknown command: {cmd}")
