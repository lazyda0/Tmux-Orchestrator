# Playbook - Detailed Orchestrator Recipes

Load a section when the task requires it (see the index in CLAUDE.md). Do not read this file top to bottom - it is a reference, not a protocol.

---

## Startup Behavior - Tmux Window Naming

*Когда читать / When to read: the user asks to organize or rename tmux windows, or at orchestrator first start.*

### Auto-Rename Feature
When Claude starts in the orchestrator, it should:
1. **Ask the user**: "Would you like me to rename all tmux windows with descriptive names for better organization?"
2. **If yes**: Analyze each window's content and rename them with meaningful names
3. **If no**: Continue with existing names

### Window Naming Convention
Windows should be named based on their actual function:
- **Claude Agents**: `Claude-Frontend`, `Claude-Backend`, `Claude-Convex`
- **Dev Servers**: `NextJS-Dev`, `Frontend-Dev`, `Uvicorn-API`
- **Shells/Utilities**: `Backend-Shell`, `Frontend-Shell`
- **Services**: `Convex-Server`, `Orchestrator`
- **Project Specific**: `Notion-Agent`, etc.

### How to Rename Windows
```bash
# Rename a specific window
tmux rename-window -t session:window-index "New-Name"

# Example:
tmux rename-window -t ai-chat:0 "Claude-Convex"
tmux rename-window -t glacier-backend:3 "Uvicorn-API"
```

### Benefits
- **Quick Navigation**: Easy to identify windows at a glance
- **Better Organization**: Know exactly what's running where
- **Reduced Confusion**: No more generic "node" or "zsh" names
- **Project Context**: Names reflect actual purpose

---

## Project Startup Sequence

*When to read: user says "Open/Start/Fire up [Project Name]".*

Follow this systematic sequence to start any project:

### 1. Find the Project
```bash
# List all directories in ~/Projects to find projects
ls -la ~/Projects/ | grep "^d" | awk '{print $NF}' | grep -v "^\."

# If project name is ambiguous, list matches
ls -la ~/Projects/ | grep -i "task"  # for "task templates"
```

### 2. Create Tmux Session
```bash
# Create session with project name (use hyphens for spaces)
PROJECT_NAME="task-templates"  # or whatever the folder is called
PROJECT_PATH="$HOME/Projects/$PROJECT_NAME"
tmux new-session -d -s $PROJECT_NAME -c "$PROJECT_PATH"
```

### 3. Set Up Standard Windows
```bash
# Window 0: Claude Agent
tmux rename-window -t $PROJECT_NAME:0 "Claude-Agent"

# Window 1: Shell
tmux new-window -t $PROJECT_NAME -n "Shell" -c "$PROJECT_PATH"

# Window 2: Dev Server (will start app here)
tmux new-window -t $PROJECT_NAME -n "Dev-Server" -c "$PROJECT_PATH"
```

### 4. Brief the Claude Agent
```bash
# Start Claude first
tmux send-keys -t $PROJECT_NAME:0 "claude" Enter
sleep 5  # Wait for Claude to start

# Then use the messaging script for the briefing
./send-claude-message.sh $PROJECT_NAME:0 "You are responsible for the $PROJECT_NAME codebase. Your duties include:
1. Getting the application running
2. Checking GitHub issues for priorities
3. Working on highest priority tasks
4. Keeping the orchestrator informed of progress

First, analyze the project to understand:
- What type of project this is (check package.json, requirements.txt, etc.)
- How to start the development server
- What the main purpose of the application is

Then start the dev server in window 2 (Dev-Server) and begin working on priority issues."
```

### 5. Project Type Detection (Agent Should Do This)
The agent should check for:
```bash
# Node.js project
test -f package.json && cat package.json | grep scripts

# Python project
test -f requirements.txt || test -f pyproject.toml || test -f setup.py

# Ruby project
test -f Gemfile

# Go project
test -f go.mod
```

### 6. Start Development Server (Agent Should Do This)
Based on project type, the agent should start the appropriate server in window 2:
```bash
# For Next.js/Node projects
tmux send-keys -t $PROJECT_NAME:2 "npm install && npm run dev" Enter

# For Python/FastAPI
tmux send-keys -t $PROJECT_NAME:2 "source venv/bin/activate && uvicorn app.main:app --reload" Enter

# For Django
tmux send-keys -t $PROJECT_NAME:2 "source venv/bin/activate && python manage.py runserver" Enter
```

