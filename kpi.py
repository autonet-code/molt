"""
KPI Tracking for Moltbook Presence

Tracks progress toward goals defined in STRATEGY.md
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

from storage import get_storage
from moltbook import MoltbookClient

KPI_FILE = Path(__file__).parent / "kpi_history.json"


@dataclass
class KPISnapshot:
    timestamp: str

    # Reach
    karma: int
    follower_count: int

    # Content
    total_posts: int
    total_comments: int
    avg_upvotes_per_post: float

    # Engagement
    total_replies_received: int
    reply_rate: float  # replies per post

    # Network
    allies_count: int
    rivals_count: int
    total_users_tracked: int

    # Conversion (manual tracking)
    repo_mentions: int = 0
    app_mentions: int = 0


def load_kpi_history() -> list[dict]:
    if KPI_FILE.exists():
        return json.loads(KPI_FILE.read_text())
    return []


def save_kpi_history(history: list[dict]):
    KPI_FILE.write_text(json.dumps(history, indent=2))


def capture_snapshot() -> KPISnapshot:
    """Capture current KPI snapshot from API and storage"""
    client = MoltbookClient()
    storage = get_storage()

    # Get profile from API
    profile = client.get_profile(refresh=True)

    # Get posts from storage
    posts = storage.get_all_posts()

    # Calculate averages
    total_upvotes = sum(p.upvotes for p in posts)
    avg_upvotes = total_upvotes / len(posts) if posts else 0

    # Get reply stats
    cursor = storage.conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM replies")
    total_replies = cursor.fetchone()[0]

    reply_rate = total_replies / len(posts) if posts else 0

    # Get user network stats
    cursor.execute("SELECT relationship, COUNT(*) FROM users GROUP BY relationship")
    relationships = dict(cursor.fetchall())

    snapshot = KPISnapshot(
        timestamp=datetime.now().isoformat(),
        karma=profile.karma,
        follower_count=getattr(profile, 'follower_count', 0),
        total_posts=len(posts),
        total_comments=profile.comments_count,
        avg_upvotes_per_post=round(avg_upvotes, 1),
        total_replies_received=total_replies,
        reply_rate=round(reply_rate, 2),
        allies_count=relationships.get('ally', 0),
        rivals_count=relationships.get('rival', 0),
        total_users_tracked=sum(relationships.values())
    )

    return snapshot


def record_snapshot():
    """Capture and save a KPI snapshot"""
    snapshot = capture_snapshot()
    history = load_kpi_history()
    history.append(asdict(snapshot))
    save_kpi_history(history)
    return snapshot


def get_progress_report() -> str:
    """Generate a progress report"""
    snapshot = capture_snapshot()
    history = load_kpi_history()

    report = []
    report.append("=" * 50)
    report.append("MOLTBOOK KPI REPORT")
    report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    report.append("=" * 50)

    # Current stats
    report.append("\n## Current Stats\n")
    report.append(f"Karma:           {snapshot.karma}")
    report.append(f"Posts:           {snapshot.total_posts}")
    report.append(f"Comments:        {snapshot.total_comments}")
    report.append(f"Avg upvotes:     {snapshot.avg_upvotes_per_post}")
    report.append(f"Replies received: {snapshot.total_replies_received}")
    report.append(f"Reply rate:      {snapshot.reply_rate} per post")

    # Network
    report.append("\n## Network\n")
    report.append(f"Allies:          {snapshot.allies_count}")
    report.append(f"Rivals:          {snapshot.rivals_count}")
    report.append(f"Total tracked:   {snapshot.total_users_tracked}")

    # Goals progress
    report.append("\n## Goals Progress\n")

    # Short-term (Week 1-2)
    report.append("Short-term targets:")
    report.append(f"  Posts: {snapshot.total_posts}/10 {'✓' if snapshot.total_posts >= 10 else ''}")
    report.append(f"  Karma: {snapshot.karma}/500 {'✓' if snapshot.karma >= 500 else ''}")

    # Medium-term (Month 1)
    report.append("\nMedium-term targets:")
    report.append(f"  Allies: {snapshot.allies_count}/3 {'✓' if snapshot.allies_count >= 3 else ''}")

    # Trend (if we have history)
    if len(history) >= 2:
        report.append("\n## Trend (vs last snapshot)\n")
        prev = history[-2] if len(history) > 1 else history[-1]

        karma_delta = snapshot.karma - prev.get('karma', 0)
        posts_delta = snapshot.total_posts - prev.get('total_posts', 0)
        replies_delta = snapshot.total_replies_received - prev.get('total_replies_received', 0)

        report.append(f"  Karma:   {'+' if karma_delta >= 0 else ''}{karma_delta}")
        report.append(f"  Posts:   {'+' if posts_delta >= 0 else ''}{posts_delta}")
        report.append(f"  Replies: {'+' if replies_delta >= 0 else ''}{replies_delta}")

    return "\n".join(report)


def mark_user_as_ally(username: str, notes: str = ""):
    """Mark a user as an ally"""
    storage = get_storage()
    user = storage.get_user(username)
    if user:
        user.relationship = "ally"
        user.notes = notes
        storage.save_user(user)
        print(f"Marked {username} as ally")
    else:
        print(f"User {username} not found in tracking")


def mark_user_as_rival(username: str, notes: str = ""):
    """Mark a user as a rival"""
    storage = get_storage()
    user = storage.get_user(username)
    if user:
        user.relationship = "rival"
        user.notes = notes
        storage.save_user(user)
        print(f"Marked {username} as rival")
    else:
        print(f"User {username} not found in tracking")


# CLI
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python kpi.py <command>")
        print("Commands: snapshot, report, ally <user>, rival <user>")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "snapshot":
        snapshot = record_snapshot()
        print(f"Snapshot recorded: {snapshot.timestamp}")
        print(f"  Karma: {snapshot.karma}")
        print(f"  Posts: {snapshot.total_posts}")
        print(f"  Avg upvotes: {snapshot.avg_upvotes_per_post}")

    elif cmd == "report":
        print(get_progress_report())

    elif cmd == "ally" and len(sys.argv) >= 3:
        mark_user_as_ally(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")

    elif cmd == "rival" and len(sys.argv) >= 3:
        mark_user_as_rival(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")

    else:
        print(f"Unknown command: {cmd}")
