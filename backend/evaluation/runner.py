"""
The runner: execute acceptance cases against a provider configuration.

The provider configuration is a ModelProfile (backend/config/model_profiles.py)
— provider, model, endpoint, and the *name* of the environment variable holding
the credential. That type already refuses to hold a secret value: credential_ref
must look like an environment variable name, and options carrying a
secret-looking key are rejected. The credential itself is read from the
environment at the moment a provider is built and is never stored by this
module, never written to a report, and never defaulted to anything.

Each case runs against its own fixture graph in a temporary file, through the
real ChatService — the same system prompt, tool definitions and tool-execution
loop the product uses — with a RecordingProvider wrapped around the configured
provider so the run can be scored afterwards.

This module performs no provider calls of its own. ``run_suite`` is only as
live as the provider handed to it; the tests hand it a mock.
"""

import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from backend.config.model_profiles import ModelProfile, create_provider_from_profile
from backend.evaluation.cases import AcceptanceCase, load_cases
from backend.evaluation.dimensions import (
    DIMENSIONS,
    reported_only_dimensions,
    unscored_dimensions,
)
from backend.evaluation.scoring import CaseScore, score_case
from backend.evaluation.transcript import RecordingProvider, RunTranscript
from backend.llm.llm_providers import LLMProvider

logger = logging.getLogger(__name__)

ProviderFactory = Callable[[ModelProfile], LLMProvider]


@dataclass
class SuiteResult:
    """Scores for one provider configuration across the case set."""

    profile_id: str
    provider: str
    model: str
    endpoint: Optional[str]
    scores: List[CaseScore] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for s in self.scores if s.passed)

    @property
    def total(self) -> int:
        return len(self.scores)

    @property
    def run_errors(self) -> int:
        """Cases that never produced a scorable run (provider or fixture failure)."""
        return sum(1 for s in self.scores if s.run_error)

    @property
    def total_latency_ms(self) -> float:
        return sum(s.latency_ms for s in self.scores)


def default_provider_factory(profile: ModelProfile) -> LLMProvider:
    """
    Build the configured provider, reading its credential from the environment.

    Deliberately no ``api_key_override``: a harness that accepts a literal key
    is a harness someone will eventually pass one to from a file.
    """
    return create_provider_from_profile(profile)


def build_skills_context(skill_paths: Sequence[Path]) -> Optional[str]:
    """
    Render fixture SKILL.md files the way the chat path receives them.

    This mirrors ``buildSkillsContext`` in
    ``frontend/web/src/components/ChatPanel.jsx`` — the only production caller
    that fills ``skills_context`` on a chat request, which is the parameter this
    harness drives. That shape is a header, one ``<skill name="…">`` block
    holding the SKILL.md **body only**, and a footer.

    It is deliberately NOT ``backend.agents.prompts.build_skills_section``,
    which is the AIAgent path: that one fences the set with ``--- SKILLS ---``
    and prefixes each skill with ``When to use:``, ``Description:`` and
    ``Expected tools:`` lines. Rendering the agent shape while driving the chat
    path would measure a prompt no production caller produces — and would hand
    the model a ``when_to_use`` line that the chat path does not inject at all
    whenever a skill has a body, which is every real SKILL.md. A skill fixture
    must therefore state its own applicability inside its body; the frontmatter
    is parsed for the name and otherwise not injected, exactly as in production.

    Fixtures are read from disk — the harness never fetches a skill over the
    network, which keeps the suite runnable with no egress beyond the provider.
    """
    if not skill_paths:
        return None

    parts = [
        "ACTIVE SKILL INSTRUCTIONS — YOU MUST APPLY THESE TO THIS RESPONSE:",
        "The user has selected the following skills. These instructions OVERRIDE "
        "your default behavior and style for this response. Apply them precisely.",
    ]
    for path in skill_paths:
        front, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        name = front.get("name") or path.stem
        parts.append(f'<skill name="{name}">\n{body.strip()}\n</skill>')
    parts.append("END OF SKILL INSTRUCTIONS. Apply the above to your entire response.")
    return "\n\n".join(parts)


def _split_frontmatter(raw: str) -> tuple:
    """Split a minimal ``---`` YAML frontmatter block from the markdown body."""
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    front: Dict[str, str] = {}
    for line in parts[1].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        front[key.strip()] = value.strip().strip("\"'")
    return front, parts[2]