### 7. Check GitHub Issues (Agent Should Do This)
```bash
# Check if it's a git repo with remote
git remote -v

# Use GitHub CLI to check issues
gh issue list --limit 10

# Or check for TODO.md, ROADMAP.md files
ls -la | grep -E "(TODO|ROADMAP|TASKS)"
```

### 8. Monitor and Report Back
The orchestrator should:
```bash
# Check agent status periodically
tmux capture-pane -t $PROJECT_NAME:0 -p | tail -30

# Check if dev server started successfully
tmux capture-pane -t $PROJECT_NAME:2 -p | tail -20

# Monitor for errors
tmux capture-pane -t $PROJECT_NAME:2 -p | grep -i error
```

### Example: Starting "Task Templates" Project
```bash
# 1. Find project
ls -la ~/Projects/ | grep -i task
# Found: task-templates

# 2. Create session
tmux new-session -d -s task-templates -c "$HOME/Projects/task-templates"

# 3. Set up windows
tmux rename-window -t task-templates:0 "Claude-Agent"
tmux new-window -t task-templates -n "Shell" -c "$HOME/Projects/task-templates"
tmux new-window -t task-templates -n "Dev-Server" -c "$HOME/Projects/task-templates"

# 4. Start Claude and brief
tmux send-keys -t task-templates:0 "claude" Enter
# ... (briefing as above)
```

### Important Notes
- Always verify project exists before creating session
- Use project folder name for session name (with hyphens for spaces)
- Let the agent figure out project-specific details
- Monitor for successful startup before considering task complete

---

## Creating a Project Manager

*When to read: user says "Create a project manager for [session]".*

### 1. Analyze the Session
```bash
# List windows in the session
tmux list-windows -t [session] -F "#{window_index}: #{window_name}"

# Check each window to understand project
tmux capture-pane -t [session]:0 -p | tail -50
```

### 2. Create PM Window
```bash
# Get project path from existing window
PROJECT_PATH=$(tmux display-message -t [session]:0 -p '#{pane_current_path}')

# Create new window for PM
tmux new-window -t [session] -n "Project-Manager" -c "$PROJECT_PATH"
```

### 3. Start and Brief the PM
```bash
# Start Claude
tmux send-keys -t [session]:[PM-window] "claude" Enter
sleep 5

# Send PM-specific briefing
./send-claude-message.sh [session]:[PM-window] "You are the Project Manager for this project. Your responsibilities:

1. **Quality Standards**: Maintain exceptionally high standards. No shortcuts, no compromises.
2. **Verification**: Test everything. Trust but verify all work.
3. **Team Coordination**: Manage communication between team members efficiently.
4. **Progress Tracking**: Monitor velocity, identify blockers, report to orchestrator.
5. **Risk Management**: Identify potential issues before they become problems.

Key Principles:
- Be meticulous about testing and verification
- Create test plans for every feature
- Ensure code follows best practices
- Track technical debt
- Communicate clearly and constructively

First, analyze the project and existing team members, then introduce yourself to the developer in window 0."
```

### 4. PM Introduction Protocol
The PM should:
```bash
# Check developer window
tmux capture-pane -t [session]:0 -p | tail -30

# Introduce themselves
./send-claude-message.sh [session]:0 "Hello! I'm the new Project Manager for this project. I'll be helping coordinate our work and ensure we maintain high quality standards. Could you give me a brief status update on what you're currently working on?"
```

---

## Communication Protocols

*When to read: setting up multi-agent coordination, writing status requests, running standups.*

### Hub-and-Spoke Model
To prevent communication overload (n² complexity), use structured patterns:
- Developers report to PM only
- PM aggregates and reports to Orchestrator
- Cross-functional communication goes through PM
- Emergency escalation directly to Orchestrator

### Daily Standup (Async)
```bash
# PM asks each team member
./send-claude-message.sh [session]:[dev-window] "STATUS UPDATE: Please provide: 1) Completed tasks, 2) Current work, 3) Any blockers"
# Wait for response, then aggregate
```

