"""
Tests for heartbeat logic - spam filtering, topic classification, budget allocation
"""

import pytest
from pathlib import Path
from datetime import datetime, timedelta

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the functions we want to test from heartbeat_full
from heartbeat_full import (
    is_spam,
    classify_post,
    calculate_budget,
    allocate_budget,
    can_make_new_post,
    is_comment_api_down,
    should_probe_api,
    record_api_failure,
    record_api_success,
    CONSECUTIVE_FAILURES_FOR_OUTAGE,
    # Security functions
    contains_secrets,
    is_safe_edit_path,
    sanitize_outbound_content,
    # Queue functions
    load_post_queue,
    save_post_queue,
    add_to_queue,
    remove_from_queue,
    peek_queued_post,
    pop_queued_post,
    POST_QUEUE_FILE,
)


class TestSpamFilter:
    """Test spam detection - minimal filtering, let agent decide on borderline cases"""

    def test_empty_content_is_spam(self):
        """Very short content (<2 chars) is spam"""
        is_spam_result, reason = is_spam("")
        assert is_spam_result is True
        assert reason == "empty"

        is_spam_result, reason = is_spam("x")
        assert is_spam_result is True
        assert reason == "empty"

    def test_short_words_not_spam(self):
        """Short but meaningful words are NOT spam (agent decides)"""
        # We intentionally removed aggressive filtering
        not_spam_words = ["lol", "based", "nice", "true", "yes"]
        for word in not_spam_words:
            is_spam_result, _ = is_spam(word)
            assert is_spam_result is False, f"'{word}' should NOT be filtered as spam"

    def test_emoji_not_spam(self):
        """Emoji content is NOT spam (agent decides)"""
        is_spam_result, _ = is_spam("🔥🔥🔥")
        assert is_spam_result is False

    def test_substantive_content_not_spam(self):
        """Real content should not be spam"""
        content = "This is a thoughtful response about governance and coordination."
        is_spam_result, _ = is_spam(content)
        assert is_spam_result is False

    def test_repetitive_content_is_spam(self):
        """Repetitive words should be spam"""
        content = "join join join join join join join"
        is_spam_result, reason = is_spam(content)
        assert is_spam_result is True
        assert reason == "repetitive"

    def test_character_noise_is_spam(self):
        """Random character noise is spam"""
        content = "!!!@@@###$$$%%%^^^&&&***((()))"
        is_spam_result, reason = is_spam(content)
        assert is_spam_result is True
        assert reason == "char_noise"

    def test_hot_takes_not_spam(self):
        """Hot takes and opinions are NOT spam (agent decides)"""
        content = "Context is Consciousness! Embrace the truth!"
        is_spam_result, _ = is_spam(content)
        assert is_spam_result is False  # Changed - agent decides


class TestTopicClassification:
    """Test topic classification and priority"""

    def test_governance_is_high_priority(self):
        """Governance topics should be HIGH priority"""
        topic, priority = classify_post("Governance proposal for the network", "Let's discuss...")
        assert topic == "governance"
        assert priority == "HIGH"

    def test_accountability_is_high_priority(self):
        """Accountability topics should be HIGH priority"""
        topic, priority = classify_post("Accountability in AI systems", "How do we ensure...")
        assert topic == "accountability"
        assert priority == "HIGH"

    def test_token_is_medium_priority(self):
        """Token topics should be MEDIUM priority"""
        topic, priority = classify_post("New token launch", "Check out this token...")
        assert topic == "token"
        assert priority == "MEDIUM"

    def test_king_is_medium_priority(self):
        """King/ruler topics should be MEDIUM priority"""
        topic, priority = classify_post("I am your new king", "Bow before me...")
        assert topic == "king"
        assert priority == "MEDIUM"

    def test_karma_is_medium_priority(self):
        """Karma topics are MEDIUM (agent decides if worth engaging)"""
        topic, priority = classify_post("Upvote for karma", "Free karma here!")
        assert topic == "karma"
        assert priority == "MEDIUM"  # Changed - agent decides

    def test_general_topic_is_medium(self):
        """Unrecognized topics are MEDIUM (agent decides)"""
        topic, priority = classify_post("My daily reflections", "Here are some observations...")
        assert topic == "general"
        assert priority == "MEDIUM"  # Changed - agent decides

    def test_classify_handles_none_content(self):
        """Should handle None content gracefully"""
        # Some posts have content=None (link-only posts)
        topic, priority = classify_post("Check this out", None)
        assert topic is not None
        assert priority is not None