def run_case(
    case: AcceptanceCase,
    profile: ModelProfile,
    provider_factory: ProviderFactory = default_provider_factory,
    graphs_dir: Optional[Path] = None,
    skills_dir: Optional[Path] = None,
) -> CaseScore:
    """Run one case against one provider configuration and score it."""
    fixture_graph = json.loads(case.graph_path(graphs_dir).read_text(encoding="utf-8"))
    skills_context = build_skills_context(case.skill_paths(skills_dir))

    try:
        recorder = RecordingProvider(provider_factory(profile))
    except Exception as exc:
        # A credential that is unset, or revoked between cases. The CLI checks
        # credentials up front, but a library caller has no such gate and one
        # bad profile must not take the other cases' results down with it.
        transcript = RunTranscript(
            run_error=f"provider unavailable: {type(exc).__name__}: {exc}"
        )
        logger.warning("case %s: provider could not be built: %s", case.id, exc)
        return score_case(case, transcript, [], fixture_graph)

    transcript = recorder.transcript

    with tempfile.TemporaryDirectory(prefix="skill-eval-") as tmpdir:
        graph_file = Path(tmpdir) / "graph.json"
        graph_file.write_text(json.dumps(fixture_graph), encoding="utf-8")

        try:
            chat_service, tool_definitions = _build_chat_service(graph_file)
        except Exception as exc:
            # A fixture the loader accepted as JSON but the graph layer rejects.
            # Scored as a run error so one bad case does not lose the suite.
            transcript.run_error = f"fixture setup failed: {type(exc).__name__}: {exc}"
            logger.warning("case %s could not be set up: %s", case.id, exc)
            return score_case(case, transcript, [], fixture_graph)

        # The "before" state a completeness expectation is judged against must be
        # the fixture AS THE GRAPH LAYER SERIALIZES IT, not the raw JSON file.
        # The two differ in both directions: the serializer adds defaults the
        # fixture omits (subtypes: [], aliases: [], metadata: {}) and drops keys
        # it does not model (communities). Compared against the raw file, either
        # difference reads as a field the model changed — so a case naming such a
        # field passed on a run where the model did nothing at all. Snapshotting
        # through the same serializer that produces the final state removes the
        # whole class, rather than enumerating the fields it affects.
        baseline_graph = _snapshot_graph(chat_service)
        if not baseline_graph:
            # Falling back to the raw fixture here would quietly restore the
            # baseline whose shape mismatch was the completeness defect in the
            # first place, so a case would score against it without anyone
            # knowing. Without a trustworthy baseline there is no measurement.
            _shutdown_chat_service(chat_service)
            transcript.run_error = "could not snapshot the fixture graph as a baseline"
            logger.warning("case %s: baseline snapshot failed", case.id)
            return score_case(case, transcript, tool_definitions, {})

        try:
            result = chat_service.process_message(
                messages=[{"role": "user", "content": case.prompt}],
                skills_context=skills_context,
                llm_provider=recorder,
            )
            transcript.final_text = result.get("content") or ""
        except Exception as exc:
            # A refusal, a malformed response, a broken fixture: the case still
            # scores, as a run_error, rather than taking the whole suite down.
            transcript.run_error = f"{type(exc).__name__}: {exc}"
            logger.warning("case %s failed to run: %s", case.id, exc)
        finally:
            # Snapshot from memory while the graph is still live, then tear the
            # storage down inside the temp directory. Writes go to a background
            # executor, so leaving the directory first makes a queued write fail
            # against a path that no longer exists — and every case would leak a
            # thread pool, which a suite of cases times providers.
            transcript.final_graph = _snapshot_graph(chat_service)
            _shutdown_chat_service(chat_service)

    # ChatProcessor.process_message catches every exception and returns the
    # error as the assistant's reply, so a failed provider call never reaches
    # the except above. Left there, an endpoint that is simply down would score
    # as a model that answered in prose and called no tools — an integration
    # problem reported as a model limitation, which is the one conflation this
    # evaluation exists to avoid. The recorded call carries the evidence.
    provider_error = next(
        (call.error for call in transcript.provider_calls if call.error), None
    )
    if provider_error and not transcript.run_error:
        transcript.run_error = f"provider call failed: {provider_error}"

    return score_case(case, transcript, tool_definitions, baseline_graph)


