# Implementeringsplan: Expert Agents med Skills och MCP-verktyg

## Nuläge

Grundläggande UI och konfiguration finns redan:
- **Konfiguration**: `config/scb/schema_config.json` definierar experter med id, namn, färg, ikon, intro-text och system_context
- **Frontend**: `ExpertAgentSelector.jsx` för val av expert, `ChatPanel.jsx` visar expertmeddelanden, Zustand-store håller state
- **Backend**: `config_loader.py` validerar config, `/api/presentation` serverar den, `chat_service.py` hanterar LLM-anrop
- **Saknas**: Faktisk expert-AI-logik, skills, MCP-integration, inter-agent-kommunikation, säkerhet

---

## Arkitekturöversikt

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
│  │  Tar emot meddelande → väljer rätt expert(er) →    │ │
│  │  kör expert med skills+MCP → returnerar svar       │ │
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
│  │  │           SkillRegistry                      │  │ │
│  │  │  Laddar skill-definitioner från disk          │  │ │
│  │  │  Mappar skill-id → prompt-template + tools    │  │ │
│  │  └──────────────────────────────────────────────┘  │ │
│  │                                                    │ │
│  │  ┌──────────────────────────────────────────────┐  │ │
│  │  │           MCPToolRegistry                    │  │ │
│  │  │  Hanterar MCP-servrar och verktyg            │  │ │
│  │  │  Exponerar godkända verktyg till experter     │  │ │
│  │  └──────────────────────────────────────────────┘  │ │
│  │                                                    │ │
│  │  ┌──────────────────────────────────────────────┐  │ │
│  │  │           MessageBus                         │  │ │
│  │  │  Expert ↔ Expert kommunikation               │  │ │
│  │  │  Expert ↔ Grafassistent koordinering         │  │ │
│  │  │  Expert → Användare svar                     │  │ │
│  │  └──────────────────────────────────────────────┘  │ │
│  └────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

---

## Fas 1: Utökad konfiguration

### 1.1 Ny config-struktur för experter

Skills kopplas nu till experter via URL-listor. Formatet är `SKILL.md` (agentskills.io-standarden, superset av Claude Code). Vid appstart hämtar `SkillsLoader` varje URL och injicerar innehållet i expertens systemprompt.

```json
{
  "expert_agents": [
    {
      "id": "metadata-expert",
      "name": "Metadataexpert",
      "name_en": "Metadata Expert",
      "specialty": "Statistiska metadata...",
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

### 1.2 SKILL.md-format (agentskills.io + Claude Code)

Skills-filer följer öppen standard: YAML frontmatter + Markdown body i en fil `SKILL.md`.

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

Du har djup kunskap om Generic Statistical Information Model (GSIM)...
```

Katalogstruktur i ett GitHub-repo (alla letade igenom automatiskt):
```
.agents/skills/         ← prioritet 1 (mest portabelt)
.claude/skills/         ← prioritet 2 (Claude Code)
.github/skills/         ← prioritet 3
skills/                 ← prioritet 4 (enklast)
```

### 1.3 Skill-nodtyp i grafen

Skill-noder lagrar dynamiskt tillagda skills och kan kopplas till Agent-noder via `USES_SKILL`-kanter. Metadata:

```json
{
  "source_url": "https://raw.githubusercontent.com/...",
  "content": "# Skill content...",
  "allowed_tools": ["graph-query"],
  "when_to_use": "...",
  "version": "1.0"
}
```

### Filer implementerade ✅

| Fil | Åtgärd |
|-----|--------|
| `backend/config_loader.py` | `ExpertAgentConfig.skills_urls`, `SkillsConfig`, `PresentationConfig.skills_config` |
| `backend/agents/config.py` | `AgentConfig.skills_urls`, `AgentConfig.skill_node_ids` |
| `backend/agents/prompts.py` | `build_skills_section()`, `build_agent_system_prompt(skills=...)` |
| `config/default/schema_config.json` | `Skill`-nodtyp + `USES_SKILL`-relation |
| `backend/skills/__init__.py` | Package |
| `backend/skills/loader.py` | `SkillsLoader` + `SkillDefinition` + cache |

---

## Fas 2: SkillsLoader (implementerad ✅)

