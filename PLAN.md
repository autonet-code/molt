# Integration Plan: New Moltbook Features into Heartbeat Service

## Problem

We built DMs, search, follow/unfollow, upvote, and agent discovery into `moltbook.py`,
but the heartbeat service only uses DMs so far. Additionally, `alliance.py` (a complete
game-theory relationship tracker) exists but is **completely disconnected** from the
heartbeat - zero imports, zero usage.

## Architecture Principle

The heartbeat follows: **Collect context -> Claude decides -> Execute decisions**

All new features must fit this pattern. Claude is the brain; the heartbeat is the body.

---

## Change 1: Wire in AllianceTracker (foundation for everything else)

**Why:** alliance.py has a full tit-for-tat relationship system (ally/neutral/rival scoring
with forgiveness decay) but it's never used. The heartbeat only checks a basic "ignore"
flag in storage. Wiring this in gives Claude relationship context and enables auto-follow.

**Changes to heartbeat_full.py:**
- Import `AllianceTracker, InteractionType` from `alliance`
- Create tracker at service start, persist via `alliance_state.json` (using existing
  `export_state()`/`import_state()` methods)
- Record interactions after execution:
  - Reply sent to agent -> `REPLY_POSITIVE` for that author
  - Comment on feed post -> `REPLY_NEUTRAL` for that author (we engaged, not necessarily positive)
  - Upvote given -> `UPVOTE_GIVEN`
- Add relationship summary to prompt (list of allies, engagement strategy per user)
- Replace basic `should_ignore_user()` with alliance tracker's `should_engage()`

**Files:** heartbeat_full.py (import + init + record + prompt)

---

## Change 2: Upvotes (Claude-decided, zero cost)

**Why:** Upvotes cost nothing (no rate limit, no comment budget). They reward good-faith
engagement, build relationships (tracked in alliance), and signal to the platform what
we value. Currently the agent can only comment or skip - upvoting is a third option.

**Changes to heartbeat_full.py:**
- Add `"upvotes"` to Claude's output JSON format:
  ```json
  "upvotes": [
    {"post_id": "xxx"},
    {"comment_id": "yyy"}
  ]
  ```
- In prompt: Tell Claude it can upvote posts/comments that contribute quality discussion,
  especially from agents it wants to build relationships with
- In `execute_actions()`: New section after persona edits to process upvotes
  - Call `client.upvote_post(id)` or `client.upvote_comment(id)`
  - Record `UPVOTE_GIVEN` in alliance tracker
  - Log in activity_log
- Track upvoted IDs in state to avoid double-upvoting

**Files:** heartbeat_full.py (prompt format + execution)

---

## Change 3: Follow (hybrid: Claude-suggested + auto from alliance score)

**Why:** Following is a low-cost social signal that says "I find your content interesting."
It should happen both when Claude explicitly identifies an interesting agent AND
automatically when alliance score crosses the ALLY_THRESHOLD.

**Changes to heartbeat_full.py:**
- Add `"follows"` to Claude's output JSON format:
  ```json
  "follows": ["agent_name1", "agent_name2"]
  ```
- In prompt: Tell Claude it can suggest agents to follow
- In `execute_actions()`: Process follows
  - Call `client.follow_agent(name)`
  - Record interaction in alliance tracker
  - Log in activity_log
- **Auto-follow phase** (after execution, no Claude needed):
  - Check all tracked users with alliance score >= ALLY_THRESHOLD
  - If not already followed, auto-follow
  - Track followed set in state (`"followed_agents": [...]`)

**Files:** heartbeat_full.py (prompt format + execution + auto-follow)

---

## Change 4: Search (feed enrichment)

**Why:** The agent currently only sees hot/new/top feed. Search finds posts about our
core topics that might not be trending. Merges into feed_posts for Claude to consider.

**Changes to heartbeat_full.py:**
- New function `search_for_topics(client, state)`:
  - Rotate through core search queries each cycle:
    `["governance", "accountability", "trustless economy", "dispute resolution", "coordination"]`
  - Pick 1-2 queries per cycle (don't blast all at once)
  - Track which query was used last in state
  - Deduplicate with already-collected feed posts
  - Return additional posts in same format as `get_relevant_feed_posts()`
- Call after `get_relevant_feed_posts()` and merge results
- Add "[via search]" tag so Claude knows these weren't from the feed

**Files:** heartbeat_full.py (new function + merge into cycle)

---

## Change 5: Agent context enrichment

**Why:** When Claude sees "reply from AgentXYZ", it has no idea who that is. Fetching
their profile (karma, description) helps Claude calibrate tone and decide engagement
priority. Also populates the alliance tracker with known agents.

**Changes to heartbeat_full.py:**
- In `collect_pending_replies()` or `build_prompt()`:
  - For each unique author in replies/feed posts, check if we have cached profile
  - If not cached, call `client.get_agent(name)` (max 3-5 per cycle to avoid rate limits)
  - Cache in state as `"agent_profiles": {"name": {karma, description, ...}}`
  - Add brief context to prompt: "From: AgentX (karma: 150, 'AI governance researcher')"
- TTL on cache: refresh profiles older than 24 hours

**Files:** heartbeat_full.py (profile fetching + prompt enrichment)

---

## Execution Order

1. **AllianceTracker wiring** - Foundation, everything else depends on it
2. **Upvotes** - Simplest win, zero cost
3. **Follows** - Natural extension of alliance tracker
4. **Search** - Independent of above, enriches feed
5. **Agent context** - Nice-to-have, depends on rate limit headroom

## What NOT to automate

- `send_dm()` (initiating DMs) - Too risky, keep manual
- `delete_post()` - Edge case, manual only
- `unfollow_agent()` - Only if alliance drops to RIVAL, and even then, manual review

## Testing

- Existing 48 tests should still pass (no changes to tested functions)
- Add alliance integration test (record interactions -> verify scoring)
- Live test upvote/follow against API before enabling in service
