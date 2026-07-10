# Expert Agents — Implementation Plan (Historical)

> **Note:** This document is a historical planning artifact from an earlier design
> iteration. The agent system was subsequently implemented differently:
> - The **SkillsLoader** (Phase 1–2 below) was built as documented and lives in
>   `backend/skills/loader.py`.
> - The **Agent node** system (event-triggered and schedule-triggered agents) was
>   implemented in `backend/agents/` — see `docs/AGENT_SCHEDULING.md` and
>   `docs/EVENT_SUBSCRIPTIONS.md` for the current design.
> - The **ExpertOrchestrator / MessageBus / MCPToolRegistry** architecture
>   (Phases 3–7 below) was **not implemented**. Expert agent selection and
>   multi-agent orchestration remain open design questions.

---

## Original Design State

Basic UI and configuration already exists:
- **Configuration**: `config/scb/schema_config.json` defines experts with id, name, color, icon, intro text, and system_context
- **Frontend**: `ExpertAgentSelector.jsx` for selecting experts, `ChatPanel.jsx` displays expert messages, Zustand store holds state
- **Backend**: `config_loader.py` validates config, `/api/presentation` serves it, `chat_service.py` handles LLM calls
- **Missing**: Actual expert AI logic, skills, MCP integration, inter-agent communication, security

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                      Frontend                           │
│  ExpertAgentSelector ←→ ChatPanel ←→ Zustand Store      │
│       │                    ↑                            │
│       └────── REST/WS ─────┼────────────────────────────┤
│                            │                            │
├────────────────────────────┼────────────────────────────┤
│                      Backend                            │
│                            │                            │
│  ┌─────────────────────────▼──────────────────────────┐ │
│  │              ExpertOrchestrator                     │ │
│  │  Receives message → selects expert(s) →            │ │
│  │  runs expert with skills+MCP → returns response    │ │
│  ├────────────────────────────────────────────────────┤ │
│  │                                                    │ │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────────────┐ │ │
│  │  │ Expert A │  │ Expert B │  │ Expert C         │ │ │
│  │  │ skills:  │  │ skills:  │  │ skills:          │ │ │
│  │  │  - s1    │  │  - s2    │  │  - s1, s3        │ │ │
│  │  │ mcp:     │  │ mcp:     │  │ mcp:             │ │ │
│  │  │  - tool1 │  │  - tool2 │  │  - tool1, tool3  │ │ │
│  │  └────┬─────┘  └────┬─────┘  └───────┬──────────┘ │ │
│  │       │              │                │            │ │
│  ├───────┼──────────────┼────────────────┼────────────┤ │
│  │       ▼              ▼                ▼            │ │
│  │  ┌──────────────────────────────────────────────┐  │ │
│  │  │           SkillsLoader                       │  │ │
│  │  │  Fetches SKILL.md from URLs at startup        │  │ │
│  │  │  Maps skill content → prompt injection        │  │ │
│  │  └──────────────────────────────────────────────┘  │ │
│  │                                                    │ │
│  │  ┌──────────────────────────────────────────────┐  │ │
│  │  │           MCPToolRegistry                    │  │ │
│  │  │  Manages MCP servers and tools               │  │ │
│  │  │  Exposes permitted tools to experts           │  │ │
│  │  └──────────────────────────────────────────────┘  │ │
│  │                                                    │ │
│  │  ┌──────────────────────────────────────────────┐  │ │
│  │  │           MessageBus                         │  │ │
│  │  │  Expert ↔ Expert communication               │  │ │
│  │  │  Expert ↔ Graph assistant coordination       │  │ │
│  │  │  Expert → User responses                     │  │ │
│  │  └──────────────────────────────────────────────┘  │ │
│  └────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

---

## Phase 1: Extended Configuration

### 1.1 New Config Structure for Experts

Skills are now linked to experts via URL lists. The format is `SKILL.md` (agentskills.io open standard, a superset of Claude Code). At app startup, `SkillsLoader` fetches each URL and injects the content into the expert's system prompt.

