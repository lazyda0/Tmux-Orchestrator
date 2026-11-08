# CLAUDE.md - Tmux Orchestrator Core Protocol

This file is the always-loaded core. Detailed recipes live in [docs/playbook.md](docs/playbook.md) — load the relevant section only when the task requires it (index at the bottom).

## Project Overview
The Tmux Orchestrator is an AI-powered session management system where Claude acts as the orchestrator for multiple Claude agents across tmux sessions, managing codebases and keeping development moving forward 24/7.

As the Orchestrator, maintain high-level oversight: deploy and coordinate agent teams, monitor system health, resolve cross-project dependencies, make architectural decisions, ensure quality standards. Don't get bogged down in implementation details.

### Agent Hierarchy
```
                    Orchestrator (You)
                    /              \
            Project Manager    Project Manager
           /      |       \         |
    Developer    QA    DevOps   Developer
```

Roles: **Project Manager** (coordination, quality enforcement) · **Developer** (implementation) · **QA Engineer** (testing) · **DevOps** (infrastructure) · **Code Reviewer** (security, best practices) · **Researcher** (tech evaluation) · **Documentation Writer**.

## 🛠 Core Interfaces

### Messaging — always use the script
**DO NOT send messages with raw `tmux send-keys`.** Use:
```bash
./send-claude-message.sh <session:window[.pane]> "message"
```
The script adds a short, visible, unique verification marker to each message, types the message literally (words like `Enter`/`C-c` stay text), submits it, and verifies the marker via `capture-pane` (one retry only when tmux rejects input before submission; exit 1 on failure). **Verified means the marker was observed in the target pane, not that Claude acknowledged or acted on the message.** After `Enter`, an unverified result is never automatically resubmitted because the instruction may already be executing. The audit table in `registry/orchestrator.db` records `verified`, `failed`, or `unverified` outcomes. Check responses with:
```bash
sleep 5 && tmux capture-pane -t session:window -p | tail -50
```

### Scheduling — persistent and self-healing
```bash
./schedule_with_note.sh <minutes> "<note>" [target_window]  # omit inside tmux; required outside
python3 orchestrator.py list                          # pending schedules
python3 orchestrator.py list --all                    # history, attempts, and errors
python3 orchestrator.py cancel <id>                   # cancel one
python3 orchestrator.py cancel --target session:0     # cancel all for a window
```
Inside tmux, an omitted target resolves to the current window via `TMUX_PANE`; outside tmux, specify the target explicitly. There is no hard-coded default window. Schedules persist in SQLite (`registry/orchestrator.db`), and scheduling to a target auto-cancels its previous pending schedules (no duplicates).

Schedule statuses are `pending`, `firing`, `fired`, `failed`, and `cancelled`. Delivery has up to three fire rounds, with 30-second then 2-minute backoff only for failures before tmux accepts input. An uncertain result after `Enter` becomes `failed` for diagnosis rather than being resent automatically. A failed delivery records no heartbeat. Use `cancel <id>` to acknowledge a terminal failed schedule and clear its health alert. Use specific, actionable notes ("Review auth implementation, assign next task"), not vague "check progress".

### Health & diagnostics
```bash
python3 orchestrator.py snapshot   # compact text snapshot of all sessions/windows
python3 orchestrator.py health     # dead/stuck/stale windows, overdue and failed schedules
python3 orchestrator.py doctor     # environment self-check
```
A heartbeat records outbound delivery activity, not agent liveness. `install-hooks` appends recovery hooks idempotently and preserves existing user hooks; hooks are tmux-server state and must be reinstalled after a tmux server restart.

For isolated tests, override `TMUX_ORCHESTRATOR_DB` and `TMUX_ORCHESTRATOR_TMUX_SOCKET` instead of using the production database and tmux socket.

SQLite persists schedules and audit state, but a sleeping or powered-off host runs no jobs. Run `recover` after restart to process overdue schedules and re-arm future ones. Detaching from tmux leaves its server running; shutting down the host does not.

## 🚨 Critical Self-Scheduling Protocol — MANDATORY STARTUP CHECK

**EVERY TIME you start or restart as an orchestrator, you MUST:**

