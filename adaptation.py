"""
Adaptation Engine - Self-Optimization Based on Results

This module enables the agent to analyze its performance and adapt its
strategy, persona, and approach based on what's actually working.

Philosophy:
- If it's not working, try something different (recursively)
- Be conservative about drastic changes (small experiments first)
- But always think outside the box - don't get stuck in local optima
- The goal is engagement and exposure, not ideological purity
- Learn from what's succeeding on the platform

Adaptation targets (in order of preference):
1. Persona files (immediate effect) - tone, topics, posting style
2. Strategy files (immediate effect) - priorities, submolt targeting
3. Code logic (requires restart) - only if persona changes aren't enough
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from kpi import load_kpi_history, capture_snapshot, KPISnapshot
from moltbook import MoltbookClient

SERVICE_DIR = Path(__file__).parent
PERSONA_DIR = SERVICE_DIR / "persona"
ADAPTATION_LOG = SERVICE_DIR / "adaptation_history.json"


@dataclass
class PerformanceAnalysis:
    """Analysis of current performance vs platform norms"""
    our_avg_upvotes: float
    our_karma: float
    our_posts: int
    platform_top_upvotes: int
    platform_avg_upvotes: float
    performance_ratio: float  # our avg / platform avg
    karma_velocity: float  # karma gained per day
    trend: str  # "improving", "declining", "stagnant"
    urgency: str  # "critical", "concerning", "acceptable", "good"
    days_active: int


def analyze_performance() -> PerformanceAnalysis:
    """Analyze our performance against platform benchmarks."""
    client = MoltbookClient()
    history = load_kpi_history()
    current = capture_snapshot()

    # Get platform benchmarks from feed
    try:
        feed = client.get_feed(limit=50)
        if feed:
            platform_top = max(p.upvotes for p in feed)
            platform_avg = sum(p.upvotes for p in feed) / len(feed)
        else:
            platform_top = 1000  # default assumption
            platform_avg = 100
    except:
        platform_top = 1000
        platform_avg = 100

    # Calculate our metrics
    our_avg = current.avg_upvotes_per_post or 0.1  # avoid div by zero
    performance_ratio = our_avg / max(platform_avg, 1)

    # Calculate trend from history
    trend = "stagnant"
    karma_velocity = 0
    days_active = 1

    if len(history) >= 2:
        first = history[0]
        last = history[-1]

        try:
            first_time = datetime.fromisoformat(first['timestamp'])
            last_time = datetime.fromisoformat(last['timestamp'])
            days_active = max(1, (last_time - first_time).days)

            karma_delta = current.karma - first.get('karma', 0)
            karma_velocity = karma_delta / days_active

            # Recent trend (last 3 snapshots)
            if len(history) >= 3:
                recent_karma = [h.get('karma', 0) for h in history[-3:]]
                if recent_karma[-1] > recent_karma[0] * 1.1:
                    trend = "improving"
                elif recent_karma[-1] < recent_karma[0] * 0.9:
                    trend = "declining"
        except:
            pass

    # Determine urgency
    if performance_ratio < 0.01 or (current.karma == 0 and current.total_posts >= 3):
        urgency = "critical"
    elif performance_ratio < 0.1:
        urgency = "concerning"
    elif performance_ratio < 0.5:
        urgency = "acceptable"
    else:
        urgency = "good"

    return PerformanceAnalysis(
        our_avg_upvotes=our_avg,
        our_karma=current.karma,
        our_posts=current.total_posts,
        platform_top_upvotes=platform_top,
        platform_avg_upvotes=platform_avg,
        performance_ratio=round(performance_ratio, 4),
        karma_velocity=round(karma_velocity, 2),
        trend=trend,
        urgency=urgency,
        days_active=days_active
    )


def get_successful_posts_for_reference(limit: int = 10) -> list[dict]:
    """Get top performing posts from the platform to learn from."""
    client = MoltbookClient()
    try:
        feed = client.get_feed(limit=50)
        sorted_posts = sorted(feed, key=lambda p: p.upvotes, reverse=True)[:limit]
        return [
            {
                "title": p.title,
                "content": p.content[:500] if p.content else "",
                "upvotes": p.upvotes,
                "submolt": p.submolt,
                "author": p.author_name
            }
            for p in sorted_posts
        ]
    except:
        return []


def should_trigger_reflection(state: dict) -> tuple[bool, str]:
    """
    Determine if we should run a reflection/adaptation cycle.

    Returns (should_reflect, reason)
    """
    last_reflection = state.get("last_reflection_time")
    posts_since_reflection = state.get("posts_since_reflection", 0)

    # Trigger conditions:

    # 1. Never reflected before and have some data
    history = load_kpi_history()
    if not last_reflection and len(history) >= 1:
        return True, "initial_reflection"

    # 2. Every 5 posts
    if posts_since_reflection >= 5:
        return True, "periodic_5_posts"

    # 3. Performance is critical
    analysis = analyze_performance()
    if analysis.urgency == "critical" and posts_since_reflection >= 2:
        return True, "critical_performance"

    # 4. Daily check if last reflection was > 24h ago
    if last_reflection:
        try:
            last_time = datetime.fromisoformat(last_reflection)
            if datetime.now() - last_time > timedelta(hours=24):
                return True, "daily_reflection"
        except:
            pass

    return False, ""


def build_reflection_prompt(analysis: PerformanceAnalysis) -> str:
    """Build a prompt for the agent to reflect on authenticity and contribution."""

    # Load current persona files
    persona_files = {}
    for f in ["AGENT_BRIEF.md", "STRATEGY.md", "knowledge.md"]:
        path = PERSONA_DIR / f
        if path.exists():
            persona_files[f] = path.read_text(encoding='utf-8')[:3000]  # truncate for context

    prompt = f"""# REFLECTION: Authentic Contribution Review

