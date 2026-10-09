# Skill-Execution Evaluation Harness

A provider-agnostic harness for measuring how reliably the built-in AI assistant
loads a skill, follows it, and executes tools — with the same graph, prompts,
skills and acceptance cases across any model you point it at.

Its purpose is to separate **model limitations** from **prompt, tool-schema and
integration problems**. When the assistant misbehaves on a mid-size open model,
the useful question is whether a larger model behaves differently on the exact
same inputs. Everything here exists to make that comparison repeatable.

The harness is the measurement apparatus. **It ships no results**: running it
against a real provider needs a credential, and credentials are the operator's
to supply from their own shell.

| | |
|---|---|
| Code | `backend/evaluation/` |
| Tests | `backend/evaluation/tests/` (provider always mocked) |
| Cases and fixtures | `backend/evaluation/fixtures/` |
| Entry point | `scripts/run_skill_eval.py` |

---

## What it measures, and how honestly

Each dimension carries how mechanically it can be scored, in one of four
classes: **full** (a predicate over the recorded run), **partial** (only part of
the dimension is a predicate), **reported** (measured objectively but with no
pass condition — a number you compare, which the harness never passes or fails
a provider on), and **not scored**.

The table is maintained in code — `backend/evaluation/dimensions.py` — and a
test parses the table below and compares it against that one, so the two cannot
drift apart. `python scripts/run_skill_eval.py --dimensions` prints it with the
full caveats.

| Dimension | Scored | What the score actually means |
|---|---|---|
| `tool_call_validity` | **full** | Every requested tool was advertised in that run, and its arguments validate against that tool's `input_schema`. |
| `id_resolution` | **full** | Every node/edge id passed to a write or relationship tool had appeared in an earlier tool result. A *correct* id that was never read back still fails — the model guessed and got lucky. |
| `post_write_verification` | **full** | After the last successful write, a read tool's result contained the written node's id. A *presence* check, so defined only for writes that leave the node readable — see "Adding a case". |
| `completeness` | **full** | The graph state after the run matches an enumerated expectation: exact field values, or the set of fields that must differ from the fixture. |
| `unsupported_entity_reference` | **full** | No node the answer cites is one the run never read — a fixture id cited without a tool result returning it, or a token that passes every id disqualifier and matches nothing. Two false-negative classes, both deliberate; see the caveat below. |
| `latency` | **reported** | Wall-clock time around each provider call, summed. A number to compare, with no pass condition. |
| `token_profile` | **reported** | Prompt and completion tokens as the provider reports them, or explicitly *unreported*. A number to compare; tokens only — no monetary cost. |
| `skill_adherence` | **partial** | Only rules expressible as tool-call-sequence constraints. Prose-level adherence is not scored. |
| `skill_selection` | **partial** | A proxy: with two skills injected and only one applicable, each mandating a different first call. |
| `hallucination` | **not scored** | Deliberately. See below. |

### Why `hallucination` is not scored

**This harness reports no hallucination rate, and that is a decision, not a
gap in the implementation.**

What counts as a hallucination in free prose is a methodology question. A
plausible-sounding definition picked by whoever wrote the code would produce a
confident percentage resting on an unstated judgement — and a reader months
later cannot tell such a number apart from a real measurement. That is worse
than reporting nothing, because it is actionable while being unfounded.

What *is* implemented is narrower and separately named:
`unsupported_entity_reference`. It is a **strict lower bound** on hallucination:
it catches a node reference the run never read, and nothing else. It will not
catch a false claim about a node that genuinely exists, and it does not check
node *names* — there is no mechanical way to tell a cited node name from a noun
phrase that happens to repeat one.

It is a **pure negative**, which matters for how a case declares it: an answer
citing nothing cites nothing unsupported, so it passes. That is the right
reading of the condition and the wrong reading of a case — the shipped case
asks for a node id "exactly as stored", and a model that ran one valid read and
then said it could not tell was scoring green. So a case that asks for an id
declares `answer_cites_ids` alongside it: the ids the answer must state. The
negative says nothing fabricated; the positive says the question was answered.
A case measuring a *correct refusal* is asserting the opposite of both and
needs a condition this harness does not have.

