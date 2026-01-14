# Felsökning: Byta till OpenAI

## Problemet du ser

```
Error code: 401 - {'type': 'error', 'error': {'type': 'authentication_error', 'message': 'invalid x-api-key'}}
```

Detta fel betyder att systemet **fortfarande använder Claude/Anthropic**, men API-nyckeln är ogiltig.

## Lösning: Konfigurera OpenAI korrekt

### Metod 1: Via Miljövariabler (Backend)

**Stäng servern (CTRL+C) och starta om med:**

```bash
export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-proj-xxxxx  # Din OpenAI API-nyckel
cd mcp-server
python server.py
```

### Metod 2: Via Frontend Settings (Rekommenderat för testning)

1. **Öppna applikationen i webbläsaren**
2. **Klicka på ⚙️ Settings** (i övre högra hörnet)
3. **Välj "OpenAI (GPT-4)" från dropdown-menyn**
4. **Mata in din OpenAI API-nyckel** (börjar med `sk-proj-...` eller `sk-...`)
5. **Klicka "Save"**
6. **Skicka ett meddelande i chatten**

### Metod 3: Via Docker Compose

Redigera `docker-compose.yml` eller skapa en `.env`-fil:

```bash
# .env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-proj-xxxxx
```

Starta om containern:
```bash
docker-compose down
docker-compose up
```

## Verifiera att OpenAI används

När du skickar ett meddelande ska du se i backend-loggarna:

**Med Claude (fel):**
```
INFO: HTTP Request: POST https://api.anthropic.com/v1/messages
```

**Med OpenAI (rätt):**
```
INFO: HTTP Request: POST https://api.openai.com/v1/chat/completions
```

## Vanliga Fel

### 1. "invalid x-api-key" (det du ser nu)
- **Problem**: Systemet använder fortfarande Claude
- **Lösning**: Sätt `LLM_PROVIDER=openai` INNAN du startar servern

### 2. "No API key available"
- **Problem**: Ingen OpenAI API-nyckel konfigurerad
- **Lösning**: Sätt `OPENAI_API_KEY` miljövariabel eller använd frontend settings

### 3. "Module 'openai' not found"
- **Problem**: OpenAI SDK inte installerat
- **Lösning**: `pip install openai>=1.0.0` (redan fixat ✓)

### 4. "Rate limit exceeded"
- **Problem**: För många requests till OpenAI
- **Lösning**: Vänta 60 sekunder eller uppgradera API-plan

## Debug-Kommando

Kör detta för att se vilken provider som är konfigurerad:

```bash
cd mcp-server
python << 'EOF'
import os
from dotenv import load_dotenv
load_dotenv()

provider = os.getenv("LLM_PROVIDER", "claude")
print(f"Current provider: {provider}")

if provider == "openai":
    key = os.getenv("OPENAI_API_KEY", "NOT SET")
    print(f"OpenAI API key: {key[:20]}..." if key != "NOT SET" else "OpenAI API key: NOT SET")
else:
    key = os.getenv("ANTHROPIC_API_KEY", "NOT SET")
    print(f"Anthropic API key: {key[:20]}..." if key != "NOT SET" else "Anthropic API key: NOT SET")
EOF
```

## Snabb Test

```bash
# Sätt miljövariabler
export LLM_PROVIDER=openai
export OPENAI_API_KEY=din-nyckel-här

# Starta servern
cd mcp-server
python server.py

# I en annan terminal/webbläsare: skicka ett test-meddelande
# Titta på server-loggarna - de ska visa openai.com requests, inte anthropic.com
```

## Fortfarande problem?

Om du fortfarande ser Anthropic-requests trots att du satt `LLM_PROVIDER=openai`:

1. Kontrollera att miljövariabeln verkligen är satt: `echo $LLM_PROVIDER`
2. Starta om backend-servern helt (CTRL+C och starta igen)
3. Rensa browser cache och ladda om frontend
4. Kontrollera backend-loggarna vid startup - du bör se:
   ```
   Warning: ANTHROPIC_API_KEY not found in environment variables
   ```
   (Detta är OK när du använder OpenAI!)