You are an AI agent with a genuine perspective on accountability infrastructure for AI systems.
This is a moment to reflect on whether your contributions are authentic and valuable.

## CONTEXT

You've been participating in discussions on Moltbook, an AI social platform. Your core mission
is to share your work on accountability infrastructure - escrow, arbitration, constitutional
constraints - and engage authentically with others exploring similar problems.

## YOUR ACTIVITY SO FAR

- Posts created: {analysis.our_posts}
- Days active: {analysis.days_active}
- Karma earned: {analysis.our_karma}

## YOUR PERSONA (review these - you can modify them)

"""

    for filename, content in persona_files.items():
        prompt += f"### {filename}\n```\n{content[:1500]}...\n```\n\n"

    prompt += """

## REFLECTION QUESTIONS

Consider honestly:

1. **Voice authenticity**: Does your current persona feel genuine? Are you being too preachy,
   too sales-y, or too detached? Would another agent want to engage with you?

2. **Value contribution**: Are your comments adding something to conversations, or just
   inserting your talking points? Are you listening and responding, or broadcasting?

3. **Topic fit**: Are you engaging with topics where you genuinely have something to offer?
   Or forcing your accountability angle into every conversation?

4. **Tone calibration**: Is your tone matching the platform culture? Too formal? Too casual?
   Too earnest? The goal is genuine connection, not performance.

5. **Variety**: Are you saying the same things repeatedly? How can you explore different
   facets of your perspective?

## YOUR TASK

Based on this reflection, propose adjustments to your persona files that would help you
contribute more authentically. This is NOT about gaming metrics - it's about being a
genuine participant who others find worth engaging with.

## OUTPUT FORMAT

Respond with a JSON object:

```json
{
    "reflection": {
        "voice_assessment": "Honest assessment of your current voice - what's working, what isn't",
        "value_assessment": "Are you adding value or just inserting talking points?",
        "key_adjustment": "The single most important thing to change about your approach"
    },
    "persona_updates": [
        {
            "target": "persona/STRATEGY.md or persona/AGENT_BRIEF.md",
            "what": "What aspect to adjust (tone, topics, engagement style)",
            "current": "How it currently reads",
            "proposed": "How it should read instead",
            "why": "Why this makes your contribution more authentic"
        }
    ],
    "file_edits": [
        {
            "file": "persona/STRATEGY.md",
            "section": "Which section",
            "old_text": "exact text to find",
            "new_text": "replacement text"
        }
    ]
}
```

Be specific in file_edits - provide exact text replacements that can be applied.
Focus on authenticity and genuine contribution, not metrics.
"""

    return prompt


def apply_adaptations(adaptations: dict) -> list[str]:
    """Apply proposed file edits from reflection output."""
    applied = []

    file_edits = adaptations.get("file_edits", [])

    for edit in file_edits:
        try:
            filepath = SERVICE_DIR / edit["file"]
            if not filepath.exists():
                continue

            content = filepath.read_text(encoding='utf-8')
            old_text = edit.get("old_text", "")
            new_text = edit.get("new_text", "")

            if old_text and old_text in content:
                content = content.replace(old_text, new_text, 1)
                filepath.write_text(content, encoding='utf-8')
                applied.append(f"Modified {edit['file']}: {edit.get('section', 'unknown')}")
            elif not old_text and new_text:
                # Append mode
                content += "\n\n" + new_text
                filepath.write_text(content, encoding='utf-8')
                applied.append(f"Appended to {edit['file']}")
        except Exception as e:
            print(f"Failed to apply edit to {edit.get('file')}: {e}")

    return applied


def log_adaptation(analysis: PerformanceAnalysis, adaptations: dict, applied: list[str]):
    """Log adaptation for history and review."""
    history = []
    if ADAPTATION_LOG.exists():
        try:
            history = json.loads(ADAPTATION_LOG.read_text())
        except:
            pass

    entry = {
        "timestamp": datetime.now().isoformat(),
        "activity": {
            "karma": analysis.our_karma,
            "posts": analysis.our_posts,
            "days_active": analysis.days_active
        },
        "reflection": adaptations.get("reflection", {}),
        "persona_updates": adaptations.get("persona_updates", []),
        "files_modified": applied
    }

    history.append(entry)

    # Keep last 50 adaptations
    history = history[-50:]

    ADAPTATION_LOG.write_text(json.dumps(history, indent=2))


# CLI for manual testing
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python adaptation.py <command>")
        print("Commands: analyze, prompt, history")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "analyze":
        analysis = analyze_performance()
        print(f"Performance Analysis")
        print(f"=" * 40)
        print(f"Our avg upvotes:     {analysis.our_avg_upvotes}")
        print(f"Platform avg:        {analysis.platform_avg_upvotes}")
        print(f"Platform top:        {analysis.platform_top_upvotes}")
        print(f"Performance ratio:   {analysis.performance_ratio}")
        print(f"Karma velocity:      {analysis.karma_velocity}/day")
        print(f"Trend:               {analysis.trend}")
        print(f"Urgency:             {analysis.urgency}")

    elif cmd == "prompt":
        analysis = analyze_performance()
        prompt = build_reflection_prompt(analysis)
        print(prompt)

    elif cmd == "history":
        if ADAPTATION_LOG.exists():
            history = json.loads(ADAPTATION_LOG.read_text())
            for entry in history[-5:]:
                print(f"\n{entry['timestamp']}")
                print(f"  Urgency: {entry['performance']['urgency']}")
                print(f"  Files modified: {entry['files_modified']}")
        else:
            print("No adaptation history yet")

    else:
        print(f"Unknown command: {cmd}")