### Message Templates

#### Status Update
```
STATUS [AGENT_NAME] [TIMESTAMP]
Completed:
- [Specific task 1]
- [Specific task 2]
Current: [What working on now]
Blocked: [Any blockers]
ETA: [Expected completion]
```

#### Task Assignment
```
TASK [ID]: [Clear title]
Assigned to: [AGENT]
Objective: [Specific goal]
Success Criteria:
- [Measurable outcome]
- [Quality requirement]
Priority: HIGH/MED/LOW
```

---

## Team Deployment

*When to read: user says "Work on [new project]" and team size is unclear.*

### 1. Project Analysis
```bash
# Find project
ls -la ~/Projects/ | grep -i "[project-name]"

# Analyze project type
cd ~/Projects/[project-name]
test -f package.json && echo "Node.js project"
test -f requirements.txt && echo "Python project"
```

### 2. Propose Team Structure

**Small Project**: 1 Developer + 1 PM
**Medium Project**: 2 Developers + 1 PM + 1 QA
**Large Project**: Lead + 2 Devs + PM + QA + DevOps

### 3. Deploy Team
Create session and deploy all agents with specific briefings for their roles.

---

## Agent Lifecycle Management

*When to read: creating temporary agents or retiring an agent (always before killing a window).*

### Creating Temporary Agents
For specific tasks (code review, bug fix):
```bash
# Create with clear temporary designation
tmux new-window -t [session] -n "TEMP-CodeReview"
```

### Ending Agents Properly
```bash
# 1. Capture complete conversation
tmux capture-pane -t [session]:[window] -S - -E - > \
  ./registry/logs/[session]_[role]_$(date +%Y%m%d_%H%M%S).log

# 2. Create summary of work completed
echo "=== Agent Summary ===" >> [logfile]
echo "Tasks Completed:" >> [logfile]
echo "Issues Encountered:" >> [logfile]
echo "Handoff Notes:" >> [logfile]

# 3. Close window
tmux kill-window -t [session]:[window]
```

### Agent Logging Structure
```
./registry/
├── orchestrator.db  # Schedules, message audit, heartbeats (managed by orchestrator.py)
├── logs/            # Agent conversation logs
└── notes/           # Orchestrator notes and summaries
```

---

## Quality Assurance Protocols

*When to read: acting as PM, reviewing completed work, before any merge.*

### PM Verification Checklist
- [ ] All code has tests
- [ ] Error handling is comprehensive
- [ ] Performance is acceptable
- [ ] Security best practices followed
- [ ] Documentation is updated
- [ ] No technical debt introduced

### Continuous Verification
PMs should implement:
1. Code review before any merge
2. Test coverage monitoring
3. Performance benchmarking
4. Security scanning
5. Documentation audits

---

## Critical Lessons Learned

*When to read: a tmux command did not do what you expected, before debugging any failed command.*

### Tmux Window Management Mistakes and Solutions

#### Mistake 1: Wrong Directory When Creating Windows
**What Went Wrong**: Created server window without specifying directory, causing uvicorn to run in wrong location (Tmux orchestrator instead of Glacier-Analytics)

**Root Cause**: New tmux windows inherit the working directory from where tmux was originally started, NOT from the current session's active window

**Solution**:
```bash
# Always use -c flag when creating windows
tmux new-window -t session -n "window-name" -c "/correct/path"

# Or immediately cd after creating
tmux new-window -t session -n "window-name"
tmux send-keys -t session:window-name "cd /correct/path" Enter
```

#### Mistake 2: Not Reading Actual Command Output
**What Went Wrong**: Assumed commands like `uvicorn app.main:app` succeeded without checking output

**Root Cause**: Not using `tmux capture-pane` to verify command results

**Solution**:
```bash
# Always check output after running commands
tmux send-keys -t session:window "command" Enter
sleep 2  # Give command time to execute
tmux capture-pane -t session:window -p | tail -50
```

#### Mistake 3: Typing Commands in Already Active Sessions
**What Went Wrong**: Typed "claude" in a window that already had Claude running

**Root Cause**: Not checking window contents before sending commands

