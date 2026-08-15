# resume-agent

Matches a candidate against a job description, grades the evidence requirement
by requirement, and — when it's a match — writes a tailored resume in which
every bullet traces back to a fact the candidate actually supplied.

**Status:** M0 (pipeline) and M1 (web app, persistence, human review gate) are
built. Grader calibration against the gold set is the open work — see
[Evaluation](#evaluation).

- [Architecture](#architecture) — the full end-to-end map
- [Web app](#web-app) · [Setup](#setup) · [Evaluation](#evaluation)
- [Gotchas already paid for](#gotchas-already-paid-for) — nine bugs, and why they happened

## Web app

```bash
cd resume-agent && ./.venv/bin/uvicorn app.web:app --reload --port 8000
```

One Python service — pipeline, store and UI. Server-rendered Jinja with vanilla
JS polling: no build step, no CDN, works offline.

- **`/`** — paste or upload a profile and a JD (PDF, DOCX, TXT, MD, image)
- **`/runs/{id}`** — live progress, then the evidence scorecard, verdict, open
  questions, gaps and adjacent roles
- **the review gate** — when the verifier can't trace a claim, the resume is
  withheld until a human ticks each one. Unticked blockers keep it blocked.
- **`/profiles/{key}`** — the accumulated career graph and every application made
  against it

Runs execute in a worker thread (30–60s, ~7 model calls) with status persisted
to SQLite, so a page refresh or a second browser sees the same state.

Profiles are keyed by the email/phone rule with alternate-key lookup, so the
data model is multi-tenant before there is any login. Auth is a wrapper to add
later, not a migration.

## Setup

```bash
cd resume-agent && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

Get a free Gemini key at <https://aistudio.google.com/apikey> (no card, ~30s)
and a free Groq key at <https://console.groq.com/keys>, then:

```bash
cp .env.example .env
```

Fill in `GEMINI_API_KEY` and `GROQ_API_KEY`. Run:

```bash
./.venv/bin/python cli.py --profile-file data/profiles/sample_resume.md --jd-file data/jds/sample_jd.md
```

Both sides accept `--profile-text` / `--profile-file` and `--jd-text` /
`--jd-file` (PDF, DOCX, TXT, MD). Pass `--email` or `--phone` when the document
has no contact details.

## Architecture

### The shape of the thing

```
                        ┌─ typed text ─┐
  intake_profile ───────┤              ├──> Document ──> build_career_graph ──┐
                        └─ file ───────┘                  (LLM: extract)      │
                          pdf/docx/txt/md/img                                 │
                                                                              ├──> match
  intake_jd ────────────┤ same ├──────────> Document ──> parse_jd ────────────┘  (LLM: match)
                                                          (LLM: parse)            │
                                                                                  ▼
                                                              compute_verdict — deterministic
                                                                                  │
             ┌────────────────────────────────┼────────────────────────────────┐
             ▼                                ▼                                ▼
       not_matching                       partial                          strong
             │                                │                                │
        gap_report                       gap_report ───────────────────────────┤
        (LLM: gaps)                      (LLM: gaps)                           │
             │                                └────────────┬───────────────────┘
            END                                            ▼
                                                    select_facts   (LLM: rank)
                                                            ▼
                                                       tailor      (LLM: write)
                                                            ▼
                                                       verify      (LLM: refute — different
                                                            │       family, JD hidden)
                                            ┌───────────────┴───────────────┐
                                     blockers?                          clean
                                            │                               │
                              human review gate                          render
                              (M1 web UI / --accept-flags)          + ats_lint
                                            │                       + provenance
                                            └──────────────> render ────────> END
```

**Seven LLM calls per full run.** Everything else is Python.

Deterministic edges throughout. This is not an agent loop and shouldn't become
one — a resume pipeline that takes a different path each run is a bug, not a
feature. See [Agents](#agents) for the honest accounting of what is and isn't
agentic here.

### Four decisions that shape everything

**The Career Graph is the primitive, not the resume.** `app/schemas/career.py`
holds atomic fact atoms with stable ids. Tailoring is selection and rewriting
over those atoms, never re-expansion of an already-lossy resume PDF — you can't
recover what the PDF threw away. It's also the stable prompt prefix across every
application one candidate makes: keep it first in the prompt and providers with
caching make applications 2..n a fraction of the first.

**The verdict is deterministic Python, not a model score.** The model grades
evidence per requirement; `app/tools/verdict.py` turns those grades into a
verdict by fixed rule. That's what makes it explainable ("you failed r_02"),
stable across runs, and testable against a gold set. A 0–100 score from a model
gives you none of those three.

**The verifier is adversarial, cross-family, and blind to the JD.** It runs on a
different model family because a model asked to check its own work shares its
own blind spots. It never sees the job description — a verifier that knows what
the text was optimised for rationalises its stretches instead of catching them.
Context starvation is doing real correctness work.

**Anything computable is computed.** Years of experience, keyword coverage, page
budget, date ranges — all Python. LLMs get overlapping employment spans wrong in
ways that look entirely plausible.

### Framework choices

| Layer | Choice | Why |
|---|---|---|
| Orchestration | **LangGraph** | Typed state, explicit conditional edges, and — the reason it earns its place — checkpointing for the human review interrupt |
| Contracts | **Pydantic v2** | Schema at every node boundary; validation failures drive the model retry |
| Model routing | **LiteLLM** | One call path across Gemini / Groq / Ollama; swap providers by config, not code |
| Web | **FastAPI + Jinja** | One Python service. Vanilla JS polling — no build step, no CDN, works offline |
| Store | **SQLite (WAL)** | Postgres-shaped schema; the worker thread and request thread both write |
| Eval | **pytest + JSONL** | Free, no vendor. `run_gold.py` adds a confusion matrix |

Deliberately **not** used: LangChain chains/agents (abstraction tax, and the
prompt *is* the product here), vector DBs (a whole career fits in context — RAG
would be complexity with no payoff), CrewAI/AutoGen (multi-agent chat is the
wrong shape for a deterministic pipeline).

---

### Nodes

Each is a plain function `state -> partial state update`, in `app/nodes/`.

| Node | Module | LLM | In → Out |
|---|---|---|---|
| `intake_profile` | `intake.py` | — | text/file → `Document` |
| `intake_jd` | `intake.py` | — | text/file → `Document` |
| `build_career_graph` | `extract.py` | `extract` | `Document` → `CareerGraph` + resolved `Identity` + computed years |
| `parse_jd` | `extract.py` | `parse` | `Document` → `JobSpec` (requirements, gates, vocabulary) |
| `match` | `matching.py` | `match` | graph + job → `Scorecard` rows, then deterministic verdict |
| `gap_report` | `matching.py` | `match` | weak rows → `Gap[]` + `adjacent_roles[]` |
| `select_facts` | `generate.py` | `tailor` | scorecard → ranked `fact_ids` within a page budget |
| `tailor` | `generate.py` | `tailor` | selected atoms → `TailoredResume` (bullets carry `fact_ids`) |
| `verify` | `generate.py` | `verify` | atoms + resume (**no JD**) → `VerifyReport` |
| `render` | `rendering.py` | — | resume → HTML/PDF + provenance log, gated on blockers |

`match` is also the **join** of the two parallel branches, so it's where an
upstream failure on either side is caught. Three guards live there: upstream
errors, an empty career graph, and a job spec with no gradable requirements —
each returns an error rather than a misleading verdict.

### Skills

A *skill* here is a versioned prompt module: instruction + output schema + model
binding, loaded from `app/skills/*.md` so prompts change without touching Python.
These are the highest-churn artifacts in the system — when quality moves, it
moves because one of these changed.

| Skill | Used by | The rule that matters most |
|---|---|---|
| `extract_profile.md` | `build_career_graph` | **Never invent.** No metric in the source → `metrics` stays empty. One claim per atom. Grade ownership honestly (`led`/`contributed`/`assisted`) — resume language inflates |
| `parse_jd.md` | `parse_jd` | `gate` only for genuine disqualifiers. "5+ years **required**" is a gate; "**preferred**" is a must. Split compound requirements. Flag boilerplate ("team player") so filler can't sink a candidate |
| `match_grader.md` | `match` | Grades `direct`/`adjacent`/`transferable`/`none`/`unknown`, each **citing atom ids**. A grade with no citation is invalid. A skills-list mention is a claim, not a demonstration. When torn, take the lower grade |
| `select_facts.md` | `select_facts` | Rank before writing, so the writer never pads. Coverage over redundancy; never leave a role with zero bullets |
| `tailor.md` | `tailor` | Every bullet carries its `fact_ids` and asserts nothing they don't contain. Don't lead the skills list with a qualified skill. Use the computed years figure, never the JD's minimum |
| `verify.md` | `verify` | Assume the resume is wrong until the facts show otherwise. **Specific→general is accurate** (EKS → "Kubernetes"), general→specific is not |
| `gap_report.md` | `gap_report` | Classify `dealbreaker`/`significant`/`coachable` honestly. On a hard no, name 3–5 roles this profile *would* fit — often the most useful output |

### Tools — the deterministic layer

`app/tools/`. Governing principle: **anything computable is computed, never
generated.** All unit-tested.

| Module | Functions | Notes |
|---|---|---|
| `identity.py` | `normalize_email`, `normalize_phone`, `resolve_identity`, `merge_identities`, `lookup_keys` | Email primary, phone fallback. **Both retained as alternate keys** so a later upload with only one reconciles instead of forking a second profile. Gmail dots deliberately *not* canonicalised — wrongly merging two people is worse than failing to merge one |
| `dates.py` | `parse_month`, `years_of_experience`, `graph_years_of_experience` | Overlapping roles are **merged, not summed** — two concurrent jobs are 5 years, not 10. LLMs get this wrong plausibly |
| `documents.py` | `from_text`, `from_file`, `load_input` | PDF/DOCX/TXT/MD/image → one `Document`. Detects a scanned PDF (empty text layer) instead of silently extracting 40 characters. Reads DOCX **tables** — resumes hide whole roles there |
| `keywords.py` | `keyword_coverage`, `resume_to_text` | Word-bounded matching ("Go" must not hit "Django"). Stuffing needs high density **and** ≥4 repetitions **and** a document long enough for density to mean anything |
| `verdict.py` | `compute_verdict`, `strongest_hooks` | The rule: any failed gate → `not_matching`; >2 absent musts → `not_matching`; ≥80% coverage with none absent → `strong`; ≥50% → `partial`. Boilerplate and unscorable categories leave the denominator |

### Hooks

Middleware around every model call, in `app/hooks/`. Kept as plain functions so
the call path in `models.py` stays readable end to end.

| Hook | When | What it does |
|---|---|---|
| `pii.redact` / `restore` | before / after every call | Swaps names, emails, phones, URLs for placeholders. The model doesn't need real PII to grade evidence. Mitigation, **not** a compliance story — employment history is itself identifying |
| `models.validate_or_retry` | after every call | Pydantic parse; on failure, feeds the validation error back and retries once |
| `cost.log_cost` | after every call | Per-node token ledger. You want this before the first bill, not after |
| `guardrail.block_on_unresolved_flags` | before `render` | **Hard gate.** Raises `RenderBlocked` while any verifier blocker is unresolved. Structural, not a policy someone remembers |
| `audit.write_audit_log` | after `render` | Persists every bullet → `fact_id` edge to `provenance.json`, flagging orphans |

### Agents

Honest accounting: **there are currently no agents in the strict sense** — no
component chooses its own tool sequence or loops until satisfied. Calling the
nodes "agents" would be marketing. Two components are agent-*shaped*:

- **The verifier** — runs on a different model family, in a deliberately starved
  context (no JD), with an adversarial instruction. Isolation is a correctness
  mechanism, not an implementation detail.
- **The document extractor** — a fallback ladder (text layer → multimodal →
  ask the user to paste) rather than a single path.

The one genuinely agentic component in the *design* is the **enrichment
interviewer** (M2, not built): it decides which scorecard gaps are worth asking
about, phrases each question, judges whether the answer resolved the gap, and
stops on its own. It needs a hard turn cap or it will interview people forever.

### Model routing

`app/config.py` + `app/models.py`. Every call goes through `complete_json()`,
which is where the hooks hang.

```
extract   gemini-3.7-flash    native PDF/image, cheap, structured output
parse     gemini-3.7-flash    low judgement
match     gemini-3.7-flash    highest-judgement node — first to upgrade on a paid key
tailor    gemini-3.7-flash    user-visible quality
verify    groq/llama-3.3-70b  DIFFERENT FAMILY, deliberately
```

Prompt layout is deliberate: `[system][career graph ← stable][JD ← varies]`.
The graph is identical across every application one candidate makes, so keeping
it first and unchanged makes it the cacheable prefix.

**Failover rotates models before sleeping.** Free-tier quotas are *per model*
("limit: 20, model: gemini-3.7-flash"), so when one is exhausted a sibling is
usually free. Sleeping on the primary first wastes a minute to learn what the
next model answers instantly. One full pass with no sleep, then back off
honouring the provider's own `retryDelay`.

**Output ceilings are per node** (`MAX_TOKENS_BY_NODE`). Too low truncates the
career graph mid-array; too high fails every Groq call, because Groq bills
`max_tokens` against your tokens-per-minute budget.

**The quota tracker is per-provider and self-clearing.** See
[Gotchas](#gotchas-already-paid-for) — the obvious implementation bricks the app.

### Data contracts

`app/schemas/` — Pydantic at every boundary.

| Schema | Key types |
|---|---|
| `career.py` | `FactAtom` (id, type, raw_text, skills, `Metric[]`, `Scope`, `evidence_strength`, `confidence`), `Identity`, `CareerGraph` |
| `job.py` | `Requirement` (kind: gate/must/nice/implicit, category, vocab, boilerplate), `JobSpec` |
| `match.py` | `ScorecardRow` (grade + `evidence_fact_ids` + rationale), `Gap`, `Scorecard`, `GRADE_WEIGHT`, `UNSCORABLE_CATEGORIES` |
| `resume.py` | `Bullet` (**text + fact_ids**), `ExperienceBlock`, `TailoredResume`, `VerifyFlag`, `VerifyReport` |
| `document.py` | `Document` (source_type, raw_text, extraction_method, confidence, warnings) |

`Bullet.fact_ids` is the single field that makes provenance, verification and
the audit log possible.

### Persistence & execution (M1)

`app/store.py` — SQLite in WAL mode, Postgres-shaped schema.

- `profiles` keyed by the identity rule; `profile_keys` maps every alternate key
  to one profile, so the data model is **multi-tenant before there is any login**
- `runs` holds the full state (job, scorecard, resume, verify report, artifacts)
  so a refresh or a second browser sees the same thing
- `applications` is **deliberately unused** — outcome data ("did this get a
  reply?") is what tells you whether your verdicts are honest, and it cannot be
  backfilled

`app/runner.py` — runs take 30–60s, so they execute in a worker thread with
status transitions (`queued → running → needs_review → done/failed`) persisted
for polling. `resolve_and_render` handles the second half of the human review:
it calls the render node **directly** rather than re-invoking the graph, because
a fresh run would generate a *different* resume whose claims no longer match the
ones just accepted.

### Repository layout

```
app/
  graph.py         the whole pipeline, readable in one screen
  state.py         typed LangGraph state (note the append reducers)
  config.py        per-node model routing + fallback chain
  models.py        complete_json(): the single call path, where hooks hang
  preflight.py     credential check — a clear message, not a stack trace
  schemas/         Pydantic contracts at every node boundary
  skills/          versioned prompt modules — highest-churn artifacts
  nodes/           one file per stage
  tools/           deterministic; unit-tested
  hooks/           PII redaction, cost ledger, render guardrail, provenance
  templates/       ats_clean.html.j2 + web/ (Jinja UI)
  store.py         SQLite persistence, identity-keyed
  runner.py        background execution + status
  web.py           FastAPI routes
evals/
  test_*.py        84 tests, no API calls
  run_gold.py      gold-set runner + confusion matrix
  gold/            22 labelled pairs, 8 profiles × 13 JDs (synthetic)
cli.py             the M0 entry point
data/              gitignored — never commit a real resume
```

## Evaluation

Five layers, cheapest first. Layers 1 and 5 run with **no API calls at all**.

| Layer | What it checks | Cost | Where |
|---|---|---|---|
| 1. Unit | Identity resolution, date math, keyword coverage, the verdict rule, the quota tracker | free | `test_identity/dates/keywords/verdict/quota.py` |
| 2. Plumbing | Fan-out, join, routing, render guardrail, provenance — models stubbed | free | `test_pipeline.py` |
| 3. **Verdict agreement** | Does the grader match human labels? **The eval that matters** | ~43 calls cold | `run_gold.py`, `test_gold.py` |
| 4. Hallucination | Unsupported claims per resume; target zero | live | *not yet built* |
| 5. Verifier calibration | Should-flag vs shouldn't-flag pairs | — | *not yet built* |

```bash
./.venv/bin/python -m pytest evals/ -q        # 84 tests, no API calls
```

Layers 4 and 5 are genuinely missing. The verifier has been tuned twice by
reading single outputs — exactly the guessing an eval exists to replace.

### The gold set

The eval that actually matters. 22 hand-labelled `(profile, JD)` pairs built
from 8 synthetic candidates against 13 job descriptions — the same candidate
appears against several roles, so the set tests *discrimination*, not just
recognition (Dev is a `strong_match` for the research role and `not_matching`
for the production MLE role; Priya is strong for the Python payments role and
partial for the Go one).

```bash
./.venv/bin/python evals/run_gold.py                 # confusion matrix
./.venv/bin/python evals/run_gold.py --offline       # no API calls at all
./.venv/bin/python evals/run_gold.py --resume        # after a quota stop
./.venv/bin/python evals/run_gold.py --only priya_x_meridian_go
RUN_GOLD=1 ./.venv/bin/python -m pytest evals/test_gold.py -v   # same, in CI
```

Three things keep this affordable on a free tier:

1. **It stops at the verdict** — 3 calls per case, not 7. Tailoring and
   verification are a separate concern with their own eval.
2. **Extractions and JD parses are cached by content hash**, so 8 profiles
   across 22 cases costs 8 extractions, not 22. Cache invalidates when a
   fixture changes.
3. **Scorecard rows are cached separately from the verdict.** The verdict is
   deterministic Python over those rows, so every change to a threshold, to
   gate handling, or to the unknown/unscorable logic re-scores the whole set
   **instantly and for free** via `--offline`. Only a change to the *grader
   prompt* genuinely needs models again.

22 cases ≈ 43 calls cold, 22 warm, 0 offline. Results append to
`.cache/results.jsonl` as each case lands, so a run killed by a rate limit
keeps everything it finished — `--resume` picks up where it stopped.

Each case carries a `tests` field naming what it probes. Several are deliberate
traps that earlier versions failed:

| Case | Trap |
|---|---|
| `priya_x_meridian_go` | "Go (basic)" in a skills list must not satisfy "strong Go" |
| `rohan_x_junior_frontend` | "degree OR equivalent (bootcamps welcome)" must not fail a bootcamp grad |
| `arjun_x_senior_frontend` | "5+ years **preferred**" is a must-have, not a gate |
| `kavya_x_meridian_go` | deep payments domain must not carry a PM into an engineering role |
| `meera_x_ml_engineer` | data engineering is not ML engineering, even next to an ML team |
| `imran_x_appsec` | profile has **no email** — key must fall back to the E.164 phone |
| `priya_x_platform_sre` | threshold probe: sits near the partial/not-matching cutoff |

Error weighting is asymmetric on purpose: over-generous verdicts fail the
build, over-strict ones xfail. Sending a candidate into an application they
cannot win costs them more than an arguable rejection they can inspect.

### Current baseline — 2026-08-15, `gemini-3.5-flash`

```
expected \ actual    no   partial  strong        Agreement      18/22 (82%)
no                   10      ·        ·          Over-generous   0
partial               2      2        ·          Over-strict     4
strong                ·      2        6          Errored         0
```

**Every error is in the conservative direction.** Nothing was sent to a
candidate as a strong match that wasn't one — the failure mode that costs them
an application and their trust never fired.

The first run of this set scored **59%** with 9 over-strict verdicts. One cause
dominated: 13 of 14 `location` gates failed, because "Must be located in India
(Remote)" grades `unknown` — no resume states willingness to relocate. Adding
`location` to `UNSCORABLE_CATEGORIES` took it to 82%, re-scored offline from
cached grades at zero API cost.

The remaining four are margin cases, not bugs: two sit 3–4 points under a
threshold (77% vs 80%, 46% vs 50%), one has a genuinely unevidenced must-have,
and one is a label worth re-examining (`meera_x_stellar_python` fails a "5+
years **backend**" gate on 6 years of *data* engineering — the same
discipline-specific reasoning already accepted for `priya_x_platform_sre`).

**Thresholds have deliberately not been tuned to close that gap.** Moving a
cutoff to fit four cases out of 22 is overfitting, and it would trade away the
zero-over-generous property that matters most.

**Known gap:** only 5 of 22 cases are `partial_match`, the hardest class — and
3 of the 4 remaining errors involve it. Weight new cases toward it.

Fixtures are synthetic — never commit a real person's resume.

## Gotchas already paid for

- **`unknown` must not be a universal gate bypass.** Excusability is a property
  of the *requirement's category* (work authorization is never on a resume),
  never of what the model chose to answer. When a bare `unknown` cleared a gate,
  a local 7B graded "Bachelor's degree in CS" as unknown — for a resume that
  plainly listed one — and walked through. Any model could have done the same to
  any gate.
- **A quota latch must be per-provider and self-clearing.** A global one meant an
  exhausted Gemini key also blocked local Ollama calls, and since a tripped
  latch refuses to call, it could never see a success to reset — permanently
  bricked until process restart.
- **Groq bills `max_tokens` against your TPM budget**, so a generous blanket
  ceiling fails every call before it runs. Size ceilings per node.
- **Free-tier quota is per model, per day** (Gemini: 20/day/model; one match is
  ~7 calls). Rotate across models *before* sleeping — sleeping on the primary
  wastes a minute to learn what a sibling answers instantly.
- **A degenerate extraction must fail loudly.** A JD that parsed into zero
  requirements used to score `strong_match` — with no must-haves, coverage is
  trivially 100% and no gate can fail, so *a failed parse looked like a perfect
  candidate*. `match` now rejects an empty career graph or a requirement-less
  job spec before any verdict is computed. Found by pointing the pipeline at a
  weak local model that returned an empty requirements list.
- **Work-authorization gates reject everyone.** "Must be authorized to work in
  India" appears in most postings and no resume states it, so grading it `none`
  failed the gate for a candidate living and working there. Hence the `unknown`
  grade and `UNSCORABLE_CATEGORIES`.
- **The tailorer copied the JD's minimum years** as the candidate's experience —
  writing "5 years" for someone with 7.0. The prompt showed the employer's
  requirement but never the candidate's computed figure.
- **`notes` and `errors` need append reducers.** The profile and JD branches run
  in parallel and both write them; without reducers LangGraph raises
  `InvalidUpdateError`. Nodes return only their *new* entries.
- **Node modules must not share a name with the function they export.**
  `match.py` exporting `match` gets shadowed in `app/nodes/__init__.py`, making
  it unpatchable in tests. Hence `matching.py` / `rendering.py`.
- **Don't route to `END` per-branch before a join.** The healthy branch still
  triggers the join node, which then reads a key the failed branch never wrote.
  The abort check belongs at the join.

## Privacy

Resumes are dense PII. `REDACT_PII=true` swaps names, emails, phones and URLs
for placeholders before every model call and restores them after — but
employment history is itself identifying, and **free provider tiers generally
train on inputs**. Fine for synthetic data and your own resume; not acceptable
once real users upload theirs. Move to a no-training tier before M1 launches.
For fully local extraction, point `MODEL_EXTRACT` at an Ollama model.

## Next

**Calibration — the baseline exists now (82%, 0 over-generous).** Next: grow
the gold set toward ~30 pairs weighted to `partial_match` (3 of the 4 remaining
errors involve it), then build the two missing eval layers — hallucination rate,
and verifier should-flag/shouldn't-flag pairs. The verifier has been tuned twice
by reading single outputs, which is the thing evals exist to stop.

**Then, in rough order:**

- **Enrichment interviewer** (M2) — the one genuinely agentic component. Reads
  scorecard gaps, asks 3–5 targeted questions, writes answers back as
  `user_claimed` atoms. This is what makes a returning user's second
  application better than their first, and it's the retention story.
- **Real `interrupt()` + checkpointer** — replace the M1 re-entry into `render`
  with a LangGraph interrupt over a Postgres checkpointer. The routing shape in
  `graph.py` is already correct for the swap.
- **Provenance in the resume UI** — hover a bullet, see its source atom.
  The data is already logged; only the UI is missing.
- **Auth + Postgres** — the store is already keyed and Postgres-shaped, so this
  is a wrapper, not a migration.
- **Batch mode** — paste 30 JDs, rank by fit. Cheap once the graph is cached,
  and it's the feature that saves the most applicant time.
- **Outcome tracking** — the `applications` table exists and is unused. Logging
  replies is what eventually tells you whether the verdicts are honest.

## Known limitations

- **Grader calibration: 82% agreement, 0 over-generous** (22 cases,
  `gemini-3.5-flash`, 2026-08-15). Good enough to build on; not yet good enough
  to trust unsupervised. See the confusion matrix above.
- **Verifier over-flags.** It catches real problems and still flags accurate
  specific→general naming. No eval yet.
- **Free tier is the binding constraint** — Gemini allows 20 requests/day/model
  and one match is ~7 calls, so ~3 runs/day. Pro models 429 immediately, which
  is why `match` runs on Flash despite being the highest-judgement node.
- **Local Ollama mode compromises the verifier** — every node on one model means
  it's no longer cross-family, which is the whole point of the pass. Local runs
  are a plumbing check, not a quality signal.
- **No auth**, so anyone reaching the port can read every profile. Fine on
  localhost; not a deployment.