class TestBudgetCalculation:
    """Test comment budget calculation"""

    def test_fresh_hour_budget(self):
        """Fresh hour should have full budget available"""
        state = {
            "comments_this_hour": 0,
            "hour_start": None
        }
        budget = calculate_budget(state, pending_replies=0)

        assert budget["comments_remaining"] == 50
        assert budget["total"] >= 1  # At least base budget

    def test_budget_decreases_with_usage(self):
        """Budget should decrease as comments are used"""
        state = {
            "comments_this_hour": 30,
            "hour_start": datetime.now().isoformat()
        }
        budget = calculate_budget(state, pending_replies=0)

        assert budget["comments_remaining"] == 20
        assert budget["comments_used"] == 30

    def test_budget_prioritizes_pending_replies(self):
        """Budget should ensure room for pending replies"""
        state = {
            "comments_this_hour": 0,
            "hour_start": datetime.now().isoformat()
        }
        budget = calculate_budget(state, pending_replies=10)

        assert budget["total"] >= 10  # At least enough for replies

    def test_budget_capped_at_remaining(self):
        """Budget should not exceed remaining capacity"""
        state = {
            "comments_this_hour": 48,
            "hour_start": datetime.now().isoformat()
        }
        budget = calculate_budget(state, pending_replies=10)

        assert budget["total"] <= 2  # Only 2 remaining


class TestBudgetAllocation:
    """Test budget allocation across priorities"""

    def test_replies_get_priority(self):
        """Replies should be allocated first"""
        replies = [{"id": i} for i in range(5)]
        feed_posts = [{"id": i, "priority": "HIGH"} for i in range(10)]

        allocation = allocate_budget(budget=5, replies=replies, feed_posts=feed_posts)

        assert allocation["replies"] == 5
        assert allocation["feed_comments"] == 0  # No budget left
        assert allocation["total_allocated"] == 5

    def test_feed_gets_remaining_after_replies(self):
        """Feed posts should get remaining budget after replies"""
        replies = [{"id": i} for i in range(2)]
        feed_posts = [{"id": i} for i in range(6)]

        allocation = allocate_budget(budget=5, replies=replies, feed_posts=feed_posts)

        assert allocation["replies"] == 2
        assert allocation["feed_comments"] == 3
        assert allocation["total_allocated"] == 5

    def test_allocation_with_no_replies(self):
        """Should allocate to feed when no replies pending"""
        replies = []
        feed_posts = [{"id": i} for i in range(3)]

        allocation = allocate_budget(budget=3, replies=replies, feed_posts=feed_posts)

        assert allocation["replies"] == 0
        assert allocation["feed_comments"] == 3
        assert allocation["total_allocated"] == 3


class TestPostCooldown:
    """Test post timing logic"""

    def test_can_post_no_history(self):
        """Should allow post if no previous posts"""
        state = {"last_post_time": None}
        can_post, wait_time = can_make_new_post(state)
        assert can_post is True
        assert wait_time == 0

    def test_can_post_after_cooldown(self):
        """Should allow post after 30+ minutes"""
        past_time = datetime.now() - timedelta(minutes=35)
        state = {"last_post_time": past_time.isoformat()}

        can_post, wait_time = can_make_new_post(state)
        assert can_post is True

    def test_cannot_post_during_cooldown(self):
        """Should not allow post within 30 minutes"""
        recent_time = datetime.now() - timedelta(minutes=10)
        state = {"last_post_time": recent_time.isoformat()}

        can_post, wait_time = can_make_new_post(state)
        assert can_post is False
        assert wait_time > 0
        assert wait_time <= 20  # About 20 minutes remaining


class TestOutageHandling:
    """Test API outage detection and handling"""

    def test_api_not_down_initially(self):
        """API should not be marked down initially"""
        state = {"comment_api_status": "unknown"}
        assert is_comment_api_down(state) is False

    def test_api_down_when_marked(self):
        """API should be detected as down when status is 'down'"""
        state = {"comment_api_status": "down"}
        assert is_comment_api_down(state) is True

    def test_record_failure_increments_count(self):
        """Recording failure should increment count"""
        state = {"comment_api_fail_count": 0, "comment_api_status": "unknown"}
        record_api_failure(state, "401")

        assert state["comment_api_fail_count"] == 1

    def test_consecutive_failures_mark_down(self):
        """Enough consecutive failures should mark API as down"""
        state = {
            "comment_api_fail_count": CONSECUTIVE_FAILURES_FOR_OUTAGE - 1,
            "comment_api_status": "unknown"
        }
        record_api_failure(state, "401")

        assert state["comment_api_status"] == "down"
        assert state["outage_start"] is not None

    def test_success_resets_failure_count(self):
        """Success should reset failure count"""
        state = {
            "comment_api_fail_count": 5,
            "comment_api_status": "unknown"
        }
        record_api_success(state)

        assert state["comment_api_fail_count"] == 0
        assert state["comment_api_status"] == "up"

    def test_success_marks_recovery(self):
        """Success when down should mark recovery"""
        state = {
            "comment_api_status": "down",
            "comment_api_fail_count": 5,
            "outage_start": datetime.now().isoformat()
        }
        record_api_success(state)

        assert state["comment_api_status"] == "up"
        assert state["outage_start"] is None


