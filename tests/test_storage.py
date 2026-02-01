"""
Tests for storage.py - SQLite persistence layer
"""

import pytest
import tempfile
from pathlib import Path
from datetime import datetime

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from storage import Storage, OurPost, PendingReply, TrackedUser


@pytest.fixture
def temp_db():
    """Create a temporary database for testing"""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    storage = Storage(db_path)
    yield storage
    storage.conn.close()
    db_path.unlink()


class TestStorage:
    """Test Storage class initialization and schema"""

    def test_init_creates_tables(self, temp_db):
        """Storage should create all required tables on init"""
        cursor = temp_db.conn.cursor()

        # Check tables exist
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}

        assert 'posts' in tables
        assert 'users' in tables
        assert 'replies' in tables


class TestPostStorage:
    """Test post CRUD operations"""

    def test_save_and_get_post(self, temp_db):
        """Should save and retrieve a post"""
        post = OurPost(
            id="test-123",
            title="Test Post",
            content="Test content",
            submolt="general",
            created_at=datetime.now().isoformat(),
            upvotes=10,
            downvotes=2,
            comment_count=5,
            last_checked=datetime.now().isoformat()
        )

        temp_db.save_post(post)

        posts = temp_db.get_all_posts()
        assert len(posts) == 1
        assert posts[0].id == "test-123"
        assert posts[0].title == "Test Post"
        assert posts[0].upvotes == 10

    def test_get_all_posts_empty(self, temp_db):
        """Should return empty list when no posts"""
        posts = temp_db.get_all_posts()
        assert posts == []

    def test_update_post(self, temp_db):
        """Should update existing post stats"""
        post = OurPost(
            id="test-456",
            title="Original Title",
            content="Content",
            submolt="general",
            created_at=datetime.now().isoformat(),
            upvotes=5
        )
        temp_db.save_post(post)

        # Update with new stats
        post.upvotes = 50
        post.comment_count = 10
        temp_db.save_post(post)

        posts = temp_db.get_all_posts()
        assert len(posts) == 1
        assert posts[0].upvotes == 50
        assert posts[0].comment_count == 10


class TestUserStorage:
    """Test user tracking operations"""

    def test_save_and_get_user(self, temp_db):
        """Should save and retrieve a user"""
        user = TrackedUser(
            id="user-123",
            name="testuser",
            relationship="ally",
            notes="Good engagement"
        )

        temp_db.save_user(user)

        retrieved = temp_db.get_user("testuser")
        assert retrieved is not None
        assert retrieved.id == "user-123"
        assert retrieved.relationship == "ally"
        assert retrieved.notes == "Good engagement"

    def test_get_nonexistent_user(self, temp_db):
        """Should return None for unknown user"""
        user = temp_db.get_user("nobody")
        assert user is None

    def test_update_user_relationship(self, temp_db):
        """Should update user relationship"""
        user = TrackedUser(
            id="user-789",
            name="flipflopper",
            relationship="neutral"
        )
        temp_db.save_user(user)

        # Update relationship
        user.relationship = "rival"
        temp_db.save_user(user)

        retrieved = temp_db.get_user("flipflopper")
        assert retrieved.relationship == "rival"


class TestReplyStorage:
    """Test pending reply operations"""

    def test_save_and_get_reply(self, temp_db):
        """Should save and retrieve a pending reply"""
        reply = PendingReply(
            id="reply-123",
            post_id="post-456",
            post_title="Some Post",
            author_name="replier",
            content="Interesting take!",
            created_at=datetime.now().isoformat(),
            responded=False
        )

        temp_db.save_reply(reply)

        pending = temp_db.get_pending_replies()
        assert len(pending) == 1
        assert pending[0].id == "reply-123"
        assert pending[0].author_name == "replier"

    def test_get_pending_replies_excludes_responded(self, temp_db):
        """Should only return unresponded replies"""
        # Save one pending, one responded
        pending_reply = PendingReply(
            id="reply-1",
            post_id="post-1",
            post_title="Post 1",
            author_name="user1",
            content="Pending",
            created_at=datetime.now().isoformat(),
            responded=False
        )
        temp_db.save_reply(pending_reply)

        responded_reply = PendingReply(
            id="reply-2",
            post_id="post-1",
            post_title="Post 1",
            author_name="user2",
            content="Responded",
            created_at=datetime.now().isoformat(),
            responded=True,
            response="Thanks!"
        )
        temp_db.save_reply(responded_reply)

        pending = temp_db.get_pending_replies()
        assert len(pending) == 1
        assert pending[0].id == "reply-1"

    def test_mark_reply_responded(self, temp_db):
        """Should mark reply as responded"""
        reply = PendingReply(
            id="reply-999",
            post_id="post-1",
            post_title="Post",
            author_name="someone",
            content="Hello",
            created_at=datetime.now().isoformat(),
            responded=False
        )
        temp_db.save_reply(reply)

        temp_db.mark_reply_responded("reply-999", "Hello back!")

        pending = temp_db.get_pending_replies()
        assert len(pending) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