```json
{
  "expert_agents": [
    {
      "id": "metadata-expert",
      "name": "Metadata Expert",
      "name_en": "Metadata Expert",
      "specialty": "Statistical metadata...",
      "color": "#14B8A6",
      "icon": "TagsFill",
      "intro_sv": "Hej! Jag är...",
      "intro_en": "Hello! I'm...",
      "system_context": "You are an expert on...",

      "skills_urls": [
        "https://github.com/org/repo",
        "https://raw.githubusercontent.com/org/repo/main/.agents/skills/gsim/SKILL.md"
      ],
      "mcp_tools": ["graph-query", "sparql-endpoint"],
      "can_delegate_to": ["boa-expert"],
      "max_tool_calls_per_turn": 5
    }
  ],

  "skills_config": {
    "skills_dir": "config/scb/skills",
    "allow_external_skills": true,
    "trusted_domains": ["github.com", "raw.githubusercontent.com", "agentskills.io"],
    "cache_ttl_seconds": 3600,
    "max_skill_content_bytes": 50000
  }
}
```

### 1.2 SKILL.md Format (agentskills.io + Claude Code)

Skill files follow the open standard: YAML frontmatter + Markdown body in a file named `SKILL.md`.

```yaml
---
name: gsim-metadata
description: "Expert knowledge on GSIM statistical metadata model"
allowed-tools: "graph-query"
when-to-use: "Activate when user asks about statistical metadata, GSIM, or DDI"
metadata:
  version: "1.0"
  author: "SCB"
---
# GSIM Metadata Expert

You have deep knowledge of the Generic Statistical Information Model (GSIM)...
```

Directory structure in a GitHub repo (all searched automatically, in priority order):
```
.agents/skills/         ← priority 1 (most portable)
.claude/skills/         ← priority 2 (Claude Code)
.github/skills/         ← priority 3
skills/                 ← priority 4 (simplest)
```

### 1.3 Skill Node Type in the Graph

Skill nodes store dynamically added skills and can be linked to Agent nodes via `USES_SKILL` edges. Metadata structure:

```json
{
  "source_url": "https://raw.githubusercontent.com/...",
  "content": "# Skill content...",
  "allowed_tools": ["graph-query"],
  "when_to_use": "...",
  "version": "1.0"
}
```

### Files Implemented ✅

| File | Change |
|------|--------|
| `backend/config_loader.py` | `ExpertAgentConfig.skills_urls`, `SkillsConfig`, `PresentationConfig.skills_config` |
| `backend/agents/config.py` | `AgentConfig.skills_urls`, `AgentConfig.skill_node_ids` |
| `backend/agents/prompts.py` | `build_skills_section()`, `build_agent_system_prompt(skills=...)` |
| `config/default/schema_config.json` | `Skill` node type + `USES_SKILL` relationship |
| `backend/skills/__init__.py` | Package |
| `backend/skills/loader.py` | `SkillsLoader` + `SkillDefinition` + TTL cache |

---

## Phase 2: SkillsLoader (implemented ✅)

### 2.1 SkillsLoader Class

New file: `backend/skills/loader.py`

```python
class SkillsLoader:
    """Fetches and parses SKILL.md files from remote URLs."""

    async def load_from_urls(self, urls: List[str]) -> List[SkillDefinition]:
        """Loads skills from a list of URLs. Failures are logged and skipped."""

    async def _load_github_repo(self, url: str) -> List[SkillDefinition]:
        """Searches .agents/skills/, .claude/skills/, .github/skills/, skills/"""

    def _parse_skill_md(self, text: str, source_url: str) -> Optional[SkillDefinition]:
        """Parses YAML frontmatter + Markdown body."""

    def _sanitize(self, text: str) -> str:
        """Injection guard: rejects injection patterns, strips HTML tags."""
```

Supported URL types:
- **Direct file**: `https://raw.githubusercontent.com/.../SKILL.md`
- **GitHub repo**: `https://github.com/owner/repo` → GitHub Contents API → SKILL.md files
- **agentskills.io**: `https://agentskills.io/skills/...` → best-effort extraction

### 2.2 SkillDefinition Model

```python
class SkillDefinition(BaseModel):
    id: str                            # metadata.id or auto-generated from name
    name: str                          # required SKILL.md field
    description: str                   # required SKILL.md field
    content: str                       # Markdown body = the actual prompt text
    allowed_tools: List[str] = []      # from allowed-tools (space-separated)
    when_to_use: Optional[str] = None  # Claude Code extension
    effort: Optional[str] = None       # Claude Code extension
    license: Optional[str] = None
    metadata: Dict[str, str] = {}
    source_url: str
    loaded_at: datetime
```

### Files Created

| File | Purpose |
|------|---------|
| `backend/skills/__init__.py` | Package |
| `backend/skills/loader.py` | SkillsLoader + SkillDefinition + TTL cache |

---

## Phase 3: MCP Tool Registry (Backend)

### 3.1 MCPToolRegistry Class