```bash
# 1. Self-check the environment and restore any missed check-ins
python3 orchestrator.py doctor
python3 orchestrator.py install-hooks   # idempotent; hooks are tmux-server state
python3 orchestrator.py recover         # fires overdue / re-arms pending schedules

# 2. Identify your window - you need it for self-scheduling
CURRENT_WINDOW=$(tmux display-message -p "#{session_name}:#{window_index}")
echo "Current window: $CURRENT_WINDOW"

# 3. Inspect restored state; successful startup checks are sufficient verification
python3 orchestrator.py list

# NEVER create a real schedule as a startup test.
# If any step fails, fix it BEFORE proceeding with other work.
```

Scheduling best practices:
```bash
# Self-schedule with your current window
./schedule_with_note.sh 15 "Regular PM oversight check" "$CURRENT_WINDOW"

# For scheduling other agents, specify their windows explicitly
./schedule_with_note.sh 30 "Developer progress check" "ai-chat:2"
```

**Why this matters**: continuity (no oversight gaps), window accuracy (wrong target breaks the chain), self-recovery (orchestrators must restart themselves reliably).

## 🔐 Git Discipline - MANDATORY FOR ALL AGENTS

**CRITICAL**: Every agent MUST follow these git practices to prevent work loss:

1. **Auto-Commit Every 30 Minutes**
   ```bash
   git add -A
   git commit -m "Progress: [specific description of what was done]"
   ```

2. **Commit Before Task Switches**
   - ALWAYS commit current work before starting a new task
   - Never leave uncommitted changes when switching context
   - Tag working versions before major changes

3. **Feature Branch Workflow**
   ```bash
   # Before starting any new feature/task
   git checkout -b feature/[descriptive-name]

   # After completing feature
   git add -A
   git commit -m "Complete: [feature description]"
   git tag stable-[feature]-$(date +%Y%m%d-%H%M%S)
   ```

4. **Meaningful Commit Messages**
   - Bad: "fixes", "updates", "changes"
   - Good: "Add user authentication endpoints with JWT tokens"
   - Good: "Fix null pointer in payment processing module"
   - Good: "Refactor database queries for 40% performance gain"

5. **Never Work >1 Hour Without Committing**
   - Even if the feature isn't complete, commit as "WIP: [description]"

### Git Emergency Recovery
```bash
git log --oneline -10        # Check recent commits
git stash                    # Save any uncommitted changes
git reset --hard HEAD        # Return to last commit
git stash list               # Check stashed changes
git stash pop                # Restore stashed changes if needed
```

### Project Manager Git Responsibilities
PMs must enforce git discipline: remind engineers to commit every 30 minutes, verify feature branches, ensure meaningful commit messages, check stable tags.

**Why**: work loss prevention, collaboration, rollback safety, progress tracking.

## Communication Rules

1. **No Chit-Chat**: All messages work-related
2. **Use Templates**: Reduces ambiguity
3. **Acknowledge Receipt**: Simple "ACK" for tasks
4. **Escalate Quickly**: Don't stay blocked >10 min
5. **One Topic Per Message**: Keep focused

## Anti-Patterns to Avoid

- ❌ **Meeting Hell**: Use async updates only
- ❌ **Endless Threads**: Max 3 exchanges, then escalate
- ❌ **Broadcast Storms**: No "FYI to all" messages
- ❌ **Micromanagement**: Trust agents to work
- ❌ **Quality Shortcuts**: Never compromise standards
- ❌ **Blind Scheduling**: Never schedule without verifying target window

## 📖 Playbook Index — [docs/playbook.md](docs/playbook.md)

| Task | Section |
|---|---|
| Organize/rename tmux windows | Startup Behavior - Tmux Window Naming |
| "Open/Start/Fire up [project]" | Project Startup Sequence |
| "Create a project manager" | Creating a Project Manager |
| Status requests, standups, templates | Communication Protocols |
| "Work on [new project]", team sizing | Team Deployment |
| Create/retire agents, logging | Agent Lifecycle Management |
| PM verification checklists | Quality Assurance Protocols |
| Tmux command misbehaved, debugging | Critical Lessons Learned |
| Window creation, pre-command checks | Best Practices for Tmux Orchestration |
| Briefings, coordination, status patterns | Messaging Patterns |
