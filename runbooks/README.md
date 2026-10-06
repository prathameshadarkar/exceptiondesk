# Runbooks

Drop short markdown runbooks here, one per failure mode. The LLM layer (step 6)
retrieves from this directory and cites the file it used — it never invents a
remediation step.

Suggested starting set, matching the scenarios:

- `vendor-file-late.md` — who to chase, what the cut-off is, when to re-run
- `fix-session-down.md` — session/sequence checks, broker contact, resend
- `db-saturation.md` — connection pool inspection, safe restart order
- `scheduler-stall.md` — Airflow scheduler health, safe DAG re-trigger
- `stale-marks.md` — how to confirm staleness before calling it a data issue
- `corporate-action-missing.md` — how to verify a split against a second source

Keep each one to: symptom, first check, safe action, escalation path, who to
tell. That last line matters more than the others on a follow-the-sun desk.
