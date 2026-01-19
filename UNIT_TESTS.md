# Unit Tests Guide

This document describes the unit tests for graph export and visualization functionality.

## Backend Tests

### Installation

Install the development dependencies:

```bash
cd mcp-server
pip install -r requirements-dev.txt
```

### Running Tests

Run export/visualization tests:
```bash
cd mcp-server
python -m pytest tests/test_export_visualization.py -v
```

Run all backend tests:
```bash
python -m pytest tests/ -v
```

### Test Coverage - Export & Visualization

The file `tests/test_export_visualization.py` includes:

#### Export Endpoint Tests
- ✅ `test_export_graph_endpoint` - Verifies export returns properly serialized data
- ✅ `test_export_empty_graph` - Handles empty graphs correctly
- ✅ Datetime field serialization (created_at, updated_at)
- ✅ Proper JSON structure (version, exportDate, nodes, edges, counts)

#### Visualization Loading Tests
- ✅ `test_get_visualization_returns_content_not_view_node` - Returns actual content nodes, NOT the view node itself
- ✅ `test_get_visualization_missing_view` - Handles non-existent views gracefully
- ✅ `test_get_visualization_with_deleted_nodes` - Handles deleted/missing nodes
- ✅ `test_get_visualization_empty_view` - Handles empty views
- ✅ `test_get_visualization_with_hidden_nodes` - Includes hidden node IDs
- ✅ `test_get_visualization_datetime_serialization` - Datetime serialization in results

## Frontend Tests

### Installation

Install frontend dependencies:

```bash
cd frontend
npm install
```

### Running Tests

Run graphStore tests:
```bash
cd frontend
npm test -- src/store/graphStore.test.js
```

Run all frontend tests:
```bash
npm test
```

### Test Coverage - Graph Store

The file `src/store/graphStore.test.js` includes:

#### Node Addition & Merging Tests
- ✅ `should add new nodes to an empty visualization`
- ✅ `should merge new nodes with existing nodes without duplicates`
- ✅ `should not add duplicate nodes`
- ✅ `should not add duplicate edges`
- ✅ `should include edges connecting new and existing nodes`
- ✅ `should set clearGroupsFlag to true`
- ✅ `should reset clearGroupsFlag after timeout`
- ✅ `should handle empty arrays gracefully`

#### Visualization Management Tests
- ✅ `should replace entire visualization` (updateVisualization)
- ✅ `should handle visualization load with positions`

#### Position Update Tests
- ✅ `should update positions of existing nodes`
- ✅ `should preserve nodes without position updates`

#### Hidden Nodes Tests
- ✅ `should set hidden node IDs`
- ✅ `should toggle node visibility`

## Error Logging

### Backend Logging

**Export endpoint** (`/export_graph`):
```
[Export] Starting graph export...
[Export] Total nodes in storage: X
[Export] Total edges in storage: Y
[Export] Successfully dumped X nodes and Y edges
[Export] Serializing export data to JSON...
[Export] Serialized N bytes
[Export] Successfully parsed JSON, returning response
```

**Visualization loading** (`get_visualization`):
```
[GetVisualization] Loading visualization: ViewName
[GetVisualization] Found view node: view-id-123
[GetVisualization] View data keys: ['nodes', 'hidden_nodes']
[GetVisualization] Node positions: X, Hidden nodes: Y
[GetVisualization] Extracted N node IDs from view
[GetVisualization] Successfully loaded X nodes and Y edges
```

### Frontend Logging

**Graph export** (Header component):
```
[Header] Starting graph export...
[Header] Fetching from http://localhost:8000/export_graph
[Header] Response status: 200
[Header] Response ok: true
[Header] Export data received: { nodes: X, edges: Y, ... }
[Header] Graph exported successfully
```

## Debugging Test Failures

### Backend

Check console output for:
- `[Export] ERROR:` - Export failures
- `[GetVisualization] WARNING:` - Missing nodes in views
- Traceback information for detailed error location

### Frontend

Check browser console for:
- `[Header] Error:` - Export request failures
- `[ChatPanel]` - Visualization loading issues
- `[GraphStore]` - State management problems

## Test Scenarios

### Scenario 1: Graph Export

**What it tests:**
- Export endpoint returns all nodes and edges
- Datetime fields are properly serialized to ISO format
- Empty graphs are handled correctly
- Response structure matches expected format

**When to run:**
- After modifying export endpoint
- After changing Node/Edge models
- After datetime handling changes

### Scenario 2: Visualization Content Loading

**What it tests:**
- When user asks to "show visualization X", system loads the actual content nodes
- The VisualizationView metadata node itself is NOT displayed
- Saved positions are included in the response
- Hidden node states are preserved
- Missing/deleted nodes are handled gracefully

**When to run:**
- After modifying `get_visualization` function
- After changing visualization view storage format
- After UI visualization display changes

### Scenario 3: Node Merging

**What it tests:**
- Adding new nodes merges with existing ones (doesn't replace)
- Duplicate nodes/edges are prevented
- Edges connecting new and existing nodes are included
- Node highlighting works correctly

**When to run:**
- After modifying `addNodesToVisualization`
- After changing node/edge data structures
- After UI node addition behavior changes

## CI/CD Integration

Add to `.github/workflows/test.yml`:

```yaml
name: Unit Tests

on: [push, pull_request]

jobs:
  backend-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
        with:
          python-version: '3.10'
      - name: Install dependencies
        run: |
          cd mcp-server
          pip install -r requirements-dev.txt
      - name: Run tests
        run: |
          cd mcp-server
          pytest tests/test_export_visualization.py -v

  frontend-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-node@v2
        with:
          node-version: '18'
      - name: Install dependencies
        run: |
          cd frontend
          npm install
      - name: Run tests
        run: |
          cd frontend
          npm test -- src/store/graphStore.test.js
```

## Manual Testing

After running unit tests, perform these manual tests:

### Test Export
1. Start the application: `./start`
2. Open browser to http://localhost:3000
3. Click "Export Graph" button in header
4. Check browser console for `[Header]` logs
5. Verify JSON file downloads successfully
6. Open JSON file and verify structure

### Test Visualization Loading
1. Create a saved view (right-click canvas → "Save View")
2. Ask Claude to "show [view name]"
3. Check console for `[GetVisualization]` logs
4. Verify only content nodes are displayed (not the view node)
5. Verify positions are restored

### Test Node Addition
1. Search for some nodes
2. Add more related nodes
3. Verify existing nodes remain (not replaced)
4. Verify new nodes are highlighted
5. Verify edges between new and existing nodes appear