### 2.1 SkillsLoader-klass

Ny fil: `backend/skills/loader.py`

```python
class SkillsLoader:
    """Hämtar och parsar SKILL.md-filer från externa URLs."""

    async def load_from_urls(self, urls: List[str]) -> List[SkillDefinition]:
        """Laddar skills från en lista URL:er. Fel loggas och hoppas över."""

    async def _load_github_repo(self, url: str) -> List[SkillDefinition]:
        """Söker i .agents/skills/, .claude/skills/, .github/skills/, skills/"""

    def _parse_skill_md(self, text: str, source_url: str) -> Optional[SkillDefinition]:
        """Parsar YAML frontmatter + Markdown body."""

    def _sanitize(self, text: str) -> str:
        """Injektionsskydd: avvisar injection-mönster, strippar HTML-taggar."""
```

URL-typer som stöds:
- **Direkt fil**: `https://raw.githubusercontent.com/.../SKILL.md`
- **GitHub repo**: `https://github.com/owner/repo` → GitHub Contents API → SKILL.md-filer
- **agentskills.io**: `https://agentskills.io/skills/...` → best-effort extraktion

### 2.2 SkillDefinition-modell

```python
class SkillDefinition(BaseModel):
    id: str                            # metadata.id eller auto-genererat från name
    name: str                          # obligatoriskt SKILL.md-fält
    description: str                   # obligatoriskt SKILL.md-fält
    content: str                       # Markdown-body = prompt-texten
    allowed_tools: List[str] = []      # från allowed-tools (space-separated)
    when_to_use: Optional[str] = None  # Claude Code-extension
    effort: Optional[str] = None       # Claude Code-extension
    license: Optional[str] = None
    metadata: Dict[str, str] = {}
    source_url: str
    loaded_at: datetime
```

### Filer skapade

| Fil | Syfte |
|-----|-------|
| `backend/skills/__init__.py` | Package |
| `backend/skills/loader.py` | SkillsLoader + SkillDefinition + TTL-cache |

---

## Fas 3: MCP Tool Registry (Backend)

### 3.1 MCPToolRegistry-klass

Ny fil: `backend/mcp/tool_registry.py`

```python
class MCPToolRegistry:
    """Hanterar MCP-servrar och exponerar verktyg till experter."""

    def __init__(self, mcp_config: dict):
        self._servers: Dict[str, MCPServerConnection] = {}
        self._tool_permissions: Dict[str, ToolPermission] = {}
        self._available_tools: Dict[str, MCPTool] = {}

    async def initialize(self) -> None:
        """Startar MCP-servrar och hämtar tillgängliga verktyg."""

    def get_tools_for_expert(self, expert_id: str, allowed_tool_ids: List[str]) -> List[MCPTool]:
        """Returnerar enbart de MCP-verktyg som experten får använda.
        Filtrerar baserat på konfigurerad allowlist."""

    async def execute_tool(self, tool_name: str, arguments: dict,
                           caller_expert_id: str) -> ToolResult:
        """Kör ett MCP-verktyg med säkerhetskontroll.
        Verifierar att anropande expert har rätt att använda verktyget."""

    def get_tool_schema(self, tool_name: str) -> dict:
        """Returnerar JSON Schema för ett verktygs input."""
```

### 3.2 Inbyggda grafverktyg som MCP

Exponera befintliga grafoperationer (från `service.py`) som MCP-verktyg:

```python
# backend/mcp/graph_tools.py
GRAPH_TOOLS = [
    {
        "name": "graph-query",
        "description": "Sök noder och relationer i kunskapsgrafen",
        "input_schema": { ... },
        "handler": lambda args: graph_service.search_nodes(args["query"])
    },
    {
        "name": "graph-get-node",
        "description": "Hämta en specifik nod med alla dess relationer",
        "input_schema": { ... },
        "handler": lambda args: graph_service.get_node(args["id"])
    }
]
```

### Filer att skapa

| Fil | Syfte |
|-----|-------|
| `backend/mcp/__init__.py` | Package |
| `backend/mcp/tool_registry.py` | MCPToolRegistry |
| `backend/mcp/graph_tools.py` | Grafoperationer som MCP-verktyg |

---

