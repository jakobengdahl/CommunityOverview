# ML Dependencies - Optimized Installation

## Diskutrymme-optimering

ML-funktionalitet är nu **inkluderad i requirements.txt** med optimeringar för att minska diskutrymme:

### Vad har optimerats?

1. **PyTorch CPU-only** (~800MB istället för ~4GB)
   - Använder `--extra-index-url https://download.pytorch.org/whl/cpu`
   - Ingen CUDA/GPU support (behövs ej för Codespaces)

2. **Minimala dependencies**
   - Bara det som faktiskt används av koden
   - Development tools flyttade till `requirements-dev.txt`

3. **--no-cache-dir flag**
   - Förhindrar pip från att spara cache
   - Sparar ytterligare 1-2GB

### Resultat

- **Tidigare:** ~10GB total installation
- **Nu:** ~2-3GB total installation
- **Besparing:** ~70% mindre diskutrymme!

## Funktioner inkluderade

✅ Full MCP-server funktionalitet
✅ LLM-integration (OpenAI & Anthropic)
✅ Kunskapsgrafer med semantic similarity
✅ Dokumenthantering (PDF, DOCX)
✅ Vector embeddings (sentence-transformers)
✅ Similarity search
✅ String matching (Levenshtein)

## GitHub Codespaces

Installation fungerar nu i GitHub Codespaces utan att fylla disken!

### Tips för Codespaces:
1. Alla ML-funktioner fungerar nu direkt
2. CPU-only PyTorch är tillräckligt snabbt för de flesta use cases
3. Om du får diskutrymme-problem, kör:
   ```bash
   # Rensa pip cache
   pip cache purge

   # Rensa npm cache
   npm cache clean --force
   ```

## Development

För testing och utveckling, installera även:

```bash
pip install --no-cache-dir -r requirements-dev.txt
```

Detta inkluderar pytest och black för code formatting.
