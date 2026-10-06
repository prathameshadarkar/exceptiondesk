# Build guide

Everything except the correlation engine and the evaluation harness is done.
This is the order to build the rest in, and the reasoning behind each step —
because the reasoning is what you will be asked about, not the code.

Run `make test` first. You should see **171 passing, 14 failing**.

The 171 are the simulator, the metric primitives, and the correlation pipeline
around the ranker — bursting, the topological split, assembly, confidence and
the evaluation harness. All verified, all must stay green.

The 14 are `tests/test_correlation.py`: the spec for `rank_root_candidates`,
which is the one function left. They fail with `NotImplementedError` and they
are your definition of done.

    make test-pipeline   # the plumbing. always green.
    make test-spec       # your half. red until step 3 lands.
    make rank            # your ranker vs two naive baselines, scenario by scenario

---

## Step 0 — understand the graph before you write anything (20 min)

```bash
python -m exception_desk.topology
python -m exception_desk.simulator
```

Then open `data/db_pool_exhaustion.alerts.json` and read it as an operator
would: eight services failing inside four seconds, no obvious ordering. Now
open `data/db_pool_exhaustion.truth.json` and see that all of them sit under
`db_primary`.

That contrast is the entire thesis of the project. If you can explain it out
loud in thirty seconds, you can explain the project in an interview.

The one convention that will bite you: in `topology.py`, an edge `A -> B` means
**"A depends on B"**. So B is upstream. Propagation walks *predecessors*;
root-cause search walks *successors*. Use `downstream_of()` and `upstream_of()`
rather than touching the edges directly.

---

## Step 1 — `group_by_signature` (45 min)

Collapse repeated firings of the same check into one signal. Signature is
`(service, alert_type, entity)`. Within a signature, a gap larger than
`signature_window_minutes` starts a new group.

**Why it exists:** `flapping_latency` has 120 alerts and one problem. Without
this step you report seven incidents and the demo is dead on arrival.

**Assert that every input alert lands in exactly one group.** The test checks
it because it is the easiest thing in the project to get subtly wrong.

Target: `test_signature_grouping_collapses_flaps` and
`test_grouping_does_not_merge_different_services` pass.

---

## Step 2 — `temporal_windows` — **DONE**

Segments groups into bursts on a simple time gap, measured from the burst's
running **end** rather than the previous group's start (a flap spanning the
window would otherwise close the burst underneath itself).

**Why crude:** time cannot separate `two_independent`. The topology step does
that. Cleverness here buys nothing and costs the ability to explain the
pipeline in one breath.

**One measured change worth knowing about.** `burst_window_minutes` went from
4.0 to 6.0. In `missed_corporate_action` the gap between the loader erroring
and risk breaching is 4.11 minutes, so a 4-minute window cut that incident in
half and the orphaned tail was then binned as noise — a real incident missed
by a six-second margin on an arbitrary constant. Widening it also means
`two_independent`'s two incidents (4.35 minutes apart) now share a burst, so
the topology split has to earn the separation instead of time doing it by
luck. That is a harder and more honest test.

---

## Step 3 — `rank_root_candidates` (2–3 hrs — **YOURS, and the only thing left**)

Three signals, combined with the weights in `CorrelationConfig`:

**Upstream coverage** — of the other services alerting in this burst, what
fraction are downstream of this candidate? This is the signal that solves
`db_pool_exhaustion`. Give it the most weight.

**Earliness** — normalised inverse first-seen time. **Weight it low.** In
`stale_fx_feed` the root alerts seven minutes *after* several of its own
symptoms, because the vendor's own staleness check is slower than the
downstream P&L check. Any engine that leans on timestamps gets this wrong.

**Blast radius** — total downstream count in the graph, normalised. A
structural prior, independent of what actually fired.

Every candidate must return `reasons`: strings like
`"upstream of 7 of 9 alerting services"`. These go on screen and into the LLM
prompt. **If you can't write the reason string, the heuristic isn't ready.**

One design question worth deciding deliberately, because you will be asked:
*can a service that didn't alert be the root?* It can — an intermediate node's
own check can miss while everything under it screams. Decide, implement it, and
be able to say why.

**The floor and the ceiling, both measured.** Run `make rank` and you get two
baselines for free:

| ranker | root@1 | fails on |
|---|---|---|
| `earliest` — first to alert wins | 0.62 | `stale_fx_feed`, `db_pool_exhaustion`, `flapping_latency` |
| `blast` — widest blast radius wins | 0.88 | `flapping_latency` |
| *a correct root on every scenario* | **1.00** | — |

With the root right on all eight, the pipeline delivers macro F1 1.000 and
noise rejection 1.00. That is measured with an oracle, not assumed, so any
shortfall is the ranking and not the plumbing.

Note what `blast` failing on `flapping_latency` tells you: `msg_bus` is the
culprit but `fix_gw_broker_b` has a wider blast radius and happens to be noisy
in the same window. Coverage over *what actually alerted* is the signal that
separates them, and blast radius alone cannot. That is the single clearest
argument for the three-signal design, and it is worth being able to say out
loud.

### One thing you should know before you start tuning

The three signals as specified **cannot separate `flapping_latency`.** Run:

    python3 tools/rank_lab.py --only flapping_latency --why

    service                   cover  early  blast  fired   score
    fix_gw_broker_b            0.69   1.00     10      1   0.846
    msg_bus                    0.69   0.93     10     12   0.833  <-- TRUE ROOT
    margin between top two: 0.013   ← near tie

