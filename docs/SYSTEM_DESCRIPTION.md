# System Description: Community Knowledge Graph

## 1. Introduction and Motivation

### 1.1 Purpose
The **Community Knowledge Graph** is a Proof of Concept (PoC) designed to facilitate knowledge sharing, resource optimization, and collaboration within public sector communities (e.g., government agencies, municipalities).

### 1.2 Motivation
In large ecosystems like the public sector, multiple actors often work on similar initiatives or require similar capabilities without being aware of each other. This leads to:
- **Overlapping investments:** Redundant projects solving the same problem.
- **Siloed knowledge:** Valuable resources (reports, software) remain hidden.
- **Missed synergies:** Opportunities for collaboration are lost.

### 1.3 Goal
The system aims to create a shared, visual, and interactive "map" of the ecosystem. By modeling relationships between **Actors**, **Initiatives**, **Resources**, and **Legislation**, the system allows users to:
- **Visualize** the landscape of ongoing work.
- **Discover** gaps and potential partners.
- **Avoid** duplication by checking for existing initiatives before starting new ones.
- **Interact** naturally using AI to query the graph (e.g., "Who is working on AI guidelines?").

---

## 2. Architecture

The system follows a modern **Client-Server** architecture enhanced with **Retrieval-Augmented Generation (RAG)** capabilities.

### 2.1 High-Level Overview

```mermaid
graph TD
    User[User] <--> Frontend[Frontend (React/Vite)]
    Frontend <-->|HTTP JSON| Backend[Backend (Python MCP Server)]
    Backend <-->|API| LLM[Anthropic Claude API]
    Backend <-->|Read/Write| Storage[Graph Storage (JSON + Pickle)]

    subgraph "Frontend Layer"
        Visualization[Graph Visualization (React Flow)]
        ChatUI[Chat Interface]
        State[State Management (Zustand)]
    end

    subgraph "Backend Layer"
        FastMCP[FastMCP Server]
        GraphEngine[NetworkX Graph Logic]
        VectorStore[Vector Embeddings (SentenceTransformers)]
        DocProcessor[Document Processor (PDF/Docx)]
    end
```

### 2.2 Core Design Principles
1.  **Model-Driven:** All data adheres to a strict metamodel (Actors, Initiatives, etc.) to ensure consistency.
2.  **AI-Assisted:** The LLM (Claude) acts as the intelligent interface, translating natural language into graph operations.
3.  **Human-in-the-Loop:** AI proposes changes (additions/deletions), but the user must explicitly approve them.
4.  **Privacy-Aware:** Vector embeddings and graph data are stored locally (or in a controlled database), with personal data warnings in place.

---

## 3. System Components

### 3.1 Frontend (Client)
Built with **React** and **Vite**, the frontend provides the user interface for visualization and interaction.

*   **Graph Visualization:** Uses **React Flow** to render nodes and edges. It supports:
    *   Color-coded node types (e.g., Blue for Actors, Green for Initiatives).
    *   Interactive navigation (Zoom, Pan, Drag).
    *   Filtering by Community or Node Type.
*   **Chat Interface:** A conversational UI where users send messages to the backend. It renders Markdown responses and interactive elements (like "Approve Proposal" buttons).
*   **State Management:** Uses **Zustand** to synchronize the graph state between the visualization and the chat components.
*   **API Service:** Handles communication with the backend endpoints.

### 3.2 Backend (Server)
Built with **Python** and **FastMCP**, the backend serves as the brain of the operation.

*   **MCP Server:** Exposes tools (functions) that the LLM can call. It handles HTTP requests from the frontend.
*   **Graph Storage:**
    *   **NetworkX:** Manages the graph structure in memory for efficient traversal and algorithm execution.
    *   **Persistence:** Saves the graph structure to `graph.json` and vector embeddings to `embeddings.pkl`.
*   **Vector Search:** Uses **SentenceTransformers** (`sentence-transformers`) to generate semantic embeddings for nodes, enabling "fuzzy" matching and duplicate detection.
*   **Document Processor:** Uses `pymupdf` (PDF) and `python-docx` (Word) to extract text from uploaded files for analysis.
*   **Chat Processor:** Manages the conversation flow, maintaining context and invoking the appropriate tools based on user intent.

### 3.3 External Services
*   **Anthropic Claude API:** The Large Language Model (LLM) used for:
    *   Understanding user queries (Natural Language Understanding).
    *   Extracting structured data from unstructured text/documents.
    *   Deciding which MCP tools to call (Tool Use).

---

## 4. Component Connections & Interfaces

The Frontend and Backend communicate via a REST-like API over HTTP (default port `8000`).

### 4.1 API Endpoints
*   **`POST /chat`**: The primary interaction point. Receives user messages, forwards them to the LLM (with tool definitions), executes any tools called by the LLM, and returns the final response.
*   **`POST /upload`**: Accepts file uploads (multipart/form-data). It parses the file and returns the extracted text to the client, which then sends it to the chat endpoint for analysis.
*   **`POST /execute_tool`**: Allows the frontend to execute specific backend functions directly, bypassing the LLM (used for UI-driven actions like "Save View" or "Force Layout").

---

## 5. Functional Description

This section details the key functions of the system and how they contribute to the workflow.

### 5.1 Graph Visualization & Exploration
*   **Visual Rendering:** Nodes are rendered with specific colors and icons based on their type (Metamodel). Edges show the relationship type (e.g., `BELONGS_TO`, `IMPLEMENTS`).
*   **Filtering:** Users can hide specific node types or focus on specific communities (e.g., "Show only 'Myndigheter'").
*   **Drill-down:** Clicking a node reveals detailed metadata and allows the user to expand related nodes ("Show 1 hop neighbors").

### 5.2 Intelligent Search (RAG)
*   **Natural Language Query:** Users can ask questions like "Are there any projects related to NIS2?".
*   **Mechanism:**
    1.  The backend uses **`search_graph`** (keyword) or **vector search** to find relevant nodes.
    2.  The relevant graph sub-section is passed to the LLM as context.
    3.  The LLM generates an answer based on the graph data.

### 5.3 Knowledge Ingestion (Add/Upload)
*   **Text Extraction:** Users can upload PDFs or Word documents. The system extracts text using the **Document Processor**.
*   **Entity Extraction:** The LLM analyzes the text to identify potential **Nodes** (Initiatives, Actors, etc.) and **Relationships**.
*   **Duplicate Detection:**
    *   The system runs **`find_similar_nodes`** using both string similarity (Levenshtein) and semantic similarity (Vector Embeddings).
    *   It warns the user if a proposed node looks like a duplicate of an existing one.
*   **Approval Workflow:** The AI proposes a set of nodes/edges. The user sees a structured preview and must click "Approve" to commit changes to the graph (`add_nodes`).

### 5.4 Graph Management (CRUD)
*   **Add:** `add_nodes` inserts new entities and connections.
*   **Update:** `update_node` modifies properties of existing nodes (e.g., changing a status or description).
*   **Delete:** `delete_nodes` allows removal of outdated or incorrect information.
    *   **Safety:** Requires explicit confirmation and limits deletion to 10 nodes at a time to prevent accidental data loss.

### 5.5 Visualization Views
*   **Save/Load:** Users can save the current visual layout (positions, visible nodes) as a named "View" (e.g., "Regulatory Landscape 2024").
*   **Mechanism:** The backend stores the view metadata as a `VisualizationView` node, while the frontend handles the restoration of positions.

### 5.6 Statistics
*   **Dashboard:** Provides real-time metrics on the graph, such as the total number of nodes, distribution by type, and community activity levels. This is powered by the `get_graph_stats` tool.
