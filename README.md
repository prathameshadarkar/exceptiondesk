# Exception Desk

**A shift simulator for a systematic trading operations desk**, built on a real
alert-correlation engine.

You take the shift. Something breaks, alerts land, and a clock runs. Every move
costs minutes: run a diagnostic, dispatch an agent, warn the portfolio managers,
declare a root cause. While you spend minutes, damage accrues — PMs trading on
stale marks, shorts going on without locates, fills missing from the blotter. At
the end you write the handover, and an agent playing the next region tells you
what it cannot act on.

Underneath it is the thing that makes the scenarios honest: a 30-service
dependency graph, a fault-propagation simulator, and a correlation engine that
collapses alert storms into incidents and scores itself against ground truth.

One root fault produces a storm. A vendor price file misses its 05:10 window;
the loader fails; pricing serves stale marks; risk limits evaluate against those
marks; P&L flags a spike that never happened; six people get paged. That is
**one incident**, not forty alerts.

Exception Desk collapses the storm, ranks the likely root cause against a
service dependency graph, and drafts the escalation — then scores itself against
ground truth, because an incident tool that cannot say how often it is wrong is
a demo, not a control.

> All data here is **synthetic**. The topology, the failure modes and the
> propagation are modelled on how systematic trading operations actually break;
> no real market data, broker relationship or firm system is involved.

---

## How it plays

Five shifts, each a real failure mode. Turn-based: the clock only moves when you
act, so the game is *which* minutes to spend, not how fast you can read.

| Shift | The trap |
|---|---|
| Equity price file misses its window | The loudest alerts — a risk breach and a P&L spike — are both false and three hops downstream |
| Everything fails at once | Eight services fail in four seconds; timestamps carry no ordering signal at all |
| Broker A goes quiet | Diagnosis is easy; being slow to give the PM a true position is what costs you |
| A P&L spike that is not real | The root alerts *after* its own symptoms |
| Two problems, one window | Two unrelated incidents overlapping in time — work one and the other runs unattended |

Scoring is deterministic where it can be: time to correct root cause, whether
the PMs were warned before the impact threshold, whether dead-end checks were
run. Only the handover note is model-graded, and even there the grader is handed
an explicit checklist of what the next region needed, so its judgement is
checkable.

## The design decision that matters

**Classification is deterministic. The language model only writes prose.**

Grouping, correlation, root-cause ranking, check findings and scoring are all
rules — over a dependency graph, a fixed answer key, or arithmetic. Every ranking
comes with `reasons` — plain strings a human can check, like
`"upstream of 7 of 9 alerting services"`. The LLM takes that output and writes
the incident summary, the runbook citation and the escalation note.

The model never decides what the root cause is, never decides what a check
returns, and never scores your run. It writes the triage assessment, the broker
escalation, the PM note and the handover, and it plays the next region reading
what you wrote. On an operations desk you have to be able to say *why* something
was escalated, and "the model thought so" is not an answer. Knowing where not to
put an LLM is a design position, and this repo is built around it.

Agents run live when an API key is configured — `.env` locally, Vercel
environment variables in production. The key never reaches the browser: the page
posts a *tier* to `/api/agent`, which is the only place a key or a model name
exists. Everything else in the simulator works without a key.

Model choice is a two-tier decision, not one model:

| Tier | Work | Default (OpenAI) | Default (Anthropic) |
|---|---|---|---|
| `fast` | Drafting and translation — broker note, PM brief, triage | `gpt-6-luna` | `claude-haiku-4-5` |
| `reason` | The handover judge, grading free text against a checklist | `gpt-6-sol` | `claude-sonnet-5` |

`exception_desk/providers.py` is the only module that knows an API shape;
agents declare what *kind* of work they are and the adapter decides who serves
it. Provider follows whichever key is present, and `MODEL_FAST` / `MODEL_REASON`
override the defaults without a code change. See `.env.example`.

---

## The eight scenarios

Each one breaks a different naive approach. Three of them are the real test.

