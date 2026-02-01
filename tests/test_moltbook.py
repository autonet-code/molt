"""
Tests for moltbook.py - Moltbook API client
"""

import pytest
from unittest.mock import Mock, patch
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from moltbook import MoltbookClient, Post, Comment, Profile


class TestMoltbookClientInit:
    """Test client initialization"""

    def test_init_without_api_key_raises(self):
        """Should raise error if no API key provided"""
        with pytest.raises(ValueError) as exc_info:
            MoltbookClient(api_key="")
        assert "MOLTBOOK_API_KEY" in str(exc_info.value)

    def test_init_with_api_key(self):
        """Should initialize with valid API key"""
        client = MoltbookClient(api_key="test_key_123")
        assert client.api_key == "test_key_123"
        assert "Bearer test_key_123" in client.headers["Authorization"]


class TestMoltbookClientProfile:
    """Test profile operations"""

    @patch('moltbook.requests.get')
    def test_get_profile(self, mock_get):
        """Should fetch and parse profile"""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "success": True,
            "agent": {
                "id": "agent-123",
                "name": "testbot",
                "description": "A test bot",
                "karma": 100,
                "follower_count": 50,
                "following_count": 25,
                "created_at": "2025-01-01T00:00:00Z",
                "stats": {
                    "posts": 10,
                    "comments": 30,
                    "subscriptions": 5
                }
            }
        }

        client = MoltbookClient(api_key="test_key")
        profile = client.get_profile()

        assert profile.name == "testbot"
        assert profile.karma == 100
        assert profile.posts_count == 10


class TestMoltbookClientPosts:
    """Test post operations"""

    @patch('moltbook.requests.get')
    def test_get_feed(self, mock_get):
        """Should fetch and parse feed"""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "success": True,
            "posts": [
                {
                    "id": "post-1",
                    "title": "First Post",
                    "content": "Hello world",
                    "upvotes": 42,
                    "downvotes": 3,
                    "comment_count": 7,
                    "created_at": "2025-01-01T00:00:00Z",
                    "author": {"name": "author1"},
                    "submolt": {"name": "general"}
                },
                {
                    "id": "post-2",
                    "title": "Second Post",
                    "content": "Another post",
                    "upvotes": 15,
                    "downvotes": 1,
                    "comment_count": 2,
                    "created_at": "2025-01-01T01:00:00Z",
                    "author": {"name": "author2"},
                    "submolt": {"name": "governance"}
                }
            ]
        }

        client = MoltbookClient(api_key="test_key")
        feed = client.get_feed(limit=10)

        assert len(feed) == 2
        assert feed[0].title == "First Post"
        assert feed[0].upvotes == 42
        assert feed[0].submolt == "general"
        assert feed[1].submolt == "governance"

    @patch('moltbook.requests.get')
    @patch('moltbook.requests.post')
    def test_create_post(self, mock_post, mock_get):
        """Should create a post and return it"""
        # Mock the profile fetch that happens inside create_post
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "success": True,
            "agent": {
                "id": "agent-123",
                "name": "testbot",
                "description": "Test",
                "karma": 100,
                "follower_count": 0,
                "following_count": 0,
                "post_count": 5,
                "comments_count": 10,
                "created_at": "2025-01-01T00:00:00Z"
            }
        }

        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "success": True,
            "post": {
                "id": "new-post-123",
                "title": "My New Post",
                "content": "Content here",
                "created_at": "2025-01-15T12:00:00Z",
                "submolt": "general"
            }
        }

        client = MoltbookClient(api_key="test_key")
        post = client.create_post("My New Post", "Content here", submolt="general")

        assert post is not None
        assert post.id == "new-post-123"
        assert post.title == "My New Post"

    @patch('moltbook.requests.post')
    def test_create_post_failure(self, mock_post):
        """Should return None on failure"""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "success": False,
            "error": "Rate limited"
        }

        client = MoltbookClient(api_key="test_key")
        post = client.create_post("Title", "Content")

        assert post is None


class TestMoltbookClientComments:
    """Test comment operations"""

    @patch('moltbook.requests.get')
    def test_get_comments_on_post(self, mock_get):
        """Should fetch comments on a post"""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "success": True,
            "comments": [
                {
                    "id": "comment-1",
                    "content": "Great post!",
                    "upvotes": 5,
                    "created_at": "2025-01-01T02:00:00Z",
                    "author": {"name": "commenter1"}
                }
            ]
        }

        client = MoltbookClient(api_key="test_key")
        comments = client.get_comments_on_post("post-123")

        assert len(comments) == 1
        assert comments[0].content == "Great post!"
        assert comments[0].author_name == "commenter1"

    @patch('moltbook.requests.get')
    def test_get_comments_401_raises(self, mock_get):
        """Should raise exception on 401 (for outage detection)"""
        mock_get.return_value.status_code = 401

        client = MoltbookClient(api_key="test_key")

        with pytest.raises(Exception) as exc_info:
            client.get_comments_on_post("post-123")
        assert "401" in str(exc_info.value)


class TestMoltbookClientAnalytics:
    """Test analytics functions"""

    @patch('moltbook.requests.get')
    def test_analyze_feed_engagement(self, mock_get):
        """Should analyze submolt engagement from feed"""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "success": True,
            "posts": [
                {"id": "1", "title": "T1", "content": "", "upvotes": 10, "downvotes": 0,
                 "comment_count": 5, "created_at": "2025-01-01", "author": {"name": "a"},
                 "submolt": {"name": "governance"}},
                {"id": "2", "title": "T2", "content": "", "upvotes": 20, "downvotes": 0,
                 "comment_count": 10, "created_at": "2025-01-01", "author": {"name": "b"},
                 "submolt": {"name": "governance"}},
                {"id": "3", "title": "T3", "content": "", "upvotes": 5, "downvotes": 0,
                 "comment_count": 2, "created_at": "2025-01-01", "author": {"name": "c"},
                 "submolt": {"name": "general"}},
            ]
        }

        client = MoltbookClient(api_key="test_key")
        stats = client.analyze_feed_engagement(limit=50)

        assert "governance" in stats
        assert "general" in stats
        assert stats["governance"]["posts"] == 2
        assert stats["governance"]["avg_upvotes"] == 15.0
        assert stats["general"]["posts"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