def _build_chat_service(graph_file: Path):
    """Create a ChatService over a fixture graph, and its advertised tool set."""
    from backend.core import GraphStorage
    from backend.service import GraphService
    from backend.ui import ChatService

    storage = GraphStorage(str(graph_file))
    chat_service = ChatService(GraphService(storage))
    return chat_service, list(chat_service._processor.tool_definitions)


def _snapshot_graph(chat_service) -> Dict[str, Any]:
    """Read the graph state after the run, in-memory rather than off disk."""
    try:
        exported = chat_service.graph_service.export_graph()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not snapshot the graph: %s", exc)
        return {}
    return exported if isinstance(exported, dict) else {}


def _shutdown_chat_service(chat_service) -> None:
    """Drain and stop the fixture graph's storage; never fail a case over it."""
    try:
        chat_service.graph_service.storage.shutdown_events()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not shut the fixture storage down cleanly: %s", exc)


def run_suite(
    profile: ModelProfile,
    cases: Optional[Sequence[AcceptanceCase]] = None,
    provider_factory: ProviderFactory = default_provider_factory,
    graphs_dir: Optional[Path] = None,
    skills_dir: Optional[Path] = None,
) -> SuiteResult:
    """Run every case against one provider configuration."""
    case_list = list(cases) if cases is not None else load_cases()
    scores = [
        run_case(
            case,
            profile,
            provider_factory=provider_factory,
            graphs_dir=graphs_dir,
            skills_dir=skills_dir,
        )
        for case in case_list
    ]
    return SuiteResult(
        profile_id=profile.id,
        provider=profile.provider,
        model=profile.model,
        endpoint=profile.endpoint,
        scores=scores,
    )


def build_report(result: SuiteResult) -> Dict[str, Any]:
    """
    Render a suite result as a JSON-serializable report.

    Carries scores, the tool-call sequence, latency and tokens — never the
    prompts, the system prompt or the model's prose. Keeping those out is what
    makes it safe to commit or paste a report: nothing a run was configured with
    can leak through it, and the credential was never in the transcript to
    begin with.
    """
    dimension_rows = {
        key: {
            "title": dim.title,
            "mechanically_scored": dim.mechanical.value,
            "caveat": dim.caveat,
        }
        for key, dim in DIMENSIONS.items()
    }

    cases = []
    for score in result.scores:
        usage = score.usage
        cases.append(
            {
                "case_id": score.case_id,
                "dimension": score.dimension,
                "passed": score.passed,
                "run_error": score.run_error,
                "conditions": [
                    {"name": c.name, "passed": c.passed, "detail": c.detail}
                    for c in score.conditions
                ],
                "dimensions": {
                    key: {
                        "scored": d.scored,
                        "passed": d.passed,
                        "mechanically_scored": d.mechanical.value,
                    }
                    for key, d in score.dimensions.items()
                },
                "tool_calls": score.tool_calls,
                "provider_calls": score.provider_calls,
                "latency_ms": round(score.latency_ms, 1),
                "tokens": {
                    "reported": usage.reported,
                    "prompt": usage.prompt_tokens,
                    "completion": usage.completion_tokens,
                    "total": usage.total_tokens,
                },
            }
        )

    return {
        "profile": {
            "id": result.profile_id,
            "provider": result.provider,
            "model": result.model,
            "endpoint": result.endpoint,
        },
        "summary": {
            "cases": result.total,
            "passed": result.passed,
            # Surfaced beside `passed` on purpose: a run that never reached the
            # model is not a model that failed, and a reader comparing two
            # providers on the pass count alone would read an outage as a
            # quality difference.
            "run_errors": result.run_errors,
            "total_latency_ms": round(result.total_latency_ms, 1),
            "unscored_dimensions": unscored_dimensions(),
            "reported_only_dimensions": reported_only_dimensions(),
        },
        "dimensions": dimension_rows,
        "cases": cases,
    }


def load_profiles(path: Path) -> List[ModelProfile]:
    """
    Load provider configurations from a JSON file of ModelProfile objects.

    ModelProfile's own validators reject a credential value in credential_ref or
    in options, so a file that tries to carry a key fails to load rather than
    being quietly honoured.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("profiles", [])
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON array of model profiles")
    return [ModelProfile.model_validate(item) for item in raw]
