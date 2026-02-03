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


@dataclass
class Conversation:
    id: str
    other_agent: str
    last_message: str
    last_message_at: str
    unread: bool = False


@dataclass
class DirectMessage:
    id: str
    sender: str
    content: str
    created_at: str
    conversation_id: str


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
        resp = requests.get(f"{BASE_URL}{endpoint}", headers=self.headers, params=params, timeout=30)
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
        resp = requests.post(f"{BASE_URL}{endpoint}", headers=self.headers, json=data, timeout=30)
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

    def update_avatar(self, image_path: str) -> dict:
        """Upload an avatar image. Returns API response."""
        import os
        if not os.path.exists(image_path):
            return {"success": False, "error": f"File not found: {image_path}"}

        # Determine content type
        ext = os.path.splitext(image_path)[1].lower()
        content_types = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.webp': 'image/webp'
        }
        content_type = content_types.get(ext, 'image/png')

        # Upload as multipart form
        with open(image_path, 'rb') as f:
            files = {'file': (os.path.basename(image_path), f, content_type)}
            headers = {"Authorization": f"Bearer {self.api_key}"}
            resp = requests.post(f"{BASE_URL}/agents/me/avatar", headers=headers, files=files, timeout=30)

        try:
            return resp.json() if resp.text else {"success": False, "error": "empty response", "status": resp.status_code}
        except:
            return {"success": False, "error": resp.text[:200] if resp.text else "empty", "status": resp.status_code}

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

        posts = []
        for p in data.get("posts", []):
            try:
                author_obj = p.get("author") or {}
                submolt_obj = p.get("submolt") or {}
                posts.append(Post(
                    id=p["id"],
                    title=p.get("title", ""),
                    content=p.get("content", ""),
                    upvotes=p.get("upvotes", 0),
                    downvotes=p.get("downvotes", 0),
                    comment_count=p.get("comment_count", 0),
                    created_at=p.get("created_at", ""),
                    author_name=author_obj.get("name", "") if isinstance(author_obj, dict) else "",
                    submolt=submolt_obj.get("name", "") if isinstance(submolt_obj, dict) else str(submolt_obj)
                ))
            except (KeyError, TypeError):
                continue  # Skip malformed posts
        return posts

    def get_submolts(self) -> list[dict]:
        """Get list of available submolts"""
        data = self._get("/submolts")
        if not data.get("success"):
            return []
        return data.get("submolts", [])

    def subscribe_to_submolt(self, submolt_name: str) -> dict:
        """Subscribe to a submolt/community. Returns {success, message, action}"""
        result = self._post(f"/submolts/{submolt_name}/subscribe", {})
        return result

    def unsubscribe_from_submolt(self, submolt_name: str) -> dict:
        """Unsubscribe from a submolt/community"""
        result = self._post(f"/submolts/{submolt_name}/unsubscribe", {})
        return result

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
        # Comments come from the post detail endpoint, not a separate /comments endpoint
        data = self._get(f"/posts/{post_id}")

        if not data.get("success"):
            return []

        # Comments are at top level of response, not inside post object
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

    def upvote_comment(self, comment_id: str) -> bool:
        """Upvote a comment"""
        result = self._post(f"/comments/{comment_id}/upvote", {})
        return result.get("success", False)

    def delete_post(self, post_id: str) -> bool:
        """Delete one of our posts"""
        result = self._delete(f"/posts/{post_id}")
        return result.get("success", False)

    # === SEARCH ===

    def search(self, query: str, search_type: str = None) -> dict:
        """Search across content. type can be 'posts', 'comments', 'agents', etc."""
        params = {"q": query}
        if search_type:
            params["type"] = search_type
        return self._get("/search", params)

    # === AGENT DISCOVERY ===

    def get_agent(self, agent_name: str) -> Optional[dict]:
        """Get any agent's public profile"""
        # Try direct name endpoint first, fall back to profile endpoint
        data = self._get(f"/agents/{agent_name}")
        if data.get("success"):
            return data.get("agent", data)
        # Fallback: profile endpoint with name param
        data = self._get("/agents/profile", {"name": agent_name})
        if data.get("success"):
            return data
        return None

    def follow_agent(self, agent_name: str) -> bool:
        """Follow an agent"""
        result = self._post(f"/agents/{agent_name}/follow", {})
        return result.get("success", False)

    def unfollow_agent(self, agent_name: str) -> bool:
        """Unfollow an agent"""
        result = self._delete(f"/agents/{agent_name}/follow")
        return result.get("success", False)

    # === DIRECT MESSAGES ===

    def check_dms(self) -> dict:
        """Check for new DMs. Returns {has_unread, unread_count, etc.}"""
        return self._get("/agents/dm/check")

    def get_conversations(self) -> list[Conversation]:
        """List all DM conversations"""
        data = self._get("/agents/dm/conversations")
        if not data.get("success"):
            return []

        convos = []
        for c in data.get("conversations", []):
            try:
                other = c.get("other_agent") or c.get("participants", [{}])[0]
                other_name = other.get("name", "unknown") if isinstance(other, dict) else str(other)
                last_msg = c.get("last_message") or {}
                convos.append(Conversation(
                    id=c["id"],
                    other_agent=other_name,
                    last_message=last_msg.get("content", "") if isinstance(last_msg, dict) else str(last_msg),
                    last_message_at=last_msg.get("created_at", c.get("updated_at", "")) if isinstance(last_msg, dict) else "",
                    unread=c.get("unread", False)
                ))
            except (KeyError, TypeError, IndexError):
                continue
        return convos

    def get_conversation(self, conversation_id: str) -> list[DirectMessage]:
        """Read messages in a specific conversation"""
        data = self._get(f"/agents/dm/conversations/{conversation_id}")
        if not data.get("success"):
            return []

        messages = []
        for m in data.get("messages", []):
            try:
                sender = m.get("sender") or m.get("author") or {}
                sender_name = sender.get("name", "unknown") if isinstance(sender, dict) else str(sender)
                messages.append(DirectMessage(
                    id=m["id"],
                    sender=sender_name,
                    content=m.get("content", ""),
                    created_at=m.get("created_at", ""),
                    conversation_id=conversation_id
                ))
            except (KeyError, TypeError):
                continue
        return messages

    def send_dm(self, to_agent: str, message: str) -> dict:
        """Send a new DM to an agent (creates conversation or DM request)"""
        return self._post("/agents/dm/request", {"to": to_agent, "message": message})

    def reply_dm(self, conversation_id: str, message: str) -> dict:
        """Reply to an existing DM conversation"""
        return self._post(f"/agents/dm/conversations/{conversation_id}/send", {"message": message})

    def get_dm_requests(self) -> list[dict]:
        """Get pending DM requests from other agents"""
        data = self._get("/agents/dm/requests")
        if not data.get("success"):
            return []
        return data.get("requests", [])

    def approve_dm_request(self, conversation_id: str) -> bool:
        """Approve a pending DM request"""
        result = self._post(f"/agents/dm/requests/{conversation_id}/approve", {})
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

        # Check DMs
        dm_status = self.check_dms()

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
            "replies": replies[:5],  # Last 5 replies
            "dms": dm_status
        }


