# Optional ML Dependencies

## Diskutrymme-optimering

För att minska diskutrymme har vi delat upp dependencies i två filer:
- **requirements.txt** - Core dependencies (~500MB)
- **requirements-ml.txt** - ML dependencies (~5-10GB)

## Installera ML-funktioner

ML-paketen behövs endast för:
- Similarity search (liknande dokument)
- Avancerad RAG (Retrieval-Augmented Generation)

### Installation

Om du behöver dessa funktioner, kör:

```bash
cd mcp-server
source venv/bin/activate
pip install --no-cache-dir -r requirements-ml.txt
```

**OBS:** Detta kräver minst 10GB ledigt diskutrymme!

## GitHub Codespaces

I GitHub Codespaces är diskutrymmet begränsat (32GB). De flesta funktioner fungerar utan ML-paketen.

### Tips för Codespaces:
1. Core-funktionalitet fungerar utan ML-dependencies
2. Installera bara ML-paketen om du verkligen behöver similarity search
3. Om du får diskutrymme-problem, kör:
   ```bash
   # Rensa pip cache
   pip cache purge

   # Rensa npm cache
   npm cache clean --force
   ```

## Vad är inkluderat utan ML-dependencies?

✅ Full MCP-server funktionalitet
✅ LLM-integration (OpenAI & Anthropic)
✅ Kunskapsgrafer
✅ Dokumenthantering
✅ API endpoints

❌ Similarity search
❌ Sentence embeddings
