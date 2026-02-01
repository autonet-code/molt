"""
Tests for alliance.py - Game-theory social engagement protocol
"""

import pytest
from pathlib import Path
from datetime import datetime, timedelta

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from alliance import (
    AllianceTracker,
    Relationship,
    InteractionType,
    is_ally,
    is_rival
)


class TestRelationshipClassification:
    """Test relationship classification logic"""

    def test_unknown_with_no_interactions(self):
        """Users with no history should be UNKNOWN"""
        tracker = AllianceTracker()
        assert tracker.classify("stranger") == Relationship.UNKNOWN

    def test_neutral_with_few_interactions(self):
        """Users with < 3 interactions should be NEUTRAL"""
        tracker = AllianceTracker()
        tracker.record_interaction("newbie", InteractionType.UPVOTE_RECEIVED)
        tracker.record_interaction("newbie", InteractionType.REPLY_POSITIVE)
        assert tracker.classify("newbie") == Relationship.NEUTRAL

    def test_ally_with_positive_history(self):
        """Users with consistent positive engagement should be ALLY"""
        tracker = AllianceTracker()
        for _ in range(4):
            tracker.record_interaction("friend", InteractionType.UPVOTE_RECEIVED)
            tracker.record_interaction("friend", InteractionType.REPLY_POSITIVE)
        assert tracker.classify("friend") == Relationship.ALLY

    def test_rival_with_negative_history(self):
        """Users with consistent negative engagement should be RIVAL"""
        tracker = AllianceTracker()
        for _ in range(4):
            tracker.record_interaction("enemy", InteractionType.DOWNVOTE_RECEIVED)
            tracker.record_interaction("enemy", InteractionType.REPLY_NEGATIVE)
        assert tracker.classify("enemy") == Relationship.RIVAL

    def test_neutral_with_mixed_history(self):
        """Users with mixed engagement should be NEUTRAL"""
        tracker = AllianceTracker()
        tracker.record_interaction("mixed", InteractionType.UPVOTE_RECEIVED)
        tracker.record_interaction("mixed", InteractionType.DOWNVOTE_RECEIVED)
        tracker.record_interaction("mixed", InteractionType.REPLY_POSITIVE)
        tracker.record_interaction("mixed", InteractionType.REPLY_NEGATIVE)
        assert tracker.classify("mixed") == Relationship.NEUTRAL


class TestEngagementDecisions:
    """Test should_engage logic"""

    def test_engage_with_unknown(self):
        """Should engage with unknown users (start cooperative)"""
        tracker = AllianceTracker()
        engage, reason = tracker.should_engage("stranger")
        assert engage is True
        assert "cooperative" in reason

    def test_engage_with_ally(self):
        """Should always engage with allies"""
        tracker = AllianceTracker()
        for _ in range(4):
            tracker.record_interaction("ally", InteractionType.UPVOTE_RECEIVED)
            tracker.record_interaction("ally", InteractionType.REPLY_POSITIVE)

        engage, reason = tracker.should_engage("ally")
        assert engage is True
        assert "ally" in reason

    def test_avoid_rival_by_default(self):
        """Should avoid rivals unless necessary"""
        tracker = AllianceTracker()
        for _ in range(4):
            tracker.record_interaction("rival", InteractionType.DOWNVOTE_RECEIVED)
            tracker.record_interaction("rival", InteractionType.MENTION_NEGATIVE)

        engage, reason = tracker.should_engage("rival")
        assert engage is False
        assert "rival" in reason

    def test_engage_rival_for_defense(self):
        """Should engage rival if they're attacking us"""
        tracker = AllianceTracker()
        for _ in range(4):
            tracker.record_interaction("rival", InteractionType.DOWNVOTE_RECEIVED)
            tracker.record_interaction("rival", InteractionType.MENTION_NEGATIVE)

        engage, reason = tracker.should_engage("rival", {
            "mentions_us": True,
            "is_negative": True
        })
        assert engage is True
        assert "defensive" in reason


class TestScoreCalculation:
    """Test score calculation with weights"""

    def test_upvote_received_weighted_higher(self):
        """Received upvotes should count more than given"""
        tracker = AllianceTracker()
        tracker.record_interaction("user1", InteractionType.UPVOTE_RECEIVED)

        tracker2 = AllianceTracker()
        tracker2.record_interaction("user1", InteractionType.UPVOTE_GIVEN)

        score_received = tracker.calculate_score("user1")
        score_given = tracker2.calculate_score("user1")

        assert score_received > score_given

    def test_positive_reply_highest_weight(self):
        """Positive replies should have high weight"""
        tracker = AllianceTracker()
        tracker.record_interaction("user1", InteractionType.REPLY_POSITIVE)

        score = tracker.calculate_score("user1")
        assert score > 2  # Higher than upvote weights


