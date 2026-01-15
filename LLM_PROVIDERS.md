# LLM Provider Configuration

This project supports multiple LLM providers, allowing you to choose between Claude (Anthropic) and OpenAI (GPT-4) as your AI backend.

## Supported Providers

- **Claude** (Anthropic) - Uses Claude Sonnet 4.5
- **OpenAI** - Uses GPT-4o (configurable)

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
cd mcp-server
python server.py
```

### Test with OpenAI
```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY=your-key
cd mcp-server
python server.py
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

## Future Enhancements

Potential additions:
- Support for more providers (Gemini, Llama, etc.)
- Provider-specific optimizations
- Automatic fallback between providers
- Cost tracking per provider
- Performance metrics comparison

## Files Modified

Key files in this implementation:

- `mcp-server/llm_providers.py` - Provider abstraction layer
- `mcp-server/chat_logic.py` - ChatProcessor with provider support
- `mcp-server/server.py` - HTTP endpoint handling
- `mcp-server/requirements.txt` - Dependencies
- `frontend/src/store/graphStore.js` - State management
- `frontend/src/components/Header.jsx` - Settings UI
- `frontend/src/services/api.js` - API client