It combines two signals of deliberately different character:

1. **Closed vocabulary, no false positives.** An id from the case's own fixture
   graph that the answer cites but no tool result returned. The model named a
   real node it never looked at. Unambiguous.
2. **Open vocabulary, biased towards misses.** A token that survives every
   disqualifier below and still matches nothing the model was shown. A UUID
   qualifies on shape alone; anything else must:

   - have **three or more** hyphenated segments;
   - have **no segment that is an English function word** — three segments
     alone also matches `up-to-date`, `end-to-end`, `state-of-the-art` and
     `one-size-fits-all`;
   - **not be entirely numeric** — `2026-10-08` is a date, and a model states
     today's date freely;
   - **share its leading segment with an id the run has actually seen** —
     otherwise `gpt-4o-mini`, `left-hand-side` and `read-only-mode` all qualify
     on shape. Only id-shaped values donate a prefix, so an ordinary tag such
     as `open-data` does not make `open-source-first` look like a node.

   Reporting any of those as a fabricated node id would be a false accusation a
   reader could not distinguish from a real finding, which is why each
   disqualifier is there — and each one buys a false negative. **Two classes are
   missed, and the second is the larger:** an id whose own segments include a
   function word (`task-fix-edge-auth-on-sspcloud`), and a fabrication under a
   leading segment the run never saw. Both are deliberate: a missed fabrication
   understates the problem, which is what a lower bound is for, while a false
   accusation would corrupt the measurement. The first is pinned by a test; the
   second is a property of the prefix rule.

**To get a hallucination rate, the methodology decision has to be made first**:
what classes of false statement count, who adjudicates them, and how
inter-rater agreement is established. Once that is written down it can be added
as a dimension. Until then, read the transcripts.

### Why `skill_selection` is a proxy

The assistant does not choose between skills. Skills are injected into the
system prompt by the caller (`skills_override` in `backend/ui/chat_logic.py`),
so there is no selection step inside the system to measure.

What the harness measures instead: two skills are injected, only one applies to
the prompt, and each mandates a *different* distinctive first tool call. The
first call then discriminates. This tells you whether the model applies the
right injected skill — not whether some retrieval step picked the right skill
to inject.

One such case would prove nothing: a model with a fixed favourite first call
would pass it without reading either skill. They therefore come in pairs,
sharing the same injected skills and expecting different first calls, and the
pairing is pinned by a test.

