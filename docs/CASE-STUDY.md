# Exception Desk
### An alert-correlation engine for a systematic trading operation, wrapped in a playable shift simulator

`Python · NetworkX · JavaScript · Vercel Edge · OpenAI`

> ⚠️ **Slots marked `[[ ]]` need the real numbers from `make eval`.** Everything else is
> final. Do not publish with placeholders in it — an unfilled number is worse than no
> number.

---

## The problem

One fault produces a storm.

A vendor price file misses its 05:10 window. The loader fails. Pricing serves stale
marks. Risk limits evaluate against those marks and throw a breach. P&L flags an
8-sigma move that never happened. Reporting goes out with yesterday's prices.

Six people get paged. **That is one incident, not forty alerts** — and the two loudest
alarms, the risk breach and the P&L spike, are both false and three hops downstream of
anything that actually broke.

The operator's real job is not reading alerts. It is working out which one is the cause.

---

## What I built

Two halves that stand on their own.

**A correlation engine.** A 30-service dependency graph of a trading stack — vendor
feeds, ingest, infrastructure, pricing, risk, OMS/EMS, FIX gateways, post-trade. Faults
are injected at known nodes and propagated along real dependency paths, so the ground
truth is known for every alert. The engine collapses the resulting storm into incidents,
ranks the likely root cause against the graph, and then **scores itself against that
ground truth.**

**A shift simulator.** Five playable scenarios where you take the shift, a clock runs,
and every move costs minutes. Run a diagnostic, warn the portfolio managers, declare a
root cause, write the handover. Damage accrues while you deliberate.

---

## The design decision that matters

**Classification is deterministic. The language model only writes prose.**

Grouping, correlation, root-cause ranking, check findings and scoring are all rules —
over a dependency graph, a fixed answer key, or arithmetic. Every ranking comes back with
`reasons`: plain strings a human can verify, like `"upstream of 7 of 9 alerting
services"`.

The model takes that output and writes the incident summary, the broker escalation and
the handover note. It never decides what the root cause is, never decides what a
diagnostic returns, and never scores the run.

On an operations desk you have to be able to say *why* something was escalated, and
*"the model thought so"* is not an answer. **Knowing where not to put an LLM was the
design position the whole project is built around.**

---

## Three problems that broke the naive approach

Each scenario exists to punish a specific wrong instinct.

### 1. Timestamps carry no signal

`db_pool_exhaustion`: eight services fail within four seconds of each other. Ordering
tells you nothing — the arrival sequence is essentially noise at that resolution.

Only the graph resolves it. `db_primary` sits upstream of sixteen services; the other
seven that alerted are all downstream of it. Upstream coverage over *what actually
alerted* is the signal that separates a cause from its symptoms.

### 2. The root can alert last

`stale_fx_feed`: the vendor's own staleness check is slower than the downstream P&L
check, so **the root cause alerts seven minutes after its own symptoms.**

Any engine that leans on "earliest alert wins" gets this wrong and cannot be argued out
of it. This is why earliness carries the lowest weight of the three signals.

### 3. Adjacent in time, unrelated in fact

`two_independent`: a late locate file and a broken FIX session, 4.35 minutes apart.
Overlapping in time, disjoint in the dependency graph.

The fix was to make the split topological rather than temporal — two services belong to
the same incident only when a directed dependency path runs between them. Two decisions
inside that:

**Reachability, not induced subgraph.** The service that links two symptoms frequently
does not alert itself. `svc_pricing` sits between the price loader and the P&L spike and
can be perfectly healthy by its own monitoring. An induced subgraph on alerting services
finds no edge and over-splits constantly; the reachability relation walks through the
silent intermediary.

**No linking through a shared ancestor.** Almost everything in this topology is
eventually downstream of `db_primary`. Treating "shares an ancestor" as a link collapses
the estate into one component and merges exactly the case this is meant to separate. A
common ancestor is evidence of a common cause only when that ancestor is itself
implicated — and if it is, it alerts and joins the component on its own.

---

## Measured, not asserted

Because the faults are injected, the right answer is known for every alert. That is what
makes the scorecard real rather than decorative.

