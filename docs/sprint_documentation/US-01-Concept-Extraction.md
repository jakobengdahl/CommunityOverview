# User Story: US-01 — Concept Extraction and Graph Matching

| Attribute | Value |
| :--- | :--- |
| **Skill** | Document parsing and analysis + metadata graph matching |
| **Actor** | Statistical Producer / Statistical Analyst |
| **Status** | Work in Progress |

## Buisness Value
When developing a new survey or introducing new questions, statistical producers typically begin by drafting questionnaires and supporting documentation. At a later stage, these concepts, variables, value domains, and relationships must be documented and integrated into the organisation's metadata system.

This process can be time-consuming because it requires identifying whether similar concepts, variables, or structures already exist in the metadata repository and determining how new content should be aligned with the existing metadata model.

By analysing questionnaire documents and comparing their content to the existing metadata graph, the AI assistant can help statistical producers identify reusable structures, discover relevant metadata, and understand how new content could be integrated into the metadata system. This reduces manual effort and supports more consistent metadata documentation.

## User story

As a statistical producer, I want to upload a questionnaire or statistical documentation and identify concepts, variables, and structures that match existing metadata, with support from an interactive graph and an AI assistant, so that I can reuse existing metadata where appropriate and efficiently integrate new content into the metadata system in a way that is consistent with the organisation's metadata model (e.g. GSIM-based).

## User Scenario

1. Upload and Analysis

The statistical producer uploads a questionnaire or other statistical documentation in PDF or Word format. The AI assistant analyses the document and extracts candidate metadata elements, such as concepts, variables, value domains, question groups, and units of observation.

2. Metadata Matching

The AI assistant compares the extracted elements with the existing metadata graph and identifies relevant matches, related structures, and potential reuse opportunities. Suggested matches are accompanied by confidence scores and explanations.

3. Visual Exploration and Explanation

The identified matches are presented in an interactive graph where the statistical producer can explore related metadata structures, inspect relationships between existing and newly identified elements, and ask the AI assistant questions about the suggested matches and their rationale.

4. Integration Guidance

Based on the identified matches and graph patterns, the AI assistant suggests how the extracted concepts and structures could be represented in the metadata graph, including opportunities to reuse existing metadata and recommendations for new nodes or relationships where needed.

5. Review and Confirmation

The statistical producer reviews the proposed mappings and integration suggestions and instructs the AI assistant which metadata elements and relationships should be added, modified, or ignored before integration into the metadata system.


## Acceptance Criteria

Document Analysis

- PDF and DOCX documents can be uploaded.
- Concepts, variables, value domains, and question groups are extracted from the document.
- The origin of extracted content is retained.

Metadata Matching

- Extracted elements are matched against the metadata graph.
- Suggested matches include confidence scores and explanations.
- The user can inspect why a match was proposed.

Graph Exploration

- Matching nodes and relationships are visualised in the graph.
- Users can navigate from suggested matches to related existing metadata.
- Users can select nodes and ask follow-up questions.

Metadata Integration Support

- The AI assistant suggests how new content can be integrated into the existing metadata model.
- Suggestions distinguish between reused metadata and newly proposed metadata.
- The user remains in control of which additions are accepted.


## Prototype Validation Scenario

### Objective

Evaluate whether the AI assistant can identify reusable metadata structures from questionnaire documents and suggest how they can be integrated into an existing metadata graph.

### Steps

1. Use the base dataset available under: config/stockholmsprint/
2. Search for:
**"Labour Force Surveys (LFS)"** in `STATISTICALPROGRAMME`.

3. Expand the graph to display the following structure:
QUESTIONNAIRE → QUESTIONNAIRE COMPONENT → INSTANCE VARIABLE → VALUE DOMAIN

4. Select and highlight a related group of questions as a reference pattern.

5. Upload the document:
*Swedish Labour Force Survey (LFS) – Questionnaire* (https://www.scb.se/contentassets/c12fd0d28d604529b2b4ffc2eb742fbe/lfs_questionnaire_240312.pdf)

6. Ask the AI assistant:
  > Find another group of questions in the uploaded document that resembles the selected pattern and suggest how it should be represented in the metadata graph.


### Expected Outcome

The AI assistant:

- Identifies a structurally similar question group.
- Maps relevant concepts, variables, and value domains.
- Suggests how the structure could be represented in the metadata graph.
- Explains the rationale behind the suggested mapping.

### Evaluation Criteria

- Does the AI identify structurally similar question groups?
- Are the suggested mappings consistent with the metadata model?
- Are reuse opportunities correctly identified?
- Is the explanation understandable and useful for a statistical producer?
- Does the proposed structure support efficient metadata documentation?
