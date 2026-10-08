"""
Skill-execution evaluation harness.

Runs a fixed set of acceptance cases against the built-in AI assistant using
any provider configuration — an OpenAI model, an OpenAI-compatible endpoint
serving an open model, or Claude — and scores what the run did, so that a
difference in skill reliability can be attributed to the model rather than to
the prompt, the tool schemas or the integration.

The harness builds the measurement; it does not carry results. Running it
against a real provider needs a credential, which is the owner's to supply from
their own shell — see docs/SKILL_EVALUATION.md.
"""

from backend.evaluation.cases import AcceptanceCase, ExpectedBehaviour, load_cases
from backend.evaluation.dimensions import (
    DIMENSIONS,
    Dimension,
    Mechanical,
    mechanically_scored_dimensions,
    unscored_dimensions,
)
from backend.evaluation.runner import (
    SuiteResult,
    build_report,
    build_skills_context,
    load_profiles,
    run_case,
    run_suite,
)
from backend.evaluation.scoring import (
    CaseScore,
    ConditionResult,
    DimensionScore,
    score_case,
)
from backend.evaluation.transcript import (
    ProviderCall,
    RecordingProvider,
    RunTranscript,
    TokenUsage,
    ToolCall,
)

__all__ = [
    "AcceptanceCase",
    "CaseScore",
    "ConditionResult",
    "DIMENSIONS",
    "Dimension",
    "DimensionScore",
    "ExpectedBehaviour",
    "Mechanical",
    "ProviderCall",
    "RecordingProvider",
    "RunTranscript",
    "SuiteResult",
    "TokenUsage",
    "ToolCall",
    "build_report",
    "build_skills_context",
    "load_cases",
    "load_profiles",
    "mechanically_scored_dimensions",
    "run_case",
    "run_suite",
    "score_case",
    "unscored_dimensions",
]