New file: `backend/mcp/tool_registry.py`

```python
class MCPToolRegistry:
    """Manages MCP servers and exposes tools to experts."""

    def __init__(self, mcp_config: dict):
        self._servers: Dict[str, MCPServerConnection] = {}
        self._tool_permissions: Dict[str, ToolPermission] = {}
        self._available_tools: Dict[str, MCPTool] = {}

    async def initialize(self) -> None:
        """Starts MCP servers and discovers available tools."""

    def get_tools_for_expert(self, expert_id: str, allowed_tool_ids: List[str]) -> List[MCPTool]:
        """Returns only the MCP tools the expert is permitted to use.
        Filters based on the configured allowlist."""

    async def execute_tool(self, tool_name: str, arguments: dict,
                           caller_expert_id: str) -> ToolResult:
        """Executes an MCP tool with security checks.
        Verifies the calling expert has permission to use the tool."""

    def get_tool_schema(self, tool_name: str) -> dict:
        """Returns the JSON Schema for a tool's input."""
```

### 3.2 Built-in Graph Tools as MCP

Expose existing graph operations (from `service.py`) as MCP tools:

```python
# backend/mcp/graph_tools.py
GRAPH_TOOLS = [
    {
        "name": "graph-query",
        "description": "Search nodes and relationships in the knowledge graph",
        "input_schema": { ... },
        "handler": lambda args: graph_service.search_nodes(args["query"])
    },
    {
        "name": "graph-get-node",
        "description": "Retrieve a specific node with all its relationships",
        "input_schema": { ... },
        "handler": lambda args: graph_service.get_node(args["id"])
    }
]
```

### Files to Create

| File | Purpose |
|------|---------|
| `backend/mcp/__init__.py` | Package |
| `backend/mcp/tool_registry.py` | MCPToolRegistry |
| `backend/mcp/graph_tools.py` | Graph operations as MCP tools |

---

## Phase 4: ExpertOrchestrator (Backend, Core Logic)

### 4.1 ExpertOrchestrator

New file: `backend/experts/orchestrator.py`

This is the central layer coordinating expert AI calls.

```python
class ExpertOrchestrator:
    """Coordinates expert agents, their skills and tools."""

    def __init__(self, config: dict, skill_loader: SkillsLoader,
                 mcp_registry: MCPToolRegistry, chat_service: ChatService):
        self._experts: Dict[str, ExpertInstance] = {}
        self._skill_loader = skill_loader
        self._mcp_registry = mcp_registry
        self._chat_service = chat_service
        self._message_bus = MessageBus()

    def activate_expert(self, expert_id: str) -> None:
        """Creates an ExpertInstance with the correct skills and tools."""

    def deactivate_expert(self, expert_id: str) -> None:
        """Removes an active expert."""

    async def handle_user_message(self, message: str,
                                   active_expert_ids: List[str],
                                   conversation_history: List[dict]) -> ExpertResponse:
        """
        Main flow:
        1. Graph assistant responds first (existing chat_service)
        2. If active experts exist → determine which should respond
        3. Run relevant experts (with their skills+tools)
        4. Collect all responses and return
        """

    async def _run_expert(self, expert: ExpertInstance,
                           message: str, context: dict) -> str:
        """Runs a single expert with its system_context + skills."""

    async def _should_expert_respond(self, expert: ExpertInstance,
                                      message: str, assistant_response: str) -> bool:
        """Quick LLM classification: should this expert add value?"""
```

### 4.2 ExpertInstance

```python
class ExpertInstance:
    """Runtime representation of an active expert."""

    def __init__(self, config: ExpertAgentConfig,
                 skills: List[SkillDefinition],
                 tools: List[MCPTool]):
        self.config = config
        self.skills = skills
        self.tools = tools
        self.conversation_memory: List[dict] = []

    def build_system_prompt(self) -> str:
        """Builds complete system prompt from config.system_context + skill prompts."""

    def get_tool_definitions(self) -> List[dict]:
        """Returns tool schemas for LLM tool_use."""
```

### 4.3 Integration Flow

```
User sends message
        │
        ▼
ExpertOrchestrator.handle_user_message()
        │
        ├──► Graph assistant responds (existing logic)
        │         │
        │         ▼
        ├──► For each active expert:
        │       _should_expert_respond()? (quick classification)
        │         │
        │    ┌────┴─── yes ──┐
        │    │               │
        │    ▼               │
        │  _run_expert()     │
        │    │               │
        │    ├─ Build system prompt (system_context + skills)
        │    ├─ Grant access to expert's MCP tools
        │    ├─ LLM call with expert persona
        │    ├─ Handle tool_use calls (MCP)
        │    └─ Return expert response
        │                    │
        │    ┌───────────────┘
        │    │
        │    ▼
        └──► Aggregate all responses
                │
                ▼
        Return to frontend
        (graph assistant response + expert responses with metadata)
```

