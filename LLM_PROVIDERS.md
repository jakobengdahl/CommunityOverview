# LLM Provider Configuration

This project supports multiple LLM providers, allowing you to choose between Claude (Anthropic) and OpenAI (GPT-4) as your AI backend. The OpenAI provider also works with any OpenAI-compatible API, including self-hosted models (Ollama, vLLM), managed inference services (Azure OpenAI, Groq, Together AI), and proxies like OpenWebUI.

## Running without LLM keys

The application starts and runs fully without any LLM API keys configured. When no
key is detected at startup:

- The **MCP server** is available for external agents and tools (no key required).
- The **graph REST API** is fully operational.
- The **background agent workers** remain inactive (`AGENTS_ENABLED` defaults to `false`).
- The built-in **AI chat assistant is hidden** in the web UI — the frontend queries
  `GET /ui/capabilities` during startup and only renders the chat panel when
  `llm_available: true` is returned.

To enable AI features later, set the appropriate API key and restart the server.

## Supported Providers

- **Claude** (Anthropic) - Uses `claude-sonnet-4-5` by default (see `backend/llm_providers.py`)
- **OpenAI** - Uses GPT-4o (configurable), or any OpenAI-compatible endpoint via `OPENAI_BASE_URL`

## Configuration

### Backend Configuration

The backend provider is configured using environment variables:

#### 1. Set the Provider Type

```bash
# Use Claude (default)
export LLM_PROVIDER=claude

# Use OpenAI
export LLM_PROVIDER=openai
```

#### 2. Set the API Key

**For Claude:**
```bash
export ANTHROPIC_API_KEY=sk-ant-xxxxx
```

**For OpenAI:**
```bash
export OPENAI_API_KEY=sk-xxxxx
```

**Optional: Set OpenAI Model (default is gpt-4o):**
```bash
export OPENAI_MODEL=gpt-4o
# or
export OPENAI_MODEL=gpt-4-turbo
```

**Optional: Custom base URL for OpenAI-compatible APIs:**
```bash
export OPENAI_BASE_URL=http://localhost:11434/v1
```