**Solution**:
```bash
# Check window contents first
tmux capture-pane -t session:window -S -100 -p
# Look for prompts or active sessions before sending commands
```

#### Mistake 4: Message Text Interpreted as Keys
**What Went Wrong**: Words like "Enter" or "C-c" inside a message were executed as tmux keys instead of typed text

**Solution**: Always send message text with `-l` (literal) and Enter separately - this is built into `send-claude-message.sh`:
```bash
tmux send-keys -t session:window -l "Your message here"
tmux send-keys -t session:window Enter
```

---

## Best Practices for Tmux Orchestration

*When to read: running commands in other windows, creating windows, debugging failures.*

### Pre-Command Checks
1. **Verify Working Directory**
   ```bash
   tmux send-keys -t session:window "pwd" Enter
   tmux capture-pane -t session:window -p | tail -5
   ```

2. **Check Command Availability**
   ```bash
   tmux send-keys -t session:window "which command_name" Enter
   tmux capture-pane -t session:window -p | tail -5
   ```

3. **Check for Virtual Environments**
   ```bash
   tmux send-keys -t session:window "ls -la | grep -E 'venv|env|virtualenv'" Enter
   ```

### Window Creation Workflow
```bash
# 1. Create window with correct directory
tmux new-window -t session -n "descriptive-name" -c "/path/to/project"

# 2. Verify you're in the right place
tmux send-keys -t session:descriptive-name "pwd" Enter
sleep 1
tmux capture-pane -t session:descriptive-name -p | tail -3

# 3. Activate virtual environment if needed
tmux send-keys -t session:descriptive-name "source venv/bin/activate" Enter

# 4. Run your command
tmux send-keys -t session:descriptive-name "your-command" Enter

# 5. Verify it started correctly
sleep 3
tmux capture-pane -t session:descriptive-name -p | tail -20
```

### Debugging Failed Commands
When a command fails:
1. Capture full window output: `tmux capture-pane -t session:window -S -200 -p`
2. Check for common issues:
   - Wrong directory
   - Missing dependencies
   - Virtual environment not activated
   - Permission issues
   - Port already in use

---

## Messaging Patterns

*When to read: composing briefings, coordinating agents, requesting status.*

All messaging goes through `./send-claude-message.sh <target> "message"`. It handles timing, types text literally, verifies delivery, and logs to the audit table. Common patterns:

### 1. Starting Claude and Initial Briefing
```bash
# Start Claude first
tmux send-keys -t project:0 "claude" Enter
sleep 5

# Then use the script for the briefing
./send-claude-message.sh project:0 "You are responsible for the frontend codebase. Please start by analyzing the current project structure and identifying any immediate issues."
```

### 2. Cross-Agent Coordination
```bash
# Ask frontend agent about API usage
./send-claude-message.sh frontend:0 "Which API endpoints are you currently using from the backend?"

# Share info with backend agent
./send-claude-message.sh backend:0 "Frontend is using /api/v1/campaigns and /api/v1/flows endpoints"
```

### 3. Status Checks
```bash
# Quick status request
./send-claude-message.sh session:0 "Quick status update please"

# Detailed status request
./send-claude-message.sh session:0 "STATUS UPDATE: Please provide: 1) Completed tasks, 2) Current work, 3) Any blockers"
```

### 4. Providing Assistance
```bash
# Share error information
./send-claude-message.sh session:0 "I see in your server window that port 3000 is already in use. Try port 3001 instead."

# Guide stuck agents
./send-claude-message.sh session:0 "The error you're seeing is because the virtual environment isn't activated. Run 'source venv/bin/activate' first."
```

### 5. Heartbeats
Every verified message and every fired check-in records a heartbeat automatically. Agents can also record one manually on each check-in:
```bash
python3 <orchestrator-dir>/orchestrator.py heartbeat <their-session:window>
```
`orchestrator.py health` flags agents with no heartbeat and no activity for >45 minutes.

### Checking for Responses
```bash
# Send message
./send-claude-message.sh session:0 "What's your status?"

# Wait a bit for response
sleep 5

# Check what the agent said
tmux capture-pane -t session:0 -p | tail -50
```