| Scenario | What it is | What it breaks |
|---|---|---|
| `vendor_price_file_late` | Equity price file misses its window | — baseline |
| `fix_session_drop` | Broker FIX session drops mid-session | Confusing severity with rootness |
| `db_pool_exhaustion` | **Hard.** DB connection pool exhausted | Temporal ordering — 8 services fail in 4 seconds |
| `airflow_dag_cascade` | Scheduler stops dispatching | Preferring data roots over infra roots |
| `stale_fx_feed` | FX rates stale, false P&L spike | "Earliest alert is the root" — here it fires *last* |
| `missed_corporate_action` | Split not applied to one name | Noise rejection: a real 2-alert incident in a noisy window |
| `flapping_latency` | **Hard.** Message bus flaps 7× | Over-splitting: 7 pages for 1 problem |
| `two_independent` | **Hard.** Two unrelated incidents overlap | Over-merging: disjoint in topology, overlapping in time |

Because the faults are injected, ground truth is known for every alert — which
is what makes the scorecard real rather than decorative.

---

## Measured, not asserted

| Metric | Question it answers |
|---|---|
| Pairwise F1 | Did the right alerts end up together? |
| Over-merge rate | Were two problems shown as one? *(the dangerous failure)* |
| Over-split rate | Was one problem shown as seven? *(the annoying failure)* |
| Root top-1 / top-3 | Was the real cause the first thing named? |
| Noise rejection | Was background left out? |
| Alert reduction | Alerts in vs incidents surfaced |

Alert reduction is the headline and the least meaningful on its own — merging
everything into one incident scores beautifully and helps nobody. It is always
quoted next to pairwise F1.

---

## Layout

```
exception_desk/
  sim.py          5 playable shifts, checks, scoring       [done]
  agents.py       6 agents, prompts, context assembly      [done]
  topology.py     30-service dependency graph              [done]
  scenarios.py    the 8 seeded failure modes               [done]
  simulator.py    propagation + noise + ground truth       [done]
  correlate.py    grouping, bursting, split, assembly       [done]
                  rank_root_candidates                      [TODO — docs/BUILD.md step 3]
  evaluate.py     metric primitives + scoring harness       [done]
web/
  index.html      the shift console
  board.html      the data dashboard
  api/agent.js    serverless model proxy (the key lives here)
tests/
  test_sim.py         shift design + scoring               [passing]
  test_simulator.py   data integrity                       [passing]
  test_metrics.py     metric primitives                    [passing]
  test_pipeline.py    bursting, split, assembly, harness   [passing]
  test_correlation.py the target spec for the ranker       [failing until built]
tools/rank_lab.py the ranker dev loop: yours vs two baselines
docs/BUILD.md     step-by-step build order and reasoning
docs/DECISIONS.md what was tuned, what it bought, what it cost
```

Ground truth is written to a **separate file** from the alert stream, so the
correlation engine cannot accidentally read the answers. That separation is
deliberate and it is the first thing worth pointing out about the repo.

---

## Running it

```bash
pip install -r requirements.txt
make site     # generate scenarios + build web/data/
make serve    # http://localhost:8000 — put ANTHROPIC_API_KEY in .env for live agents
make test     # 171 pass, 14 fail until rank_root_candidates is implemented
make rank     # the ranker dev loop: your ranker against two naive baselines
make eval     # scorecard across all 8 scenarios -> data/scorecard.json
```

---

## Status

Simulator, topology, metrics, correlation pipeline and evaluation harness are
complete and tested: 171 passing.

One function is outstanding — `rank_root_candidates`, the rootness heuristic
itself — and its 14 tests are red by design, because they are its specification.
`docs/BUILD.md` step 3 has the shape, the traps and the scenario that catches
each one.

The floor and the ceiling are both measured rather than asserted. Two naive
baselines ship in `tools/rank_lab.py`:

| ranker | root@1 | fails on |
|---|---|---|
| earliest alert wins | 0.62 | `stale_fx_feed`, `db_pool_exhaustion`, `flapping_latency` |
| widest blast radius wins | 0.88 | `flapping_latency` |
| *correct root everywhere (oracle)* | **1.00** | — |

With a correct root on every scenario the pipeline delivers macro pairwise F1
**1.000**, over-merge **0.00**, over-split **0.00**, noise rejection **1.00**,
and 258 alerts reduced to 9 incidents (**28.7×**). Any shortfall from there is
the ranking heuristic, not the clustering.