`msg_bus` and `fix_gw_broker_b` have **identical** upstream coverage (0.69) and
**identical** blast radius (10) — their downstream sets in this topology are
almost the same. So with the default weights the tiebreak falls entirely to
earliness, and `fix_gw_broker_b` wins it because a single background alert on
it landed first. The wrong answer, decided by the one signal the whole design
says to distrust.

Lowering `w_earliest` does not fix this. It turns a narrow loss into a tie, and
a tie broken by service name is not a heuristic.

Look at the `fired` column: `msg_bus` fired 12 times across the window,
`fix_gw_broker_b` once. That is the distinguishing fact and no signal in the
current spec reads it.

**So there is a design decision here, and it is yours.** A fourth signal? A
coverage measure weighted by alert volume rather than service count? Something
about flap persistence? Whatever you choose, the answer to "how did you arrive
at these weights" becomes a real answer instead of a shrug — you found a case
the original design could not express and you extended it. That is a much
better story than three weights that happened to work.

Target: the four root-finding tests pass, then all 14.

---

## Step 4 — `assemble_incidents` — **DONE**

Wires steps 1–3, plus four things most implementations skip:

**Split topologically disjoint bursts.** Project the burst's services onto the
dependency graph and check the weakly-connected components. More than one
component means more than one incident. This is all of `two_independent`.

**Drop weak bursts as noise** — but carefully. `missed_corporate_action` is a
*real* incident with two alerts. An aggressive threshold buys you noise
rejection and costs you that scenario. Measure the trade-off rather than
guessing at it; the measurement is the interesting part.

**Set confidence from the margin** between the top two candidates. Floor 0.55
for a near-tie, ceiling 0.99 — the engine never claims certainty, because it is
a heuristic over a model of the estate rather than the estate.

**Prune each incident to what its root can explain.** Connectivity puts a group
in the component; reachability from the chosen root decides whether it stays.
Measured on `flapping_latency`, which has background alerts landing inside a
fifty-minute window:

| | pairwise F1 | noise rejection |
|---|---|---|
| pruning off, correct root | 1.000 | 0.45 |
| pruning on, correct root | 1.000 | **1.00** |
| pruning on, wrong root | 0.869 | 0.91 |

Free when the ranking is right, expensive when it is wrong. That is the
behaviour worth having: a root that explains none of its own symptoms should
look broken rather than quietly plausible. It also means **your ranking now
affects clustering quality, not just the label** — which is the honest coupling,
since an incident is a cause plus what it explains.

**Two decisions in `split_by_topology` you will be asked about:**

*Relation, not induced subgraph.* Two services are linked when a directed
dependency path runs between them in either direction — not when there is an
edge. The service that links two symptoms very often does not alert itself;
`svc_pricing` sits between the loader and the P&L spike and can be perfectly
healthy from its own monitoring's point of view. Reachability walks through
silent intermediaries; an induced subgraph cannot, and over-splits constantly.

*No linking through a shared ancestor.* `svc_locate` and `svc_blotter` are both
eventually downstream of `db_primary`. Treating "shares an ancestor" as a link
collapses this topology into one component and merges `two_independent`. A
common ancestor is evidence of a common cause only when that ancestor is itself
implicated — and if it is, it alerts and joins the component on its own.

---

## Step 5 — the evaluation harness — **DONE**

`score_scenario` and `run_evaluation` in `evaluate.py`, plus `make eval` writing
`data/scorecard.json`. Scenario order comes from `scenarios.SCENARIOS` rather
than a directory glob, so a diff shows a changed number instead of a reordered
file.

Each row also carries `notes` — the engine saying where it is uncomfortable:
incidents it missed entirely, roots it called wrong, whether precision is below
recall or the other way round, how many incidents it flagged for a human. A
scorecard that only reports numbers invites you to read the good ones.

Commit the file once your ranker lands. A scorecard in git means a regression
shows up in a diff.

---

## Step 6 — the LLM layer (2 hrs)

Three generators, all of which take the **output** of the correlation engine:

1. **Incident summary** — plain English, for a PM who does not care about your
   architecture. *"Vendor equity price file missed its 05:10 window. Pricing
   has been serving stale marks since 05:12; 14 instruments affected; the risk
   breach and P&L spike are consequences, not separate problems."*
2. **Proposed action** — retrieved from `runbooks/`, cited by name. Retrieved,
   not invented. If nothing matches, say nothing matches.
3. **Escalation / handover note** — in the four-part structure from the job
   posting: resolved, watch items, for the next region, reminders.

**The boundary is the point.** The LLM never picks the root. It narrates a
ranking the deterministic engine produced. Put that sentence in the README and
say it in the interview.

**Pre-generate and cache every narrative into JSON.** A demo that needs an API
key will break the first time a hiring manager opens it, and you will not be
there to explain. Keep the generation script in the repo to prove you wrote it;
ship the page static.

---

## Step 7 — the web app (Sunday)

Single self-contained page reading the JSON. Four regions:

- **Left:** raw alert feed, streaming against a time scrubber.
- **Centre:** the collapse — alerts flowing into incident cards. This is the
  three-second sell. Make it the best thing on the page.
- **Right:** selected incident — dependency graph with the root highlighted,
  blast radius, summary, proposed action, draft escalation.
- **Top:** counters — alerts in, incidents out, time to first signal, noise
  suppressed.

Plus a **scorecard tab** rendering `data/scorecard.json`. That tab is what
separates this from a demo: it says you measured your own work and you know
where it is weak.

---

## What to write down as you go

Keep a `DECISIONS.md`. Every time you change a weight or a threshold, one line:
what you changed, what moved, what you gave up. Three or four of those entries
are worth more in an interview than a clean commit history — they are evidence
of how you think, which is the thing being assessed.