## Fas 4: ExpertOrchestrator (Backend, kärnlogik)

### 4.1 ExpertOrchestrator

Ny fil: `backend/experts/orchestrator.py`

Detta är det centrala lagret som koordinerar experternas AI-anrop.

```python
class ExpertOrchestrator:
    """Koordinerar expert-agenter, deras skills och verktyg."""

    def __init__(self, config: dict, skill_registry: SkillRegistry,
                 mcp_registry: MCPToolRegistry, chat_service: ChatService):
        self._experts: Dict[str, ExpertInstance] = {}
        self._skill_registry = skill_registry
        self._mcp_registry = mcp_registry
        self._chat_service = chat_service
        self._message_bus = MessageBus()

    def activate_expert(self, expert_id: str) -> None:
        """Skapar en ExpertInstance med rätt skills och verktyg."""

    def deactivate_expert(self, expert_id: str) -> None:
        """Tar bort en aktiv expert."""

    async def handle_user_message(self, message: str,
                                   active_expert_ids: List[str],
                                   conversation_history: List[dict]) -> ExpertResponse:
        """
        Huvudflöde:
        1. Grafassistenten svarar först (befintlig chat_service)
        2. Om aktiva experter finns → fråga vilka som bör svara
        3. Kör relevanta experter (med deras skills+tools)
        4. Samla ihop svar och returnera
        """

    async def _run_expert(self, expert: ExpertInstance,
                           message: str, context: dict) -> str:
        """Kör en enskild expert med dess system_context + skills."""

    async def _should_expert_respond(self, expert: ExpertInstance,
                                      message: str, assistant_response: str) -> bool:
        """Snabb LLM-bedömning: ska denna expert tillföra något?"""
```

### 4.2 ExpertInstance

```python
class ExpertInstance:
    """Runtime-representation av en aktiv expert."""

    def __init__(self, config: ExpertAgentConfig,
                 skills: List[SkillDefinition],
                 tools: List[MCPTool]):
        self.config = config
        self.skills = skills
        self.tools = tools
        self.conversation_memory: List[dict] = []

    def build_system_prompt(self) -> str:
        """Bygger komplett system-prompt från config.system_context + skill-prompts."""

    def get_tool_definitions(self) -> List[dict]:
        """Returnerar verktygs-scheman för LLM tool_use."""
```

### 4.3 Integrationsflöde

```
Användare skickar meddelande
        │
        ▼
ExpertOrchestrator.handle_user_message()
        │
        ├──► Grafassistenten svarar (befintlig logik)
        │         │
        │         ▼
        ├──► För varje aktiv expert:
        │       _should_expert_respond()? (snabb klassificering)
        │         │
        │    ┌────┴─── ja ───┐
        │    │                │
        │    ▼                │
        │  _run_expert()      │
        │    │                │
        │    ├─ Bygg system prompt (system_context + skills)
        │    ├─ Ge tillgång till expertens MCP-verktyg
        │    ├─ LLM-anrop med expertpersona
        │    ├─ Hantera ev. tool_use (MCP-anrop)
        │    └─ Returnera expert-svar
        │                     │
        │    ┌────────────────┘
        │    │
        │    ▼
        └──► Sammanställ alla svar
                │
                ▼
        Returnera till frontend
        (grafassistent-svar + expert-svar med metadata)
```

### Filer att skapa/ändra

| Fil | Åtgärd |
|-----|--------|
| `backend/experts/__init__.py` | Package |
| `backend/experts/orchestrator.py` | ExpertOrchestrator |
| `backend/experts/instance.py` | ExpertInstance |
| `backend/ui/chat_service.py` | Integrera orchestrator i befintligt chatflöde |
| `backend/ui/rest_api.py` | Utöka `POST /ui/chat` med expert-svar i response |

---

## Fas 5: MessageBus (inter-agent-kommunikation)

### 5.1 Strukturerad kommunikation

Ny fil: `backend/experts/message_bus.py`

