# US-04 — Concept Harmonization & Cross-System Mapping: Implementation Plan

| **Status** | Draft |

## Context

US-04 (`docs/sprint_documentation/US-04 Concept-Harmonizer.md`) introduces a harmonization workflow on top of the existing WP12 Metadata Graph prototype.

The objective is to allow metadata experts and statistical producers to:

1. Discover variables across different statistical operations that measure the same real-world concept.
2. Compare associated definitions and codelists.
3. Detect semantic divergences automatically.
4. Generate AI-assisted concordance mappings.
5. Validate and persist harmonization decisions.
6. Track harmonization status across the statistical system.

The current platform already provides:

* NetworkX metadata graph storage
* GSIM-inspired metadata model
* LLM provider abstraction
* Chat-driven graph exploration
* Interactive graph visualization
* Configurable schema profiles

The major gaps are:

* No explicit Codes entities are considered in th model.
* No Mapping entity for harmonization proposals (Correspondence Tables).
* No codelist comparison service, (LLM should do it).
* No harmonization status tracking.
* No workflow for AI-generated concordance proposals and expert validation.

---

## Confirmed Design Decisions

### Concept abstraction

Introduce a first-class `Code` node type.

This allow the system to include the codes included in each codlists.

### Mapping representation

Use a dedicated `Mapping` node rather than embedding concordance payloads directly in a `RELATES_TO` edge.

Reasons:

* easier provenance management
* validation lifecycle support
* richer metadata
* future extensibility

### Harmonization status

Stored on Concept nodes:

```json
{
  "harmonization_status": "aligned"
}
```

Allowed values:

* aligned
* partial
* divergent

### Provenance

Stored on Mapping nodes:

```json
{
  "validated_by": "...",
  "validated_date": "...",
  "authority_reference": "..."
}
```

---

# Workstream 1 — Schema Extensions (`config/stockholmsprint/`)

## Add Code node type

Extend:

```text
config/stockholmsprint/schema_config.json
```

with:

```json
{
  "name": "Code",
  "category": "domain"
}
```

Suggested metadata:

* preferred_label
* definition

Suggested icon:

---

## Add Mapping node type

Represents proposed or approved concordances.

Metadata:

```json
{
  "status": "candidate",
  "mapping_type": "equivalence",
  "created_by": "...",
  "validated_by": null
}
```

Possible statuses:

* candidate
* validated
* rejected

---

## Add relationship types

```text
MEASURES_CONCEPT
USES_CODE_LIST
MAPS_TO
RECOMMENDS_ADOPTION
```

Examples:

```text
Variable → Concept
Mapping → CodeList
Mapping → CodeList
Operation → CodeList
```

---

## Seed harmonization example

Extend:

```text
config/stockholmsprint/graph.json
```

with:

### Concept

```text
Employment Status
```

### Variables

```text
EMPSTAT
SIT_LAB
COT_REG
```

all linked to:

```text
Employment Status Concept
```

### Codelists

```text
ILO Employment Status
National Legacy Employment Status
```

### Mapping

Candidate mapping node between both codelists.

This reproduces the scenario from the user story.

---

# Workstream 2 — Concept Discovery Engine

## New service module

```text
backend/service/concept_harmonizer.py
```

Responsibilities:

### Find variables by concept

Input:

```python
concept_name
```

Output:

```python
[
  variable,
  producer,
  codelist,
  definition
]
```

The service traverses:

```text
Concept
 ← MEASURES_CONCEPT
 Variable
```

---

## Add GraphStorage helper

In:

```text
backend/core/storage.py
```

Add:

```python
find_variables_by_concept(concept_id)
```

Returns:

```python
{
  "concept": ...,
  "variables": ...,
  "codelists": ...
}
```

Used by both UI and chat.

---

## Workstream 3 — AI-Assisted Concordance Generation

### Objective

Enable an LLM to analyse variables, concepts, definitions and codelists across statistical producers and generate candidate concordance mappings that support harmonization activities.

This workstream implements scenario steps 3 and 4.

### New service module

```text
backend/service/mapping_generator.py
```

The module reuses the existing LLM abstraction (`create_provider`) and receives:

* Concept definition
* Variable definitions
* Source codelist
* Target codelist
* Existing metadata context

### LLM Mapping Generation

The prompt asks the model to:

* Identify exact equivalences
* Identify partial equivalences
* Detect merged categories
* Detect split categories
* Detect unmapped categories
* Explain methodological implications
* Suggest convergence recommendations

Example prompt:

