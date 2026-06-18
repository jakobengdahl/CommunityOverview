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

# Workstream 3 — Codelist Divergence Analysis

## New comparison service

```text
backend/service/codelist_comparison.py
```

Responsibilities:

### Compare two codelists

Input:

```python
source_codelist
target_codelist
```

Output:

```python
{
  "aligned_codes": [],
  "missing_codes": [],
  "merged_codes": [],
  "split_codes": [],
  "divergence_score": ...
}
```

---

## Divergence detection rules

### Exact match

```text
ILO code 1
↔
National code 1
```

Status:

```text
aligned
```

### Many-to-one

```text
ILO 2
ILO 3
→ National 2
```

Status:

```text
partial
```

### Missing category

Status:

```text
divergent
```

The rules remain deterministic and testable.

---

# Workstream 4 — AI Mapping Generation

## LLM-assisted concordance generation

New module:

```text
backend/service/mapping_generator.py
```

Uses:

```python
create_provider()
```

from:

```text
backend/llm_providers.py
```

### Prompt inputs

Provide:

* Concept definition
* Source codelist
* Target codelist
* Variable definitions

Ask for:

* equivalences
* partial equivalences
* missing mappings
* methodological warnings

### Output structure

```json
{
  "mapping_rows": [
    {
      "source_code": "2",
      "target_code": "2",
      "confidence": 0.92,
      "equivalence": "partial"
    }
  ]
}
```

---

## Create candidate Mapping node

Generated mappings are persisted as:

```text
Mapping node
```

with status:

```text
candidate
```

No automatic approval.

---

# Workstream 5 — Harmonization Workflow

## Expert validation

New GraphService operations:

```python
validate_mapping(mapping_id)
reject_mapping(mapping_id)
```

### Validation metadata

When approved:

```json
{
  "status": "validated",
  "validated_by": "...",
  "validated_date": "...",
  "authority_reference": "..."
}
```

### Save concordance

Upon approval:

Create:

```text
CodeList
  ← MAPS_TO →
CodeList
```

and preserve:

```text
Mapping node
```

for provenance.

No codelists are removed.

### Recommendations

Metadata expert may add:

```json
{
  "recommended_for_future_use": true
}
```

to codelists or operations.

---

# Workstream 6 — Harmonization Status Engine

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

# Workstream 7 — API & MCP Integration

# Workstream 8 — Frontend Harmonization View


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


Coverage:

* concept discovery
* codelist comparison
* mapping generation
* harmonization status rules
* validation workflow

---


