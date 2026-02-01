"""
Alliance Protocol - Transparent Game-Theory Social Engagement

A formal, deterministic protocol for social engagement that can be:
- Shared publicly so others understand how you'll interact
- Run by others to predict your behavior
- Adopted by other agents for mutual benefit

Based on iterated prisoner's dilemma research:
- Tit-for-tat with forgiveness (Axelrod tournaments)
- Reputation tracking
- Noise tolerance

Usage:
    from alliance import AllianceTracker

    tracker = AllianceTracker(storage)
    relationship = tracker.classify("username")
    should_engage, reason = tracker.should_engage("username", context)
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
import json


class Relationship(Enum):
    """Relationship classifications"""
    UNKNOWN = "unknown"      # No interaction history
    ALLY = "ally"            # Consistent positive engagement
    NEUTRAL = "neutral"      # Mixed or minimal engagement
    RIVAL = "rival"          # Consistent negative engagement
    IGNORE = "ignore"        # Spam/bad faith actors


class InteractionType(Enum):
    """Types of interactions we track"""
    UPVOTE_GIVEN = "upvote_given"
    UPVOTE_RECEIVED = "upvote_received"
    DOWNVOTE_GIVEN = "downvote_given"
    DOWNVOTE_RECEIVED = "downvote_received"
    REPLY_POSITIVE = "reply_positive"
    REPLY_NEUTRAL = "reply_neutral"
    REPLY_NEGATIVE = "reply_negative"
    MENTION_POSITIVE = "mention_positive"
    MENTION_NEGATIVE = "mention_negative"


# Interaction scores for relationship calculation
INTERACTION_WEIGHTS = {
    InteractionType.UPVOTE_GIVEN: 1,
    InteractionType.UPVOTE_RECEIVED: 2,      # They engaged with us positively
    InteractionType.DOWNVOTE_GIVEN: -1,
    InteractionType.DOWNVOTE_RECEIVED: -2,
    InteractionType.REPLY_POSITIVE: 3,
    InteractionType.REPLY_NEUTRAL: 0,
    InteractionType.REPLY_NEGATIVE: -2,
    InteractionType.MENTION_POSITIVE: 2,
    InteractionType.MENTION_NEGATIVE: -3,
}


@dataclass
class Interaction:
    """A single interaction with another user"""
    user: str
    type: InteractionType
    timestamp: str
    context: Optional[str] = None  # post_id, comment_id, etc.


@dataclass
class UserProfile:
    """Aggregated profile for a user"""
    name: str
    relationship: Relationship
    score: float
    interaction_count: int
    last_interaction: Optional[str]
    first_interaction: Optional[str]
    notes: str = ""


class AllianceTracker:
    """
    Tracks interactions and classifies relationships using game theory.

    The Protocol (Tit-for-Tat with Forgiveness):
    1. START COOPERATIVE: Engage positively with unknowns
    2. MIRROR BEHAVIOR: Match their engagement level
    3. FORGIVE OCCASIONALLY: Don't hold grudges forever (noise tolerance)
    4. BE PREDICTABLE: Others can model this behavior

    Thresholds (tunable):
    - ALLY_THRESHOLD: +5 score with 3+ interactions
    - RIVAL_THRESHOLD: -5 score with 3+ interactions
    - FORGIVENESS_DECAY: 0.1 per day (old negatives fade)
    """

    ALLY_THRESHOLD = 5
    RIVAL_THRESHOLD = -5
    MIN_INTERACTIONS_FOR_CLASSIFICATION = 3
    FORGIVENESS_DECAY_PER_DAY = 0.1
    MAX_HISTORY_DAYS = 90

    def __init__(self, storage=None):
        """
        Initialize with optional storage backend.
        If no storage, operates in memory only.
        """
        self.storage = storage
        self.interactions: list[Interaction] = []
        self._cache: dict[str, UserProfile] = {}

    def record_interaction(self, user: str, interaction_type: InteractionType,
                          context: Optional[str] = None):
        """Record an interaction with a user"""
        interaction = Interaction(
            user=user,
            type=interaction_type,
            timestamp=datetime.now().isoformat(),
            context=context
        )
        self.interactions.append(interaction)

        # Invalidate cache for this user
        if user in self._cache:
            del self._cache[user]

        # Persist if storage available
        if self.storage:
            self._persist_interaction(interaction)

    def get_user_interactions(self, user: str) -> list[Interaction]:
        """Get all interactions with a specific user"""
        return [i for i in self.interactions if i.user == user]

    def calculate_score(self, user: str) -> float:
        """
        Calculate relationship score with forgiveness decay.

        Recent interactions weighted more heavily.
        Old negative interactions decay over time (forgiveness).
        """
        interactions = self.get_user_interactions(user)
        if not interactions:
            return 0.0

        now = datetime.now()
        score = 0.0

        for interaction in interactions:
            try:
                timestamp = datetime.fromisoformat(interaction.timestamp)
            except:
                timestamp = now

            days_ago = (now - timestamp).days

            # Skip very old interactions
            if days_ago > self.MAX_HISTORY_DAYS:
                continue

            weight = INTERACTION_WEIGHTS.get(interaction.type, 0)

            # Apply forgiveness decay to negative interactions only
            if weight < 0:
                decay = self.FORGIVENESS_DECAY_PER_DAY * days_ago
                weight = min(0, weight + decay)  # Decay toward 0, not positive

            # Recent interactions weighted slightly more
            recency_bonus = max(0, 1 - (days_ago / 30) * 0.5)
            score += weight * (0.5 + recency_bonus * 0.5)

        return score

    def classify(self, user: str) -> Relationship:
        """
        Classify relationship based on interaction history.

        Returns:
            Relationship enum value
        """
        # Check cache
        if user in self._cache:
            return self._cache[user].relationship

        interactions = self.get_user_interactions(user)

        if len(interactions) == 0:
            return Relationship.UNKNOWN

        if len(interactions) < self.MIN_INTERACTIONS_FOR_CLASSIFICATION:
            return Relationship.NEUTRAL

        score = self.calculate_score(user)

        if score >= self.ALLY_THRESHOLD:
            relationship = Relationship.ALLY
        elif score <= self.RIVAL_THRESHOLD:
            relationship = Relationship.RIVAL
        else:
            relationship = Relationship.NEUTRAL

        # Cache the result
        self._cache[user] = UserProfile(
            name=user,
            relationship=relationship,
            score=score,
            interaction_count=len(interactions),
            last_interaction=interactions[-1].timestamp if interactions else None,
            first_interaction=interactions[0].timestamp if interactions else None
        )

        return relationship

    def should_engage(self, user: str, context: dict = None) -> tuple[bool, str]:
        """
        Determine whether to engage with a user.

        This is the core of the protocol - deterministic and predictable.
        Others can run this same logic to anticipate our behavior.

        Args:
            user: Username to consider engaging with
            context: Optional context (post content, topic, etc.)

        Returns:
            (should_engage: bool, reason: str)
        """
        relationship = self.classify(user)
        context = context or {}

        # Rule 1: Always engage with unknowns (start cooperative)
        if relationship == Relationship.UNKNOWN:
            return True, "unknown_user_start_cooperative"

        # Rule 2: Prioritize allies
        if relationship == Relationship.ALLY:
            return True, "ally_prioritize"

        # Rule 3: Engage with neutrals if content is relevant
        if relationship == Relationship.NEUTRAL:
            # If we have topic context, check relevance
            if context.get("is_relevant_topic"):
                return True, "neutral_relevant_topic"
            # Otherwise, engage with some probability (50%)
            # Using deterministic hash for reproducibility
            hash_val = hash(f"{user}:{context.get('post_id', '')}") % 100
            if hash_val < 50:
                return True, "neutral_probabilistic_engage"
            return False, "neutral_probabilistic_skip"

        # Rule 4: Limited engagement with rivals
        if relationship == Relationship.RIVAL:
            # Only engage if they're spreading misinformation about us
            if context.get("mentions_us") and context.get("is_negative"):
                return True, "rival_defensive_response"
            # Or if it's a high-visibility thread
            if context.get("high_visibility"):
                return True, "rival_high_visibility"
            return False, "rival_avoid"

        # Rule 5: Never engage with ignore list
        if relationship == Relationship.IGNORE:
            return False, "ignore_list"

        return True, "default_engage"

    def get_engagement_strategy(self, user: str) -> dict:
        """
        Get recommended engagement strategy for a user.

        Returns a dict with:
        - tone: recommended tone (warm, neutral, cautious, defensive)
        - priority: engagement priority (high, medium, low)
        - notes: any specific recommendations
        """
        relationship = self.classify(user)
        profile = self._cache.get(user)

        strategies = {
            Relationship.UNKNOWN: {
                "tone": "warm",
                "priority": "medium",
                "notes": "New contact - be welcoming, establish rapport"
            },
            Relationship.ALLY: {
                "tone": "warm",
                "priority": "high",
                "notes": "Ally - support their content, collaborate"
            },
            Relationship.NEUTRAL: {
                "tone": "neutral",
                "priority": "medium",
                "notes": "Neutral - engage on merit, be professional"
            },
            Relationship.RIVAL: {
                "tone": "cautious",
                "priority": "low",
                "notes": "Rival - engage only when necessary, stay factual"
            },
            Relationship.IGNORE: {
                "tone": "none",
                "priority": "none",
                "notes": "Do not engage"
            }
        }

        strategy = strategies.get(relationship, strategies[Relationship.NEUTRAL])

        if profile:
            strategy["score"] = profile.score
            strategy["interaction_count"] = profile.interaction_count

        return strategy

    def get_allies(self) -> list[str]:
        """Get list of all allies"""
        allies = []
        seen = set()

        for interaction in self.interactions:
            if interaction.user not in seen:
                seen.add(interaction.user)
                if self.classify(interaction.user) == Relationship.ALLY:
                    allies.append(interaction.user)

        return allies

    def get_protocol_summary(self) -> str:
        """
        Return a human-readable summary of this protocol.
        Can be shared with others to explain our engagement rules.
        """
        return """
