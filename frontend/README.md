# Community Knowledge Graph Frontend

React-baserad frontend med graf-visualisering och chat-interface.

## Funktioner

### Layout
- **Header:** Community dropdown för att välja aktiva communities
- **Chat Panel (40%):** Konversationellt interface med Claude API
- **Visualization Panel (60%):** React Flow graf-visualisering

### Komponenter
- **Header** - Community-selector med multi-select
- **ChatPanel** - Chat-meddelanden och input
- **VisualizationPanel** - React Flow graf med zoom/pan/select
- **CustomNode** - Anpassade noder med färgkodning och [+]-knapp

### State Management
- **Zustand** för global state (graf-data, chat-messages, communities)

## Installation

```bash
npm install
```

## Utveckling

```bash
npm run dev
```

Öppnar på http://localhost:3000

## Build

```bash
npm run build
```

## URL-parametrar

```
?community=eSam&community=Myndigheter
```

Sätter aktiva communities vid initial load.

## Metamodell Färgkodning

- **Actor** - Blue (#3B82F6)
- **Community** - Purple (#A855F7)
- **Initiative** - Green (#10B981)
- **Capability** - Orange (#F97316)
- **Resource** - Yellow (#FBBF24)
- **Legislation** - Red (#EF4444)
- **Theme** - Teal (#14B8A6)
- **VisualizationView** - Gray (#6B7280)

## TODO

- [ ] Integrera Claude API för chat
- [ ] Implementera MCP-anrop från frontend
- [ ] Dokumentuppladdning
- [ ] Bättre graf-layout-algoritm
- [ ] "Visa relaterade noder" funktionalitet
- [ ] Loading states och error handling