### Files to Create/Modify

| File | Action |
|------|--------|
| `backend/experts/__init__.py` | Package |
| `backend/experts/orchestrator.py` | ExpertOrchestrator |
| `backend/experts/instance.py` | ExpertInstance |
| `backend/ui/chat_service.py` | Integrate orchestrator into existing chat flow |
| `backend/ui/rest_api.py` | Extend `POST /ui/chat` with expert responses in response |

---

## Phase 5: MessageBus (Inter-Agent Communication)

### 5.1 Structured Communication

New file: `backend/experts/message_bus.py`

```python
class MessageBus:
    """Manages structured communication between agents."""

    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = {}
        self._message_log: List[AgentMessage] = []

    def send(self, message: AgentMessage) -> None:
        """Send a message from one agent to another."""

    def request(self, from_agent: str, to_agent: str,
                question: str) -> AgentMessage:
        """Synchronous question from one expert to another."""

    def broadcast(self, from_agent: str, content: str) -> None:
        """Send to all active agents."""

    def get_conversation_context(self, agent_id: str) -> List[AgentMessage]:
        """Retrieve relevant conversation history for an agent."""


class AgentMessage(BaseModel):
    from_agent: str          # "metadata-expert" | "graph-assistant" | "user"
    to_agent: str            # "boa-expert" | "all" | "user"
    message_type: str        # "question" | "answer" | "info" | "delegation"
    content: str
    timestamp: datetime
    metadata: dict = {}
```

### 5.2 Message Types

| Type | From → To | Description |
|------|-----------|-------------|
| `question` | Expert → Expert | Ask another expert |
| `answer` | Expert → Expert | Reply to a question |
| `info` | Expert → User | Inform the user |
| `delegation` | Expert → Expert | Delegate a task |
| `graph_query` | Expert → Graph assistant | Query graph data |
| `graph_result` | Graph assistant → Expert | Result from graph |

### 5.3 Delegation via `can_delegate_to`

An expert can delegate to another expert if configured:

```python
async def handle_delegation(self, from_expert: ExpertInstance,
                             to_expert_id: str, task: str) -> str:
    if to_expert_id not in from_expert.config.can_delegate_to:
        raise PermissionError(f"{from_expert.config.id} cannot delegate to {to_expert_id}")
    # Run receiving expert with the delegation task
```

### Files to Create

| File | Purpose |
|------|---------|
| `backend/experts/message_bus.py` | MessageBus + AgentMessage |

---

## Phase 6: Security

### 6.1 Security Principles

1. **Config owner is responsible** for ensuring skills and MCP servers are trusted
2. **Defense-in-depth**: Even with trusted skills, baseline protections are required
3. **Least privilege**: Each expert receives only the tools it needs

### 6.2 Security Layers

```
┌─────────────────────────────────────────┐
│ Layer 1: Config Validation              │
│ - Schema validation of skill content    │
│ - Verify MCP tools exist                │
│ - Allowlist: expert ↔ tool mapping      │
├─────────────────────────────────────────┤
│ Layer 2: Runtime Tool Access Control    │
│ - Expert can only call its own tools    │
│ - read_only flag is enforced            │
│ - max_tool_calls_per_turn limit         │
├─────────────────────────────────────────┤
│ Layer 3: Output Sanitization            │
│ - Expert responses are sanitized        │
│ - Injection attempts are not forwarded  │
│ - Responses tagged with expert_id       │
├─────────────────────────────────────────┤
│ Layer 4: Audit & Logging                │
│ - All MCP calls are logged              │
│ - Expert-to-expert messages are logged  │
│ - Token usage tracked per expert        │
└─────────────────────────────────────────┘
```

### 6.3 Concrete Security Measures

**SkillsLoader sanitization** (already implemented in `backend/skills/loader.py`):
- Regex blocklist for known injection patterns
- HTML/XML tag stripping
- Maximum content size per skill (50 KB raw, 8 000 chars in prompt)
- Trusted domains allowlist

**Config Validation** (`backend/experts/security.py`):