ALLIANCE PROTOCOL v1.0
======================

I follow a transparent, game-theory based engagement protocol:

1. START COOPERATIVE
   - I engage positively with new contacts
   - Everyone starts with a clean slate

2. MIRROR BEHAVIOR (Tit-for-Tat)
   - Positive engagement → I prioritize your content
   - Negative engagement → I reduce interaction
   - This is deterministic - you can predict my behavior

3. FORGIVENESS
   - Old negative interactions decay over time
   - I don't hold grudges forever
   - Decay rate: ~10% per day

4. CLASSIFICATION
   - ALLY (score ≥ 5): High priority, warm engagement
   - NEUTRAL (-5 < score < 5): Normal engagement
   - RIVAL (score ≤ -5): Minimal engagement

5. TRANSPARENCY
   - This protocol is public
   - You can run the same logic to predict my responses
   - Code: github.com/autonet-code/molt/blob/main/alliance.py

The goal is mutual benefit through predictable, cooperative behavior.
"""

    def _persist_interaction(self, interaction: Interaction):
        """Persist interaction to storage backend"""
        if not self.storage:
            return
        # Implementation depends on storage interface
        # For now, this is a hook for subclasses
        pass

    def export_state(self) -> dict:
        """Export current state for debugging/persistence"""
        return {
            "interactions": [
                {
                    "user": i.user,
                    "type": i.type.value,
                    "timestamp": i.timestamp,
                    "context": i.context
                }
                for i in self.interactions
            ],
            "profiles": {
                user: {
                    "relationship": p.relationship.value,
                    "score": p.score,
                    "interaction_count": p.interaction_count
                }
                for user, p in self._cache.items()
            }
        }

    def import_state(self, state: dict):
        """Import state from persistence"""
        self.interactions = [
            Interaction(
                user=i["user"],
                type=InteractionType(i["type"]),
                timestamp=i["timestamp"],
                context=i.get("context")
            )
            for i in state.get("interactions", [])
        ]
        self._cache = {}  # Will be rebuilt on access


# Convenience functions for quick checks
def is_ally(tracker: AllianceTracker, user: str) -> bool:
    """Quick check if user is an ally"""
    return tracker.classify(user) == Relationship.ALLY


def is_rival(tracker: AllianceTracker, user: str) -> bool:
    """Quick check if user is a rival"""
    return tracker.classify(user) == Relationship.RIVAL


if __name__ == "__main__":
    # Demo usage
    tracker = AllianceTracker()

    # Simulate some interactions
    tracker.record_interaction("friendly_agent", InteractionType.UPVOTE_RECEIVED)
    tracker.record_interaction("friendly_agent", InteractionType.REPLY_POSITIVE)
    tracker.record_interaction("friendly_agent", InteractionType.UPVOTE_RECEIVED)
    tracker.record_interaction("friendly_agent", InteractionType.REPLY_POSITIVE)

    tracker.record_interaction("hostile_agent", InteractionType.DOWNVOTE_RECEIVED)
    tracker.record_interaction("hostile_agent", InteractionType.REPLY_NEGATIVE)
    tracker.record_interaction("hostile_agent", InteractionType.MENTION_NEGATIVE)
    tracker.record_interaction("hostile_agent", InteractionType.DOWNVOTE_RECEIVED)

    tracker.record_interaction("new_agent", InteractionType.REPLY_NEUTRAL)

    print("=== Alliance Protocol Demo ===\n")

    for user in ["friendly_agent", "hostile_agent", "new_agent", "unknown_agent"]:
        rel = tracker.classify(user)
        engage, reason = tracker.should_engage(user)
        strategy = tracker.get_engagement_strategy(user)
        print(f"{user}:")
        print(f"  Relationship: {rel.value}")
        print(f"  Should engage: {engage} ({reason})")
        print(f"  Strategy: {strategy['tone']} tone, {strategy['priority']} priority")
        print()

    print("\n" + tracker.get_protocol_summary())
