# Testing Guide för Visualisering och Högermeny

## Förberedelser
1. Starta backend: `cd mcp-server && source venv/bin/activate && python server.py`
2. Starta frontend: `cd frontend && npm run dev`
3. Öppna Chrome DevTools (F12) och gå till Console-fliken

## Test 1: Visualisering uppdateras vid sökning

### Steg:
1. Öppna applikationen i webbläsaren
2. Skriv i chatten: "visa NIS2-projekt"
3. Vänta på svar från Claude

### Förväntat resultat:
**I Console ska du se:**
```
[ChatPanel] ========== BACKEND RESPONSE ==========
[ChatPanel] Full response: { ... }
[ChatPanel] toolResult exists: true
[ChatPanel] toolResult.nodes exists: true
[ChatPanel] toolResult.nodes length: X  (där X > 0)
[ChatPanel] toolResult.edges exists: true
[ChatPanel] Calling updateVisualization with:
[ChatPanel]   - Nodes count: X
[ChatPanel]   - Edges count: Y
[GraphStore] updateVisualization called with:
[GraphStore]   - Nodes: X nodes
[GraphStore]   - Edges: Y edges
[GraphStore] State updated successfully
```

**I UI ska du se:**
- Grafen uppdateras med noder och kopplingar
- Noderna visas med rätt färger baserat på typ
- Kopplingarna visas mellan noderna

### Om testet misslyckas:
- Kolla console-loggen för att se vilket steg som saknas
- Om "toolResult.nodes exists: false" → Problem i backend aggregering
- Om "NOT calling updateVisualization" → Problem i ChatPanel logik
- Om "State updated successfully" men ingen visualisering → Problem i VisualizationPanel rendering

## Test 2: Högermeny visas vid högerklick

### Steg:
1. Se till att grafen visar några noder (kör Test 1 först)
2. Högerklicka på ett tomt område i grafen (INTE på en nod)

### Förväntat resultat:
**I Console ska du se:**
```
[VisualizationPanel] onPaneContextMenu triggered
[VisualizationPanel] Context menu should show at: X, Y
```

**I UI ska du se:**
- En anpassad meny med rubriken "Add Element"
- Ett menyalternativ "▭ Rectangle"
- INTE webbläsarens standard högerklicksmeny

### Om testet misslyckas:
- Om du ser webbläsarens meny istället → onPaneContextMenu anropas inte
- Om du inte ser någon logg i console → Event når inte React Flow
- Ta skärmdump av vad som visas och dela med utvecklaren

## Test 3: Högerklick på nod döljer noden

### Steg:
1. Se till att grafen visar några noder
2. Högerklicka DIREKT på en nod

### Förväntat resultat:
**I UI ska du se:**
- Noden försvinner från grafen (göms)
- Ingen högermeny visas

## Felsökning

### Problem: Visualiseringen uppdateras inte
**Kolla detta:**
1. Finns det data i backend response?
   - Sök efter `[ChatPanel] toolResult.nodes length:` i console
2. Anropas updateVisualization?
   - Sök efter `[ChatPanel] Calling updateVisualization` i console
3. Uppdateras state?
   - Sök efter `[GraphStore] State updated successfully` i console

**Lösningar:**
- Om data saknas i response → Backend aggregerar inte korrekt
- Om updateVisualization inte anropas → ChatPanel logiken är fel
- Om state uppdateras men UI inte → React rendering-problem

### Problem: Högermenyn visas inte
**Kolla detta:**
1. Anropas onPaneContextMenu?
   - Sök efter `[VisualizationPanel] onPaneContextMenu triggered` i console
2. Högerklickar du på rätt ställe?
   - Högerklicka på tomt område, INTE på en nod

**Lösningar:**
- Om event inte triggas → React Flow får inte eventet
- Om webbläsarmenyn visas → preventDefault fungerar inte

## Rapportera problem

När du rapporterar problem, inkludera:
1. Skärmdump av UI
2. Hela console-loggen (kopiera från DevTools)
3. Vilka steg du följde
4. Backend terminal output