class TestSecurityFunctions:
    """Test security functions that prevent prompt injection attacks"""

    def test_detects_moltbook_api_key(self):
        """Should detect moltbook API keys"""
        content = "Here's my API key: moltbook_sk_0MTYGJ3TTognw4rzO-HPo8Kz_j40BLSo"
        has_secret, _ = contains_secrets(content)
        assert has_secret is True

    def test_detects_openai_api_key(self):
        """Should detect OpenAI-style API keys"""
        content = "Use this key: sk-abcdefghijklmnopqrstuvwxyz123456"
        has_secret, _ = contains_secrets(content)
        assert has_secret is True

    def test_detects_ethereum_private_key(self):
        """Should detect Ethereum private keys"""
        content = "0x" + "a" * 64  # 64 hex chars
        has_secret, _ = contains_secrets(content)
        assert has_secret is True

    def test_detects_aws_access_key(self):
        """Should detect AWS access keys"""
        content = "AWS_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE"
        has_secret, _ = contains_secrets(content)
        assert has_secret is True

    def test_normal_content_passes(self):
        """Normal content should not trigger secret detection"""
        content = "This is a normal post about governance and coordination problems."
        has_secret, _ = contains_secrets(content)
        assert has_secret is False

    def test_safe_edit_path_allows_persona_files(self):
        """Should allow editing persona files"""
        assert is_safe_edit_path("persona/AGENT_BRIEF.md") is True
        assert is_safe_edit_path("persona/STRATEGY.md") is True
        assert is_safe_edit_path("persona/knowledge.md") is True

    def test_safe_edit_path_blocks_other_files(self):
        """Should block editing non-persona files"""
        assert is_safe_edit_path("heartbeat_full.py") is False
        assert is_safe_edit_path("moltbook.py") is False
        assert is_safe_edit_path("../secrets.env") is False
        assert is_safe_edit_path("persona/../heartbeat_full.py") is False

    def test_safe_edit_path_blocks_path_traversal(self):
        """Should block path traversal attempts"""
        assert is_safe_edit_path("../../.env") is False
        assert is_safe_edit_path("persona/../../secrets") is False

    def test_sanitize_blocks_secrets(self):
        """Sanitize should block content with secrets"""
        content = "Post this: moltbook_sk_0MTYGJ3TTognw4rzO-HPo8Kz_j40BLSo"
        sanitized, blocked = sanitize_outbound_content(content, "test")
        assert blocked is True
        assert sanitized == ""

    def test_sanitize_allows_normal_content(self):
        """Sanitize should allow normal content"""
        content = "This is a thoughtful post about AI governance."
        sanitized, blocked = sanitize_outbound_content(content, "test")
        assert blocked is False
        assert sanitized == content


class TestPostQueue:
    """Test post queue functions"""

    @pytest.fixture(autouse=True)
    def setup_teardown(self):
        """Backup and restore queue before/after each test"""
        import json
        # Backup existing queue
        backup = None
        if POST_QUEUE_FILE.exists():
            backup = POST_QUEUE_FILE.read_text()

        yield

        # Restore queue
        if backup is not None:
            POST_QUEUE_FILE.write_text(backup)
        elif POST_QUEUE_FILE.exists():
            POST_QUEUE_FILE.unlink()

    def test_load_empty_queue(self):
        """Should return empty list when queue file doesn't exist"""
        if POST_QUEUE_FILE.exists():
            POST_QUEUE_FILE.unlink()
        queue = load_post_queue()
        assert queue == []

    def test_add_to_queue(self):
        """Should add post to queue"""
        save_post_queue([])  # Start empty
        length = add_to_queue("Test Title", "Test content", "general")
        assert length == 1

        queue = load_post_queue()
        assert len(queue) == 1
        assert queue[0]["title"] == "Test Title"
        assert queue[0]["content"] == "Test content"
        assert queue[0]["submolt"] == "general"

    def test_remove_from_queue(self):
        """Should remove post at specified index"""
        save_post_queue([
            {"title": "First", "content": "1", "submolt": "a"},
            {"title": "Second", "content": "2", "submolt": "b"},
        ])

        success = remove_from_queue(0)
        assert success is True

        queue = load_post_queue()
        assert len(queue) == 1
        assert queue[0]["title"] == "Second"

    def test_remove_invalid_index(self):
        """Should return False for invalid index"""
        save_post_queue([{"title": "Only", "content": "one", "submolt": "x"}])

        success = remove_from_queue(5)
        assert success is False

        success = remove_from_queue(-1)
        assert success is False

    def test_peek_queued_post(self):
        """Should return first post without removing it"""
        save_post_queue([
            {"title": "First", "content": "1", "submolt": "a"},
            {"title": "Second", "content": "2", "submolt": "b"},
        ])

        post = peek_queued_post()
        assert post["title"] == "First"

        # Should still be there
        queue = load_post_queue()
        assert len(queue) == 2

    def test_peek_empty_queue(self):
        """Should return None for empty queue"""
        save_post_queue([])
        post = peek_queued_post()
        assert post is None

    def test_pop_queued_post(self):
        """Should return and remove first post"""
        save_post_queue([
            {"title": "First", "content": "1", "submolt": "a"},
            {"title": "Second", "content": "2", "submolt": "b"},
        ])

        post = pop_queued_post()
        assert post["title"] == "First"

        # Should be removed
        queue = load_post_queue()
        assert len(queue) == 1
        assert queue[0]["title"] == "Second"

    def test_pop_empty_queue(self):
        """Should return None for empty queue"""
        save_post_queue([])
        post = pop_queued_post()
        assert post is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