```python
class MessageBus:
    """Hanterar strukturerad kommunikation mellan agenter."""

    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = {}
        self._message_log: List[AgentMessage] = []

    def send(self, message: AgentMessage) -> None:
        """Skicka ett meddelande från en agent till en annan."""

    def request(self, from_agent: str, to_agent: str,
                question: str) -> AgentMessage:
        """Synkron fråga från en expert till en annan."""

    def broadcast(self, from_agent: str, content: str) -> None:
        """Skicka till alla aktiva agenter."""

    def get_conversation_context(self, agent_id: str) -> List[AgentMessage]:
        """Hämta relevant konversationshistorik för en agent."""


class AgentMessage(BaseModel):
    from_agent: str          # "metadata-expert" | "graph-assistant" | "user"
    to_agent: str            # "boa-expert" | "all" | "user"
    message_type: str        # "question" | "answer" | "info" | "delegation"
    content: str
    timestamp: datetime
    metadata: dict = {}
```

### 5.2 Kommunikationstyper

| Typ | Från → Till | Beskrivning |
|-----|-------------|-------------|
| `question` | Expert → Expert | Fråga en annan expert |
| `answer` | Expert → Expert | Svar på fråga |
| `info` | Expert → Användare | Informera användaren |
| `delegation` | Expert → Expert | Delegera uppgift vidare |
| `graph_query` | Expert → Grafassistent | Fråga om grafdata |
| `graph_result` | Grafassistent → Expert | Resultat från graf |

### 5.3 Delegering via `can_delegate_to`

En expert kan delegera till en annan expert om den har det i sin config:

```python
async def handle_delegation(self, from_expert: ExpertInstance,
                             to_expert_id: str, task: str) -> str:
    if to_expert_id not in from_expert.config.can_delegate_to:
        raise PermissionError(f"{from_expert.config.id} cannot delegate to {to_expert_id}")
    # Kör mottagande expert med delegeringsuppgiften
```

### Filer att skapa

| Fil | Syfte |
|-----|-------|
| `backend/experts/message_bus.py` | MessageBus + AgentMessage |

---

## Fas 6: Säkerhet

### 6.1 Säkerhetsprinciper

1. **Config-ägaren ansvarar** för att skills och MCP-servrar är betrodda
2. **Defense-in-depth**: Även med betrodda skills behövs grundskydd
3. **Least privilege**: Varje expert får bara de verktyg den behöver

### 6.2 Säkerhetslager

```
┌─────────────────────────────────────────┐
│ Lager 1: Config Validation              │
│ - Schema-validering av skill.json       │
│ - Kontroll att MCP-verktyg finns        │
│ - Allowlist: expert ↔ verktyg-mappning  │
├─────────────────────────────────────────┤
│ Lager 2: Runtime Tool Access Control    │
│ - Expert kan bara anropa sina verktyg   │
│ - read_only-flagga respekteras          │
│ - max_tool_calls_per_turn limit         │
├─────────────────────────────────────────┤
│ Lager 3: Output Sanitization            │
│ - Experternas svar saniteras            │
│ - Inga injektionsförsök vidarebefordras │
│ - Svar märks med expert-id (spårbarhet) │
├─────────────────────────────────────────┤
│ Lager 4: Audit & Logging               │
│ - Alla MCP-anrop loggas                 │
│ - Expert-till-expert-meddelanden loggas │
│ - Token-förbrukning per expert spåras   │
└─────────────────────────────────────────┘
```

### 6.3 Konkreta säkerhetsåtgärder

**Config Validation** (`backend/experts/security.py`):

```python
class ExpertSecurityValidator:
    def validate_expert_config(self, expert: ExpertAgentConfig) -> List[str]:
        """Kontrollerar att experten bara refererar till existerande skills/tools."""

    def validate_skill(self, skill: SkillDefinition) -> List[str]:
        """Kontrollerar att skill-prompten inte innehåller farliga instruktioner."""

    def validate_tool_access(self, expert_id: str, tool_name: str) -> bool:
        """Kontrollerar att experten har rätt att använda verktyget."""
```

**Runtime Guards** (i `ExpertOrchestrator`):

```python
# Räknare per expert per turn
if expert.tool_call_count >= expert.config.max_tool_calls_per_turn:
    return "Expert har nått sin gräns för verktygningsanrop denna omgång."

# Verktygs-allowlist
if tool_name not in expert.allowed_tools:
    log.warning(f"Expert {expert.id} tried to call unauthorized tool {tool_name}")
    raise PermissionError(...)

# Timeout per MCP-anrop
result = await asyncio.wait_for(
    mcp_registry.execute_tool(tool_name, args, expert.id),
    timeout=tool_permissions[tool_name].timeout_ms / 1000
)
```