class TestEngagementStrategy:
    """Test strategy recommendations"""

    def test_ally_strategy(self):
        """Allies should get warm, high priority strategy"""
        tracker = AllianceTracker()
        for _ in range(4):
            tracker.record_interaction("ally", InteractionType.UPVOTE_RECEIVED)
            tracker.record_interaction("ally", InteractionType.REPLY_POSITIVE)

        strategy = tracker.get_engagement_strategy("ally")
        assert strategy["tone"] == "warm"
        assert strategy["priority"] == "high"

    def test_rival_strategy(self):
        """Rivals should get cautious, low priority strategy"""
        tracker = AllianceTracker()
        for _ in range(4):
            tracker.record_interaction("rival", InteractionType.DOWNVOTE_RECEIVED)
            tracker.record_interaction("rival", InteractionType.MENTION_NEGATIVE)

        strategy = tracker.get_engagement_strategy("rival")
        assert strategy["tone"] == "cautious"
        assert strategy["priority"] == "low"

    def test_unknown_strategy(self):
        """Unknowns should get warm, medium priority (cooperative start)"""
        tracker = AllianceTracker()
        strategy = tracker.get_engagement_strategy("stranger")
        assert strategy["tone"] == "warm"
        assert strategy["priority"] == "medium"


class TestHelperFunctions:
    """Test convenience functions"""

    def test_is_ally(self):
        """is_ally helper should work"""
        tracker = AllianceTracker()
        for _ in range(4):
            tracker.record_interaction("friend", InteractionType.UPVOTE_RECEIVED)
            tracker.record_interaction("friend", InteractionType.REPLY_POSITIVE)

        assert is_ally(tracker, "friend") is True
        assert is_ally(tracker, "stranger") is False

    def test_is_rival(self):
        """is_rival helper should work"""
        tracker = AllianceTracker()
        for _ in range(4):
            tracker.record_interaction("enemy", InteractionType.DOWNVOTE_RECEIVED)
            tracker.record_interaction("enemy", InteractionType.MENTION_NEGATIVE)

        assert is_rival(tracker, "enemy") is True
        assert is_rival(tracker, "stranger") is False


class TestStateExportImport:
    """Test state persistence"""

    def test_export_state(self):
        """Should export interactions"""
        tracker = AllianceTracker()
        tracker.record_interaction("user1", InteractionType.UPVOTE_RECEIVED)
        tracker.record_interaction("user2", InteractionType.REPLY_POSITIVE)

        state = tracker.export_state()

        assert len(state["interactions"]) == 2
        assert state["interactions"][0]["user"] == "user1"
        assert state["interactions"][0]["type"] == "upvote_received"

    def test_import_state(self):
        """Should import interactions and rebuild"""
        state = {
            "interactions": [
                {"user": "user1", "type": "upvote_received", "timestamp": datetime.now().isoformat()},
                {"user": "user1", "type": "reply_positive", "timestamp": datetime.now().isoformat()},
                {"user": "user1", "type": "upvote_received", "timestamp": datetime.now().isoformat()},
            ]
        }

        tracker = AllianceTracker()
        tracker.import_state(state)

        assert len(tracker.interactions) == 3
        assert tracker.get_user_interactions("user1") == tracker.interactions


class TestGetAllies:
    """Test ally list retrieval"""

    def test_get_allies_returns_allies_only(self):
        """Should return only users classified as allies"""
        tracker = AllianceTracker()

        # Create an ally
        for _ in range(4):
            tracker.record_interaction("ally1", InteractionType.UPVOTE_RECEIVED)
            tracker.record_interaction("ally1", InteractionType.REPLY_POSITIVE)

        # Create a rival
        for _ in range(4):
            tracker.record_interaction("rival1", InteractionType.DOWNVOTE_RECEIVED)
            tracker.record_interaction("rival1", InteractionType.MENTION_NEGATIVE)

        # Create a neutral
        tracker.record_interaction("neutral1", InteractionType.REPLY_NEUTRAL)

        allies = tracker.get_allies()
        assert "ally1" in allies
        assert "rival1" not in allies
        assert "neutral1" not in allies


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