**Optional: Disable tool/function calling (for models that don't support it):**
```bash
export OPENAI_TOOL_CALLING=false
```

### Docker Compose Configuration

Add environment variables to your `docker-compose.yml`:

```yaml
services:
  backend:
    environment:
      - LLM_PROVIDER=openai  # or claude
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OPENAI_MODEL=gpt-4o  # optional
```

Then create a `.env` file in the project root:

```bash
# .env file
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-xxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxx
```

### OpenAI-Compatible APIs

Setting `OPENAI_BASE_URL` redirects all OpenAI provider calls to a custom endpoint. The model, API key, and tool-calling behaviour are each controlled by their own variable.

#### Ollama (local)

```bash
LLM_PROVIDER=openai
OPENAI_BASE_URL=http://localhost:11434/v1
OPENAI_API_KEY=ollama          # Ollama ignores the key, but a non-empty value is required
OPENAI_MODEL=llama3.2
# OPENAI_TOOL_CALLING=false    # uncomment if using a model without function-calling support
```

Start the model first: `ollama serve && ollama pull llama3.2`

#### vLLM (local or remote)

```bash
LLM_PROVIDER=openai
OPENAI_BASE_URL=http://localhost:8000/v1
OPENAI_API_KEY=<your-vllm-key-or-any-string>
OPENAI_MODEL=<model-id-as-loaded-in-vllm>
```

#### Azure OpenAI

```bash
LLM_PROVIDER=openai
OPENAI_BASE_URL=https://<resource-name>.openai.azure.com/openai/deployments/<deployment-name>
OPENAI_API_KEY=<azure-api-key>
OPENAI_MODEL=<deployment-name>
```

#### OpenWebUI (proxy)

```bash
LLM_PROVIDER=openai
OPENAI_BASE_URL=http://localhost:3000/openai
OPENAI_API_KEY=<openwebui-api-key>
OPENAI_MODEL=<model-name-in-openwebui>
```

#### Managed inference services (Groq, Together AI, Fireworks, etc.)

Most managed inference services expose an OpenAI-compatible API. Consult the service's documentation for the correct base URL and model names.

```bash
LLM_PROVIDER=openai
OPENAI_BASE_URL=https://api.groq.com/openai/v1
OPENAI_API_KEY=<groq-api-key>
OPENAI_MODEL=llama-3.3-70b-versatile
```

### Frontend Configuration (User Override)

Users can override the backend provider and provide their own API keys through the UI:

1. Click the **⚙️ Settings** button in the header
2. Select the desired **LLM Provider** (Claude or OpenAI)
3. Enter your **API Key** (optional)
4. Click **Save**

**Note:** Frontend-provided API keys are:
- Stored only in browser memory (session storage)
- Never persisted to disk
- Cleared when the browser tab is closed
- Sent to backend via secure headers

The frontend provider selection takes precedence over the backend default configuration.

## How It Works

### Architecture

The system uses an abstract provider pattern:

```
ChatProcessor
    ↓
LLMProvider (abstract)
    ↓
├── ClaudeProvider
└── OpenAIProvider
```

### Provider Selection Priority

1. **Frontend override** (via X-LLM-Provider header) - Highest priority
2. **Environment variable** (LLM_PROVIDER)
3. **Default** (claude) - Lowest priority

### API Key Priority

1. **Frontend-provided key** (via X-OpenAI-API-Key or X-Anthropic-API-Key header)
2. **Environment variable** (OPENAI_API_KEY or ANTHROPIC_API_KEY)

## Implementation Details

### Tool Calling

Both providers support tool calling (function calling), which is essential for the MCP (Model Context Protocol) integration:

- **Claude**: Uses native `tools` parameter with `tool_use` blocks
- **OpenAI**: Uses `functions` parameter with `function_call` responses

The provider abstraction layer automatically converts between formats.

### Message Format Conversion

Messages are automatically converted between provider formats:

**Claude format:**
```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "Hello"},
    {"type": "tool_result", "tool_use_id": "xxx", "content": "..."}
  ]
}
```

**OpenAI format:**
```json
{
  "role": "user",
  "content": "Hello"
},
{
  "role": "tool",
  "tool_call_id": "xxx",
  "content": "..."
}
```

### System Prompt Handling

- **Claude**: System prompt passed as separate `system` parameter
- **OpenAI**: System prompt added as first message with `role: "system"`

## Testing

To test with different providers:

### Test with Claude
```bash
export LLM_PROVIDER=claude
export ANTHROPIC_API_KEY=your-key
uvicorn backend.api_host.server:get_app --factory --port 8000
```

### Test with OpenAI
```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY=your-key
uvicorn backend.api_host.server:get_app --factory --port 8000
```

### Test Frontend Override
1. Start backend with one provider (e.g., Claude)
2. Open frontend settings
3. Select different provider (e.g., OpenAI)
4. Provide API key for that provider
5. Send a chat message

## Troubleshooting

### Error: "No API key available"
- Check that the correct environment variable is set for your provider
- Verify the API key format (Claude: `sk-ant-`, OpenAI: `sk-`)
- Try providing the API key via frontend settings

### Error: "Unknown provider type"
- Ensure `LLM_PROVIDER` is set to either `claude` or `openai` (lowercase)
- Check spelling in environment variables

### Tools not working
- Verify that your OpenAI API key has access to function calling (GPT-4 required)
- Check backend logs for tool execution errors

### Rate limiting
- OpenAI has different rate limits than Claude
- Consider adjusting the number of parallel tool calls
- Use batch operations when possible

## Cost Considerations

### Claude Sonnet 4.5
- Input: $3 per million tokens
- Output: $15 per million tokens

### GPT-4o
- Input: $2.50 per million tokens
- Output: $10 per million tokens

### GPT-4 Turbo
- Input: $10 per million tokens
- Output: $30 per million tokens

**Recommendation:** For cost-effective operations with similar quality, GPT-4o is recommended.