**Output Sanitization**:

```python
def sanitize_expert_output(response: str, expert_id: str) -> str:
    """
    - Strippa system-prompt-liknande instruktioner
    - Märk svaret med expert_id för spårbarhet
    - Begränsa svarslängd
    """
```

### Filer att skapa

| Fil | Syfte |
|-----|-------|
| `backend/experts/security.py` | ExpertSecurityValidator + sanitering |

---

## Fas 7: Frontend-uppdateringar

### 7.1 Utöka API-response

```python
# backend/ui/rest_api.py - POST /ui/chat response
{
    "content": "Grafassistentens svar...",
    "toolUsed": "search_nodes",
    "toolResult": {...},
    "expert_responses": [
        {
            "expert_id": "metadata-expert",
            "expert_name": "Metadataexpert",
            "expert_color": "#14B8A6",
            "content": "Jag vill tillägga att...",
            "tools_used": ["graph-query"]
        }
    ]
}
```

### 7.2 Frontend-ändringar

| Fil | Ändring |
|-----|---------|
| `frontend/web/src/services/api.js` | Hantera `expert_responses` i svaret |
| `frontend/web/src/store/graphStore.js` | Lägg till expert-svar som chatmeddelanden |
| `frontend/web/src/components/ChatPanel.jsx` | Visa expert-svar med rätt stil (redan delvis klart) |
| `frontend/web/src/components/ExpertAgentSelector.jsx` | Visa skills per expert (valfritt, fas 2) |

### 7.3 Meddelandeflöde i UI

```
Användaren skriver: "Vad är SNI-koden för tillverkning?"
    │
    ▼
[Grafassistent]: "SNI-kod 10-33 täcker tillverkningsindustrin..."
    │
    ▼  (om Metadataexpert är aktiv)
[Metadataexpert 🟢]: "Jag kan tillägga att SNI 2007 bygger på
 NACE Rev.2 och att det finns undernivåer..."
```

---

## Fas 8: Tester

| Testfil | Testar |
|---------|--------|
| `tests/test_skill_registry.py` | Laddning, validering, prompt-byggning |
| `tests/test_mcp_registry.py` | Verktygsregistrering, åtkomstkontroll |
| `tests/test_orchestrator.py` | Expert-routing, multi-expert-svar |
| `tests/test_message_bus.py` | Agent-kommunikation, delegering |
| `tests/test_security.py` | Allowlist, rate-limiting, sanitering |

---

## Implementeringsordning

```
Fas 1: Konfiguration         ████░░░░░░░░  Grund
  1.1 Utökad config-modell
  1.2 Skill-filer på disk

Fas 2: Skill Registry        ██████░░░░░░  Kärna
  2.1 SkillRegistry
  2.2 SkillDefinition

Fas 3: MCP Tool Registry     ████████░░░░  Kärna
  3.1 MCPToolRegistry
  3.2 Grafverktyg som MCP

Fas 4: ExpertOrchestrator     ██████████░░  Kärna
  4.1 ExpertOrchestrator
  4.2 ExpertInstance
  4.3 Integration med ChatService

Fas 5: MessageBus             ████████████  Koordinering
  5.1 AgentMessage-modell
  5.2 Kommunikationsflöde
  5.3 Delegering

Fas 6: Säkerhet               ████████████  Parallellt med 3-5
  6.1 Config-validering
  6.2 Runtime-guards
  6.3 Output-sanitering
  6.4 Audit-loggning

Fas 7: Frontend               ████████████  Integration
  7.1 API-response-hantering
  7.2 Expert-meddelanden i chatten

Fas 8: Tester                 ████████████  Löpande
```

## Beroenden mellan faser

```
Fas 1 ──► Fas 2 ──► Fas 4 ──► Fas 7
              │         ▲
              │         │
Fas 1 ──► Fas 3 ───────┘
                        │
              Fas 5 ────┘

Fas 6 körs parallellt med Fas 3-5
Fas 8 körs löpande
```
