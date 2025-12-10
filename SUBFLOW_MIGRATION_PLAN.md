# Plan: Migrera från shapes-layer till React Flow Subflows

## Problem
De nuvarande rektanglarna/grupperna implementeras som en separat `shapes-layer` ovanför React Flow, vilket ger:
- Z-index problem (visas antingen över eller under noder)
- Delay/lag när man flyttar view (shapes uppdateras efter React Flow)
- Svårare att interagera med shapes jämfört med noder
- Ingen integrerad funktionalitet med React Flow

## Lösning: React Flow Subflows
Använd React Flow's inbyggda `parentId` och `type: 'group'` funktionalitet.

## Implementation Plan

### Steg 1: Skapa GroupNode Component
✅ Skapa `GroupNode.jsx` - En React Flow node av typ 'group'
- Använd `Handle` för edge connections
- Editera titel inline
- Färgväljare med gradient-knapp
- Delete-knapp

### Steg 2: Uppdatera graphStore.js
- Ta bort: `shapes`, `addShape`, `updateShape`, `deleteShape`, `addNodeToShape`
- Gruppering hanteras genom node.parentId istället

### Steg 3: Uppdatera VisualizationPanel.jsx

#### 3.1 Ta bort shapes från store
```javascript
// FÖRE:
const { shapes, addShape, addNodeToShape } = useGraphStore();

// EFTER:
// (inga shape-references)
```

#### 3.2 Uppdatera nodeTypes
```javascript
const nodeTypes = useMemo(() => ({
  custom: CustomNode,
  group: GroupNode,  // NY
}), []);
```

#### 3.3 Uppdatera handleAddRectangle (context menu)
```javascript
const handleAddRectangle = useCallback(() => {
  if (!reactFlowInstance || !contextMenu) return;

  const viewport = reactFlowInstance.getViewport();
  const bounds = reactFlowWrapper.current.getBoundingClientRect();

  const x = (contextMenu.x - bounds.left - viewport.x) / viewport.zoom;
  const y = (contextMenu.y - bounds.top - viewport.y) / viewport.zoom;

  // Skapa en ny group node
  const newGroupNode = {
    id: `group-${Date.now()}`,
    type: 'group',
    position: { x, y },
    data: { label: 'New Group' },
    style: {
      width: 300,
      height: 200,
      backgroundColor: 'rgba(59, 130, 246, 0.1)',
      borderColor: '#3B82F6',
    },
  };

  // Lägg till noden till React Flow
  reactFlowInstance.addNodes(newGroupNode);

  setContextMenu(null);
}, [contextMenu, reactFlowInstance, reactFlowWrapper]);
```

#### 3.4 Ta bort shapes-layer rendering
```javascript
// TA BORT:
<div className="shapes-layer">
  {shapes.map(shape => (
    <ShapeRectangle ... />
  ))}
</div>
```

#### 3.5 Ta bort shape-relaterade callbacks
- `handleNodeDrag` - behövs inte längre (React Flow hanterar detta)
- `onNodeDragStop` för shape association - ersätt med parentId logic

#### 3.6 Implementera node-till-group association
```javascript
const handleNodeDragStop = useCallback((event, node) => {
  // Hitta om noden droppades inuti en group node
  const nodes = reactFlowInstance.getNodes();
  const groupNodes = nodes.filter(n => n.type === 'group');

  for (const group of groupNodes) {
    const nodeInGroup = isNodeInsideGroup(node, group);
    if (nodeInGroup) {
      // Sätt parentId för att associera node med group
      reactFlowInstance.setNodes((nds) =>
        nds.map((n) => {
          if (n.id === node.id) {
            return {
              ...n,
              parentId: group.id,
              extent: 'parent', // Håll noden inuti gruppen
              // Konvertera position till relativt koordinatsystem
              position: {
                x: node.position.x - group.position.x,
                y: node.position.y - group.position.y,
              },
            };
          }
          return n;
        })
      );
      break;
    }
  }
}, [reactFlowInstance]);
```

### Steg 4: Uppdatera CSS

#### 4.1 Ta bort shapes-layer styles från VisualizationPanel.css
```css
/* TA BORT:
.shapes-layer { ... }
.shapes-layer > * { ... }
*/
```

#### 4.2 Lägg till group node styles
```css
/* GroupNode.css redan skapad med korrekta styles */

.react-flow__node-group {
  min-width: 200px;
  min-height: 150px;
  /* React Flow hanterar z-index automatiskt */
}
```

### Steg 5: Ta bort gamla filer
- `ShapeRectangle.jsx` - inte längre behövs
- `ShapeRectangle.css` - inte längre behövs

### Steg 6: Uppdatera handleSaveView
Ta bort shapes från metadata, de sparas automatiskt som group nodes.

```javascript
const handleSaveView = async (name) => {
  // ... existing code ...

  const viewNode = {
    name: name,
    type: 'VisualizationView',
    metadata: {
      node_ids: nodeIds,
      positions: positions,
      hidden_node_ids: hiddenNodeIds,
      // REMOVE: shapes: shapes
    },
    communities: []
  };
};
```

## Fördelar med denna approach
✅ Ingen z-index konflikt - React Flow hanterar det automatiskt
✅ Ingen delay - allt är integrerat i React Flow
✅ Smidigare interaktion - samma system som noder
✅ Inbyggd support för nested groups (group i group)
✅ Nodes kan enkelt flyttas in/ut ur groups med drag & drop
✅ Grupper kan ha edges precis som vanliga noder
✅ Automatisk serialization vid spara/ladda

## Testing Checklist
- [ ] Högerklick skapar group node
- [ ] Group node kan editeras (titel, färg)
- [ ] Group node kan tas bort
- [ ] Drag node över group associerar dem (parentId)
- [ ] Nodes stannar inuti group när gruppen flyttas
- [ ] Group kan ändra storlek
- [ ] Nested groups fungerar (group i group)
- [ ] Spara view inkluderar groups korrekt
- [ ] Ladda view återställer groups korrekt
- [ ] Inga z-index problem
- [ ] Ingen delay vid zoom/pan

## Estimerad arbetsinsats
~2-3 timmar för fullständig implementation och testning