> Compare these two statistical classifications and propose a concordance table. Identify exact equivalences, partial equivalences, merged categories, split categories and unmapped categories. Explain any methodological implications and recommend possible harmonization actions.

### Mapping Output Structure

```json
{
  "mapping_rows": [
    {
      "source_code": "2",
      "target_code": "2",
      "equivalence": "partial",
      "confidence": 0.91,
      "reason": "Target category merges unemployed and inactive persons."
    }
  ],
  "summary": "...",
  "recommendations": [...]
}
```

### Candidate Mapping Creation

The generated concordance is persisted as a Mapping node:

```json
{
  "type": "Mapping",
  "status": "candidate",
  "created_by": "llm"
}
```

The graph displays the Mapping node between both codelists for expert review.

### Tools

```python
generate_mapping()
```

### Acceptance Verification

* User selects two codelists.
* LLM generates a concordance proposal.
* A candidate Mapping node appears in the graph.
* Partial equivalences and methodological warnings are clearly identified.

---

## Workstream 4 — Expert Review, Validation & Harmonization Persistence

### Objective

Allow metadata experts to review AI-generated concordances, validate or reject them, record governance decisions, and persist approved harmonization information in the graph.

This workstream implements scenario steps 5 and 6.

### Mapping Lifecycle

Supported statuses:

```text
candidate
reviewed
validated
rejected
```

### Review Workflow

```text
LLM
 ↓
Mapping (candidate)
 ↓
Metadata Expert Review
 ↓
Validated Mapping
 ↓
RELATES_TO relationship persisted
```

### Expert Actions

New GraphService operations:

```python
review_mapping(mapping_id)
validate_mapping(mapping_id)
reject_mapping(mapping_id)
```

### Review Metadata

Validated mappings store governance information:

```json
{
  "status": "validated",
  "reviewed_by": "...",
  "review_date": "...",
  "authority_reference": "...",
  "review_note": "...",
  "recommended_action": "Split merged category in future survey editions"
}
```

### Concordance Persistence

After validation:

1. Mapping node remains in the graph for provenance.
2. A RELATES_TO relationship is created between both codelists.
3. Recommendations are stored as metadata.
4. No codelist is removed.
5. Both classifications remain queryable.

### Harmonization Status

The system derives the Concept harmonization status from approved mappings.

Possible values:

```text
aligned
partial
divergent
```

Rules:

* aligned → all mappings are exact.
* partial → at least one approved partial equivalence exists.
* divergent → no approved mappings or major unresolved divergences remain.

### Tools

```python
validate_mapping()
reject_mapping()
get_harmonization_summary()
```

### Acceptance Verification

* Expert reviews candidate Mapping.
* Expert adds governance notes.
* Mapping is validated.
* RELATES_TO relationship is created.
* Provenance information is preserved.
* Harmonization status updates automatically.
* Both codelists remain available in the graph.


# Workstream 5 — Harmonization Status Engine

New module:

```text
backend/service/harmonization_status.py
```

Computes:

```text
aligned
partial
divergent
```

for each Concept.

### Rules

#### Aligned

All variables use equivalent codelists.

#### Partial

At least one approved mapping contains partial equivalences.

#### Divergent

No approved mapping exists or major structural differences remain.

### Aggregate query

Support:

```python
get_harmonization_summary()
```

Returns:

```json
{
  "aligned": 12,
  "partial": 5,
  "divergent": 3
}
```

Satisfies the final acceptance criterion.

---

# Workstream 6 — API & MCP Integration

To be done

# Workstream 7 — Frontend Harmonization View

To be done

# Verification

### 1. Concept discovery

Query:

> Show me all variables related to employment status.

Confirm:

```text
EMPSTAT
SIT_LAB
COT_REG
```

appear linked to the Employment Status Concept.

### 2. Divergence analysis

Compare:

```text
ILO codelist
vs
National Legacy codelist
```

Confirm merged categories are detected.

### 3. AI-generated mapping

Request:

> Suggest a mapping between the national legacy codelist and the ILO codelist.

Confirm candidate Mapping node is created.

### 4. Expert validation

Validate mapping.

Confirm provenance metadata is stored.

### 5. Persistence

Reload graph.

Confirm:

* Mapping node persists
* MAPS_TO relationship persists
* validation metadata persists

### 6. Harmonization status

Confirm Concept status updates:

```text
divergent → partial → aligned
```

based on mapping approvals.

### 7. Automated tests

To be done

---

Coverage:

* concept discovery
* codelist comparison
* mapping generation
* harmonization status rules
* validation workflow

---
