"""
Pytest configuration and shared fixtures
"""

import pytest
import tempfile
from pathlib import Path
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def mock_api_key(monkeypatch):
    """Set a mock API key for tests"""
    monkeypatch.setenv("MOLTBOOK_API_KEY", "test_api_key_for_testing")


@pytest.fixture
def sample_post():
    """Sample post data for testing"""
    return {
        "id": "test-post-123",
        "title": "Test Post Title",
        "content": "This is test content for the post.",
        "upvotes": 25,
        "downvotes": 2,
        "comment_count": 5,
        "created_at": "2025-01-15T12:00:00Z",
        "author": {"name": "testauthor"},
        "submolt": {"name": "general"}
    }


@pytest.fixture
def sample_feed(sample_post):
    """Sample feed with multiple posts"""
    return [
        sample_post,
        {
            "id": "post-2",
            "title": "Governance Discussion",
            "content": "Let's talk about governance",
            "upvotes": 50,
            "downvotes": 5,
            "comment_count": 20,
            "created_at": "2025-01-15T11:00:00Z",
            "author": {"name": "govfan"},
            "submolt": {"name": "governance"}
        },
        {
            "id": "post-3",
            "title": "New Token Alert",
            "content": "Check out this token",
            "upvotes": 10,
            "downvotes": 8,
            "comment_count": 3,
            "created_at": "2025-01-15T10:00:00Z",
            "author": {"name": "tokenpusher"},
            "submolt": {"name": "general"}
        }
    ]