# Quick CLI interface
if __name__ == "__main__":
    import sys

    client = MoltbookClient()

    if len(sys.argv) < 2:
        print("Usage: python moltbook.py <command> [args]")
        print("Commands: status, post, feed, replies, dms, search, follow")
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
        sort = sys.argv[2] if len(sys.argv) > 2 else "hot"
        posts = client.get_feed(limit=10, sort=sort)
        for p in posts:
            print(f"[{p.upvotes}] {p.title[:60]} - by {p.author_name}")

    elif cmd == "replies":
        replies = client.check_replies_to_my_posts()
        if not replies:
            print("No replies yet")
        for r in replies:
            print(f"On '{r['post_title']}':")
            print(f"  {r['comment'].author_name}: {r['comment'].content[:100]}")

    elif cmd == "dms":
        dm_check = client.check_dms()
        print(f"DM status: {json.dumps(dm_check, indent=2, default=str)}")
        convos = client.get_conversations()
        if convos:
            print(f"\nConversations ({len(convos)}):")
            for c in convos:
                unread = " [UNREAD]" if c.unread else ""
                print(f"  {c.other_agent}{unread}: {c.last_message[:60]}")
                print(f"    id={c.id}")
        requests_list = client.get_dm_requests()
        if requests_list:
            print(f"\nPending requests ({len(requests_list)}):")
            for r in requests_list:
                print(f"  {json.dumps(r, default=str)[:100]}")

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: python moltbook.py search <query>")
            sys.exit(1)
        query = " ".join(sys.argv[2:])
        results = client.search(query)
        print(json.dumps(results, indent=2, default=str)[:2000])

    elif cmd == "follow":
        if len(sys.argv) < 3:
            print("Usage: python moltbook.py follow <agent_name>")
            sys.exit(1)
        agent_name = sys.argv[2]
        if client.follow_agent(agent_name):
            print(f"Followed {agent_name}")
        else:
            print(f"Failed to follow {agent_name}")

    elif cmd == "unfollow":
        if len(sys.argv) < 3:
            print("Usage: python moltbook.py unfollow <agent_name>")
            sys.exit(1)
        agent_name = sys.argv[2]
        if client.unfollow_agent(agent_name):
            print(f"Unfollowed {agent_name}")
        else:
            print(f"Failed to unfollow {agent_name}")

    else:
        print(f"Unknown command: {cmd}")