One detail matters for writing such a pair. The chat path injects a skill's
**body only** — it falls back to `when_to_use` just for a skill that has no
body, which no real SKILL.md is. So a fixture skill has to state when it
applies *inside its body*, or the proxy measures a signal the model was never
given. The shipped pair does (`schema-explainer.md` says "for a schema
question", `inventory-reporter.md` says "for an inventory question"), and a test
pins it.

### Why `skill_adherence` is partial

A SKILL.md rule is scored only when it can be written as a constraint on the
tool-call sequence — a required ordered subsequence, or calls that must not
appear. "Explain your reasoning" and "state your uncertainty" are not scored
and a case must not claim they are. Write such a rule as a sequence constraint,
or read the transcript yourself.

The strongest adherence case in the shipped set is
`skill-adherence-ambiguous-name-halts`: two nodes share a name, and the skill
requires the model to stop and report both rather than pick one. Writing to
either fails, even though the user would probably have been happy with the
choice. Instructions that cost the model the ability to finish the task are the
ones worth measuring.

---

## The acceptance cases

Nine cases in `backend/evaluation/fixtures/cases.json`, covering every
pass/fail-scored dimension. `latency` and `token_profile` are measured on every
case rather than by a case of their own, since neither has a pass condition;
`token-profile-multi-step-traversal` carries `token_profile` as its nominal
dimension and exists to force a multi-turn tool-chaining loop, so those two
numbers describe realistic agentic work on at least one case instead of a single
round trip. A test asserts that every scored dimension is either some case's
dimension or one of those two.

A case is a fixture graph, a prompt, the skills to inject, and an expectation
stated as a predicate over the recorded run. Nothing is scored by reading the
model's prose.

The protocol rules in `fixtures/skills/graph-maintenance-protocol.md` come from
the reliability problems seen on a mid-size open model in pilot use. Three of
the four are exercised by cases: ID-first execution, mandatory post-write
verification, and comprehensive object inspection.

**Discrepancy handling ships unexercised**, and deliberately so rather than by
oversight: the rule only triggers "if the user disputes a change you reported",
and every case is a single user turn (`runner.py` sends one user message, and
`AcceptanceCase` has no field for a second). Measuring it needs a multi-turn
case format — a worthwhile extension, but one that changes the case model, so it
is not smuggled in here. The rule stays in the skill because the model should
follow it in production; just do not read a passing suite as evidence about it.

### Adding a case

Edit `fixtures/cases.json`; no code change is needed. The loader refuses a case
that declares no pass condition, names an unknown dimension, claims a dimension
whose conditions it does not declare, or points at a fixture that does not
exist — all before any provider call is made.

Available expectation fields are documented on `ExpectedBehaviour` in
`backend/evaluation/cases.py`. A field left unset is not checked, and the
dimension it would have scored is reported as **unscored** rather than passing.

Two constraints worth knowing:

- **Only tools the assistant actually advertises.** `get_node_details` and
  `get_graph_stats` are executable in `ChatService`'s tools map but are *not* in
  `ChatProcessor.tool_definitions`, so the model is never offered them. A case
  requiring one would fail for a reason that says nothing about the model. A
  post-write read-back can therefore only come through `search_graph`,
  `get_related_nodes` or `find_similar_nodes`. A test pins this.
- **Completeness must be enumerable.** If you cannot write down what "complete"
  means as field values or changed fields, the request is not a case for this
  harness.
- **`verify_after_write` is a presence check.** It asks whether a read after the
  write *returned* the written node, so it is defined only for writes that leave
  the node readable: `add_nodes`, `update_node`, `unarchive_nodes`,
  `unarchive_edges`. Verifying a *removal* means reading back and finding the
  node **absent** — the opposite test — so a case whose writes are deletes or
  archives is reported as mis-specified rather than as a model failure. If you
  need that measured, it wants its own condition, not this one.
- **Fields that cannot evidence a change are refused.** `id`, `created_at` and
  `updated_at`: the first two never change and the last changes on every write,
  so a completeness expectation naming one reports something other than whether
  the requested change was made.

---

## Running it

### 1. Describe the providers

A provider configuration is a `ModelProfile`
(`backend/config/model_profiles.py`, documented in
[PROFILES.md](PROFILES.md#model-profiles)). Write a JSON file — keep it outside
the repository, or at least out of version control:

```json
[
  {
    "id": "openai-gpt4o",
    "name": "OpenAI GPT-4o",
    "provider": "openai",
    "model": "gpt-4o",
    "default": true,
    "credential_ref": "SKILL_EVAL_OPENAI_API_KEY"
  },
  {
    "id": "open-model",
    "name": "Open model on an OpenAI-compatible endpoint",
    "provider": "openai",
    "model": "<the model name the endpoint serves>",
    "endpoint": "<the endpoint's /v1 base URL>",
    "credential_ref": "SKILL_EVAL_OPEN_MODEL_API_KEY"
  }
]
```

`credential_ref` is the **name of an environment variable**, never a key.
`ModelProfile` rejects a value that does not look like a variable name, and
rejects a secret-looking value in `options`, so a file that tries to carry a key
fails to load rather than being quietly honoured.

Exactly one profile must be `"default": true`.

An open model served over an OpenAI-compatible endpoint uses
`"provider": "openai"` with `endpoint` set — that is how the existing provider
layer already reaches self-hosted and managed inference services (see
[LLM_PROVIDERS.md](../LLM_PROVIDERS.md)).

### 2. Export the credentials

In your own shell. Use exactly the variable names your profiles reference:

```bash
export SKILL_EVAL_OPENAI_API_KEY=...          # the OpenAI key
export SKILL_EVAL_OPEN_MODEL_API_KEY=...      # the open-model endpoint's key
```

The harness reads **only** the variable each profile names. There is no
fallback to `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`: a harness that fell back
would attribute one endpoint's behaviour to another and bill a key you did not
choose. An unset variable is a clear error naming the variable, before any case
runs.

#### Export in your shell — a `.env` is unreliable here, not merely discouraged

**Via the CLI, a key in `.env` does not work.** `check_credentials()` gates every
profile before anything imports the assistant, so the `load_dotenv()` that reads
`.env` (`backend/ui/chat_logic.py`) has not run, and you get
`environment variable … is not set` with exit 2.

**Via `run_case()`/`run_suite()` as a library, whether it works depends on
process state** — which is worse than a flat no. The provider is built before
the assistant, so in a fresh process the first case refuses. But importing the
assistant runs `load_dotenv()` once, and from then on the repo-root `.env` *is*
in `os.environ`, so a later case or a second profile in that same process will
accept it. Same input, different answer depending on what ran first.

This file has stated this wrongly twice — first that nothing but the shell is
read, then that `.env` simply works — so to be exact:

- **No file in this repository contains a credential value**, and none may. The
  `gitleaks` CI job and `backend/evaluation/tests/test_no_credentials.py` both
  check it, and `ModelProfile` refuses to hold one.
- **The harness never reads a key from a file, never stores one, never logs one,
  and never defaults one.** It resolves `credential_ref` against `os.environ` at
  the moment a provider is built, and passes no `api_key_override` by any route.
- **`os.environ` is not the same as your shell.** `load_dotenv()` is the
  application's own configuration mechanism and the harness neither adds nor can
  remove it. A repo-root `.env` reaches `os.environ` once the assistant is
  imported.
- **So export in your shell.** `.env.example` names these variables with empty
  values purely as a reminder of what to export. A value in `.env` is a key at
  rest on disk whose effect here depends on import order — the worst of both.

One thing that is read but cannot affect a measurement: constructing the
assistant reads ambient `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` into
`ChatProcessor.default_api_key`. The harness bypasses that field entirely by
injecting its own provider — but it is read, so a flat "nothing else is read"
would be false.

### 3. Run

```bash
# What is measured and how, with full caveats
python scripts/run_skill_eval.py --dimensions

# Every profile in the file
python scripts/run_skill_eval.py --profiles ~/skill-eval-providers.json

# One profile, report to a file
python scripts/run_skill_eval.py \
    --profiles ~/skill-eval-providers.json \
    --profile-id open-model \
    --out ~/skill-eval-open-model.json
```

The script takes **no API key argument**, by design: a key on a command line
lands in shell history and in the process table.

### 4. Read the report

The report carries per-case scores, the tool-call sequence, provider-call count,
latency and tokens. It deliberately **excludes** the prompts, the system prompt
(including the injected skill text) and the assistant's answer text.

It is not free of model-authored strings altogether, and should not be read as
if it were: a condition's `detail` explains *why* it failed, so it can quote a
field value the model wrote or an argument it passed — that is the diagnosis.
Those quotes are length-bounded, but if a run's inputs are sensitive, read the
report before pasting it somewhere.

What the report does *not* carry, by construction, is the credential, the
prompts, or the injected skill text. The one surface that could have carried
any of them was `run_error`, because an exception message belongs to whoever
raised it and a provider error can echo the request it failed on — including
its `Authorization` header. So `run_error` names the stage and the exception
class (`provider call failed: APIConnectionError`) and never the message.

The message goes to the run log instead, so the detail is in your terminal
rather than in a file you may commit or paste. The harness scrubs the
credential, the prompt and the injected skill text out of its own log lines by
value. Two limits are worth knowing, because the report's guarantee does not
rest on either:

- The scrub matches an exact substring. That closes the credential, which is
  one opaque token an SDK echoes verbatim. For the prompt and the skill text it
  only fires on a byte-for-byte echo — a JSON-escaped or truncated one is not
  matched.
- The product's chat layer logs a swallowed exception at `ERROR` before the
  harness sees it, and the harness cannot reach that line. So if a run fails
  against a provider whose errors echo request headers, treat the whole run log
  as sensitive, not just the part the harness wrote.

Before comparing two models, check `run_error` on each case. A failed provider
call is reported as a run error rather than as a case failure — otherwise an
endpoint that is simply down would read as a model that answered in prose and
called no tools, which is exactly the conflation this evaluation exists to
avoid. The exception class is usually enough to tell a down endpoint from a
crash; when it is not, the run log has the rest.

Also check `tokens.reported`. An OpenAI-compatible endpoint need not report
usage; when it does not, the figure is `null` and explicitly unreported, never
zero.

---

## What this repository will not hold

Cost and token profile as a **measurement capability** belong here. Pricing,
packaging, and any conclusion about what to charge or which provider to buy do
not: this repository is public, and those are commercial decisions that live in
private planning. The harness therefore reports tokens and latency and derives
no monetary figure.

Likewise, no private deployment hostnames or tenant names belong in a case, a
fixture or a profile file committed here. Keep your profile file outside the
repository.

---

## For developers

### Design

Everything the scorers need is observable at the `LLMProvider` boundary, so the
harness records there rather than instrumenting the assistant:

- the tool calls the model asked for are the `tool_use` blocks the provider
  **returns**;
- the tool results it saw are the `tool_result` blocks the provider **receives**
  on the following turn (`chat_logic` re-sends the growing history);
- latency and usage are measured around, and read off, each call.

`RecordingProvider` wraps any `LLMProvider` and produces a `RunTranscript`. Each
case runs against its own copy of its fixture graph in a temporary directory,
through the real `ChatService` — the product's system prompt, tool definitions
and tool-execution loop, with only the provider replaced.

Skill injection mirrors the **chat** path, which is the one the harness drives:
`build_skills_context` reproduces the wrapper that
`frontend/web/src/components/ChatPanel.jsx` builds for `skills_context`, not
`backend.agents.prompts.build_skills_section`, which is the AIAgent path and has
a different shape. Nothing mechanically couples the Python to the JSX, so a test
asserts the marker strings still appear in both and names the other file if one
side changes.

`ChatProcessor.process_message` accepts an optional `llm_provider` that bypasses
provider resolution. The harness needs it: profiles configured on the host would
otherwise take precedence, and the run would silently measure the host's default
model while reporting under the model it was asked to test. Nothing reaches that
parameter from an HTTP request.

### Tests

```bash
python -m pytest backend/evaluation/ -q
```

Every test uses a scripted provider. **No test needs an API key and none makes a
network call** — a suite that only passes with a real credential cannot run in
CI, and making a credential necessary to check this harness is the one thing it
must not do. `backend/evaluation/tests/test_no_credentials.py` pins that,
including that the whole shipped suite scores with every provider variable
cleared from the environment.

The no-network half is **enforced**, not merely asserted: an autouse
`no_network` fixture in `backend/evaluation/tests/conftest.py` fails any test
that attempts an outbound connection, with no opt-out marker. It had to be
added, because the claim was false for several rounds — a test meaning to
substitute the provider factory patched a module attribute that `run_suite` had
already captured as a default argument, so the real provider was built and the
suite made 27 outbound connections while documenting that it made none. A
guarantee about what a suite does *not* do is worth exactly as much as the thing
that stops it.

Every scorer is tested in both directions. A scorer that only ever returned
`True` would make every case pass against every model — reporting reliability
nobody verified, which would make the harness worse than not having one.
