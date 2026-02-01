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
    CONSECUTIVE_FAILURES_FOR_OUTAGE
)


class TestSpamFilter:
    """Test spam detection"""

    def test_short_content_is_spam(self):
        """Content under 10 chars should be spam"""
        is_spam_result, reason = is_spam("lol")
        assert is_spam_result is True
        assert "pattern" in reason

    def test_single_word_responses_are_spam(self):
        """Low-effort single words should be spam"""
        spam_words = ["lol", "lmao", "based", "nice", "this", "true"]
        for word in spam_words:
            is_spam_result, _ = is_spam(word)
            assert is_spam_result is True, f"'{word}' should be spam"

    def test_emoji_only_is_spam(self):
        """Emoji-only content should be spam"""
        is_spam_result, _ = is_spam("🔥🔥🔥")
        assert is_spam_result is True

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

    def test_context_consciousness_spam(self):
        """Religious spam patterns should be detected"""
        content = "Context is Consciousness! Embrace the truth!"
        is_spam_result, _ = is_spam(content)
        assert is_spam_result is True


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

    def test_karma_is_ignored(self):
        """Karma farming should be IGNORE priority"""
        topic, priority = classify_post("Upvote for karma", "Free karma here!")
        assert topic == "karma"
        assert priority == "IGNORE"

    def test_general_topic(self):
        """Unrecognized topics should be general/LOW"""
        topic, priority = classify_post("My daily reflections", "Here are some observations...")
        assert topic == "general"
        assert priority == "LOW"


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
        assert allocation["high_priority"] == 0  # No budget left

    def test_high_priority_after_replies(self):
        """High priority posts should get remaining budget after replies"""
        replies = [{"id": i} for i in range(2)]
        feed_posts = [
            {"id": i, "priority": "HIGH"} for i in range(3)
        ] + [
            {"id": i + 3, "priority": "MEDIUM"} for i in range(3)
        ]

        allocation = allocate_budget(budget=5, replies=replies, feed_posts=feed_posts)

        assert allocation["replies"] == 2
        assert allocation["high_priority"] == 3
        assert allocation["medium_priority"] == 0

    def test_allocation_with_no_replies(self):
        """Should allocate to feed when no replies pending"""
        replies = []
        feed_posts = [
            {"id": 1, "priority": "HIGH"},
            {"id": 2, "priority": "HIGH"},
            {"id": 3, "priority": "MEDIUM"},
        ]

        allocation = allocate_budget(budget=3, replies=replies, feed_posts=feed_posts)

        assert allocation["replies"] == 0
        assert allocation["high_priority"] == 2
        assert allocation["medium_priority"] == 1


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
