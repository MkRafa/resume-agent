# resume-agent — M0

Matches a candidate against a job description, grades the evidence requirement
by requirement, and — when it's a match — writes a tailored resume in which
every bullet traces back to a fact the candidate actually supplied.

M0 is the core, with no UI. All the product risk lives here: **is the scorecard
trustworthy, and is the tailored output honest and good enough to send?** Auth,
upload flows and billing are known work with no learning in them, so they come
after this is proven.

## Web app (M1)

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

## How it works

```
intake_profile ─> build_career_graph ─┐
                                      ├─> match ─> verdict (deterministic rule)
intake_jd ─────> parse_jd ────────────┘             │
                                                    ├─ not_matching ─> gap_report ─> END
                                                    ├─ partial ──────> gap_report ─┐
                                                    └─ strong ────────────────────┤
                                                                                   ▼
                                              select_facts ─> tailor ─> verify ─> render
                                                                          │
                                                              blockers? ──┴─> render blocked
```

Deterministic edges throughout. This is not an agent loop and shouldn't become
one — a resume pipeline that takes a different path each run is a bug. The only
genuinely agentic component in the design is the enrichment interviewer (M2).

### Four decisions worth knowing

**The Career Graph is the primitive, not the resume.** `app/schemas/career.py`
holds atomic fact atoms with ids. Tailoring is retrieval + selection + rewriting
over those atoms, never re-expansion of an already-lossy resume PDF. It is also
the stable prompt prefix across every application one candidate makes — keep it
first in the prompt and providers with caching make applications 2..n cheap.

**The verdict is Python, not a model.** The model grades evidence per
requirement (`direct` / `adjacent` / `transferable` / `none`, each citing atom
ids); `app/tools/verdict.py` turns those grades into a verdict by fixed rule.
That's what makes it explainable ("you failed r_02"), stable across runs, and
testable against a gold set. A 0–100 score from a model gives you none of that.

**The verifier is adversarial, cross-family, and blind to the JD.** It runs on a
different model (`MODEL_VERIFY`, Groq by default) because a model asked to check
its own work shares its own blind spots. It never sees the job description — a
verifier that knows what the text was optimised for rationalises its stretches.
Unresolved blockers make `render` raise, not warn.

**Anything computable is computed.** Years of experience, keyword coverage,
page budget, date ranges — all Python (`app/tools/`). LLMs get overlapping
employment spans wrong in ways that look entirely plausible.

## Layout

```
app/
  graph.py        the whole pipeline, readable in one screen
  state.py        typed graph state (note the append reducers — see below)
  schemas/        Pydantic contracts at every node boundary
  skills/         versioned prompt modules; the highest-churn artifacts
  nodes/          one file per stage
  tools/          deterministic; unit-tested
  hooks/          PII redaction, cost ledger, render guardrail, provenance audit
  templates/      ATS-safe HTML
evals/            pytest + JSONL fixtures
data/             gitignored except samples — never commit a real resume
```

## Tests

```bash
./.venv/bin/python -m pytest evals/ -q
```

61 tests, no API calls — the stubbed pipeline test in `evals/test_pipeline.py`
exercises fan-out, join, routing, the render guardrail and provenance offline.

### The gold set

The eval that actually matters. 22 hand-labelled `(profile, JD)` pairs built
from 8 synthetic candidates against 13 job descriptions — the same candidate
appears against several roles, so the set tests *discrimination*, not just
recognition (Dev is a `strong_match` for the research role and `not_matching`
for the production MLE role; Priya is strong for the Python payments role and
partial for the Go one).

```bash
./.venv/bin/python evals/run_gold.py            # confusion matrix
./.venv/bin/python evals/run_gold.py --only priya_x_meridian_go
RUN_GOLD=1 ./.venv/bin/python -m pytest evals/test_gold.py -v   # same, in CI
```

Two things keep this affordable on a free tier: the runner **stops at the
verdict** (3 calls per case, not 7 — tailoring and verification are a separate
concern), and **extractions and JD parses are cached on disk by content hash**,
so 8 profiles across 22 cases costs 8 extractions, and re-running after a
change to the *grader* prompt costs zero. 22 cases ≈ 43 calls cold, 22 warm.

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

**Known gap:** only 5 of 22 cases are `partial_match`, the hardest class.
Weight new cases toward it.

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

- Grow the gold set to ~30 pairs; that number gates everything else.
- Swap the M0 hard stop on verify blockers for a real LangGraph `interrupt()` +
  Postgres checkpointer — the routing shape in `graph.py` is already correct.
- Enrichment interviewer (the one true agent), driven by scorecard gaps.
- Then M1: upload → verdict → PDF, with auth and storage.