| Metric | Result | What it answers |
|---|---|---|
| Pairwise F1 | `[[ ]]` | Did the right alerts end up together? |
| Over-merge rate | `[[ ]]` | Were two problems shown as one? *(the dangerous failure)* |
| Over-split rate | `[[ ]]` | Was one problem shown as seven? *(the annoying one)* |
| Root top-1 | `[[ ]]` | Was the real cause the first thing named? |
| Noise rejection | `[[ ]]` | Was background correctly left out? |
| Alert reduction | `[[ ]]` | 258 alerts in, `[[ ]]` incidents surfaced |

Alert reduction is the headline figure and the least meaningful on its own — collapsing
everything into one incident scores beautifully and helps nobody. It is always quoted
next to pairwise F1.

### Where it is weak

`[[ Name the scenario the engine still gets wrong, and why. ]]`

This section is the point of the whole page. An incident tool that cannot say how often
it is wrong is a demo, not a control.

---

## The finding I did not expect

Partway through I ran the three ranking signals by hand and found they **cannot separate
`flapping_latency`**:

```
service                   cover  early  blast  fired   score
fix_gw_broker_b            0.69   1.00     10      1   0.846
msg_bus                    0.69   0.93     10     12   0.833  <-- TRUE ROOT
margin between top two: 0.013
```

`msg_bus` and `fix_gw_broker_b` have **identical** upstream coverage and **identical**
blast radius — their downstream sets in this topology are almost the same. So the
tiebreak fell entirely to earliness, and the wrong service won it because a single
background alert on it happened to land first.

Lowering the earliness weight does not fix it. It converts a narrow loss into a tie, and
a tie broken by service name is not a heuristic.

The distinguishing fact was in the data the whole time: **`msg_bus` fired 12 times across
the window; `fix_gw_broker_b` fired once.** A service that is genuinely broken keeps
complaining. No signal in the original design read that.

`[[ What you added, and what it cost. ]]`

---

## Engineering notes

**Ground truth lives in a separate file from the alert stream**, so the correlation
engine cannot accidentally read the answers. That separation is deliberate and it is the
first thing worth pointing out about the repository.

**A non-determinism bug worth recording.** A baseline ranker was flipping between right
and wrong across runs of an unchanged repo. Cause: `networkx.connected_components`
yields sets in an unstable order, and the ranker sorted on score alone — so ties
(0.013 apart, in the case above) were being broken by dict iteration order. Both are now
sorted deterministically, with a test that runs the pipeline twelve times and asserts the
output is identical.

**The pipeline is tested independently of the heuristic.** `test_pipeline.py` injects a
stand-in ranker, so clustering quality is verified without depending on which service
gets named the cause. 171 tests, split so a regression in the plumbing is never hidden
inside a wall of expected failures.

---

## The simulator

The engine is the substance; the simulator is how you show it to someone who is not
going to clone a repository.

Five shifts, each a real failure mode, each punishing a different instinct:

| Shift | The trap |
|---|---|
| Equity price file misses its window | The loudest alerts are both false and three hops downstream |
| Everything fails at once | Eight services in four seconds; timestamps carry no ordering |
| Broker A goes quiet | Diagnosis is easy; being slow to give the PM a true position is what costs you |
| A P&L spike that is not real | The root alerts *after* its own symptoms |
| Two problems, one window | Work one and the other runs unattended |

Scoring is deterministic where it can be: time to correct root cause, whether the PMs
were warned before the impact threshold, whether dead-end diagnostics were run. Only the
handover note is model-graded, and even there the grader is handed an explicit checklist
of what the next region needed — so its judgement is checkable.

The desk arrives as you would find it at the start of a shift: machine off, screens dark.
You power it on, read the incident, accept the shift, and the clock starts.

---

## What I would do differently

`[[ Two or three honest items. Candidates, pick what is true: ]]`

- *The three ranking signals were specified before I had measured anything. I should have
  built the diagnostic view first — the per-candidate signal table that eventually found
  the flapping tie — because it would have shown the gap in an hour rather than a week.*
- *The topology is hand-built from how I understand a trading stack to fit together.
  It is plausible, not authoritative, and a real estate would be messier — cycles,
  undocumented dependencies, services nobody owns.*
- *Everything here is synthetic. Injected faults produce cleaner propagation than reality,
  and the engine has never seen a genuinely ambiguous incident.*

---

## Scope and honesty

All data is **synthetic**. The topology, failure modes and propagation are modelled on
how systematic trading operations break; no real market data, broker relationship or firm
system is involved.

---

**[ Live demo ]** · **[ GitHub ]**
