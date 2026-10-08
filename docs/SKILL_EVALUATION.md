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
| `post_write_verification` | **full** | After the last successful write, a read tool's result contained the written node's id. |
| `completeness` | **full** | The graph state after the run matches an enumerated expectation: exact field values, or the set of fields that must differ from the fixture. |
| `unsupported_entity_reference` | **full** | No node the answer cites is one the run never read — a fixture id cited without a tool result returning it, or an id-shaped token matching nothing. See the caveat below. |
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

It combines two signals of deliberately different character:

1. **Closed vocabulary, no false positives.** An id from the case's own fixture
   graph that the answer cites but no tool result returned. The model named a
   real node it never looked at. Unambiguous.
2. **Open vocabulary, biased towards misses.** An id-shaped token matching
   nothing the model was shown. "Id-shaped" is a UUID, or three-or-more
   hyphenated segments *none of which is an English function word* — because
   three segments alone also matches `up-to-date`, `end-to-end`,
   `state-of-the-art` and `one-size-fits-all`, and reporting one of those as a
   fabricated node id would be a false accusation a reader could not
   distinguish from a real finding. The cost is a known false negative: an id
   whose own segments include such a word (`task-fix-edge-auth-on-sspcloud`) is
   not flagged by this signal. That trade is deliberate and pinned by a test —
   a missed fabrication understates the problem, which is what a lower bound is
   for.

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

#### Where that variable can come from

Being precise about this, because the loose version of the claim is wrong and
worth knowing: the harness reads the **process environment**, and that is not
the same as "the shell only". Importing the assistant calls `load_dotenv()`
(`backend/ui/chat_logic.py`), which the harness triggers on every case, so an
untracked `.env` in the repository root **is** loaded into the environment and
**will** satisfy a `credential_ref`. That is how the application itself is
configured, and the harness neither adds nor can remove it.

So the guarantee is this, exactly:

- **No file in this repository contains a credential value**, and none may. The
  `gitleaks` CI job and
  `backend/evaluation/tests/test_no_credentials.py` both check it, and
  `ModelProfile` refuses to hold one.
- **The harness itself never reads a key from a file, never stores one, never
  logs one, and never defaults one.** It resolves `credential_ref` against
  `os.environ` at the moment a provider is built.
- **A `.env` you create is yours to manage.** It is gitignored, it works, and it
  is still a key at rest on disk. `.env.example` names these variables with
  empty values purely as a reminder of what to export. **Prefer exporting in
  your shell**, so the value never lands in a file at all.

One more thing that is read but unused: constructing the assistant reads ambient
`OPENAI_API_KEY`/`ANTHROPIC_API_KEY` into `ChatProcessor.default_api_key` (you
will see an `ANTHROPIC_API_KEY not found` log line during a run). The harness
bypasses that field entirely by injecting its own provider, so it cannot affect
a measurement — but it is read, and saying "nothing else is read" would be
false.

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
and the model's prose, which is what makes it safe to paste into an issue.

Before comparing two models, check `run_error` on each case. A failed provider
call is reported as a run error rather than as a case failure — otherwise an
endpoint that is simply down would read as a model that answered in prose and
called no tools, which is exactly the conflation this evaluation exists to
avoid.

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
must not do. `backend/evaluation/tests/test_no_credentials.py` pins that
guarantee, including that the whole shipped suite scores with every provider
variable cleared from the environment.

Every scorer is tested in both directions. A scorer that only ever returned
`True` would make every case pass against every model — reporting reliability
nobody verified, which would make the harness worse than not having one.