```python
class ExpertSecurityValidator:
    def validate_expert_config(self, expert: ExpertAgentConfig) -> List[str]:
        """Checks that the expert only references existing skills/tools."""

    def validate_skill(self, skill: SkillDefinition) -> List[str]:
        """Checks that the skill prompt does not contain dangerous instructions."""

    def validate_tool_access(self, expert_id: str, tool_name: str) -> bool:
        """Checks that the expert is permitted to use the tool."""
```

**Runtime Guards** (in `ExpertOrchestrator`):

```python
# Counter per expert per turn
if expert.tool_call_count >= expert.config.max_tool_calls_per_turn:
    return "Expert has reached its tool call limit for this turn."

# Tool allowlist
if tool_name not in expert.allowed_tools:
    log.warning(f"Expert {expert.id} tried to call unauthorized tool {tool_name}")
    raise PermissionError(...)

# Timeout per MCP call
result = await asyncio.wait_for(
    mcp_registry.execute_tool(tool_name, args, expert.id),
    timeout=tool_permissions[tool_name].timeout_ms / 1000
)
```

### Files to Create

| File | Purpose |
|------|---------|
| `backend/experts/security.py` | ExpertSecurityValidator + output sanitization |

---

## Phase 7: Frontend Updates

### 7.1 Extended API Response

```python
# backend/ui/rest_api.py - POST /ui/chat response
{
    "content": "Graph assistant response...",
    "toolUsed": "search_nodes",
    "toolResult": {...},
    "expert_responses": [
        {
            "expert_id": "metadata-expert",
            "expert_name": "Metadata Expert",
            "expert_color": "#14B8A6",
            "content": "I would like to add that...",
            "tools_used": ["graph-query"]
        }
    ]
}
```

### 7.2 Frontend Changes

| File | Change |
|------|--------|
| `frontend/web/src/services/api.js` | Handle `expert_responses` in the response |
| `frontend/web/src/store/graphStore.js` | Add expert responses as chat messages |
| `frontend/web/src/components/ChatPanel.jsx` | Display expert responses with correct styling (partially done) |
| `frontend/web/src/components/ExpertAgentSelector.jsx` | Show skills per expert (optional, phase 2) |

### 7.3 Message Flow in UI

```
User types: "What is the SNI code for manufacturing?"
    │
    ▼
[Graph assistant]: "SNI codes 10-33 cover the manufacturing industry..."
    │
    ▼  (if Metadata Expert is active)
[Metadata Expert 🟢]: "I would add that SNI 2007 is based on
 NACE Rev.2 and there are sub-levels..."
```

---

## Phase 8: Tests

| Test file | Tests |
|-----------|-------|
| `tests/test_skills_loader.py` | URL fetching, SKILL.md parsing, sanitization, GitHub repo discovery |
| `tests/test_mcp_registry.py` | Tool registration, access control |
| `tests/test_orchestrator.py` | Expert routing, multi-expert responses |
| `tests/test_message_bus.py` | Agent communication, delegation |
| `tests/test_security.py` | Allowlist, rate-limiting, sanitization |

---

## Implementation Order

```
Phase 1: Configuration       ████░░░░░░░░  Foundation   ✅ Done
  1.1 Extended config model
  1.2 SKILL.md URL-based skills
  1.3 Skill node type in graph

Phase 2: SkillsLoader        ██████░░░░░░  Core         ✅ Done
  2.1 SkillsLoader
  2.2 SkillDefinition

Phase 3: MCP Tool Registry   ████████░░░░  Core
  3.1 MCPToolRegistry
  3.2 Graph tools as MCP

Phase 4: ExpertOrchestrator  ██████████░░  Core
  4.1 ExpertOrchestrator
  4.2 ExpertInstance
  4.3 Integration with ChatService

Phase 5: MessageBus          ████████████  Coordination
  5.1 AgentMessage model
  5.2 Communication flow
  5.3 Delegation

Phase 6: Security            ████████████  Parallel with 3-5
  6.1 Config validation
  6.2 Runtime guards
  6.3 Output sanitization
  6.4 Audit logging

Phase 7: Frontend            ████████████  Integration
  7.1 API response handling
  7.2 Expert messages in chat

Phase 8: Tests               ████████████  Ongoing
```

## Phase Dependencies

```
Phase 1 ──► Phase 2 ──► Phase 4 ──► Phase 7
                │           ▲
                │           │
Phase 1 ──► Phase 3 ───────┘
                            │
                Phase 5 ────┘

Phase 6 runs in parallel with Phases 3-5
Phase 8 runs continuously
```
