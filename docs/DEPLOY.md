# Deploying Exception Desk to Vercel

Goal: a public URL you can put on your resume and portfolio, with the live agents
working and your API key never reaching the browser.

Budget: about 25 minutes the first time. Free tier is enough.

---

## Before you start

```bash
cd exception-desk
make site          # regenerates data/ and web/data/
make test          # 171 pass, 14 fail until the ranker lands — expected
```

`make site` must have been run at least once, because **Vercel serves `web/` as static
files and does not run Python.** The JSON in `web/data/` is the app's entire data layer,
so it has to be committed.

---

## Step 1 — Get it onto GitHub

```bash
cd exception-desk
git init
git add .
git commit -m "Exception Desk: alert correlation engine + ops shift simulator"
```

Create an empty repo on github.com (no README, no .gitignore — you have files already),
then:

```bash
git remote add origin https://github.com/<you>/exception-desk.git
git branch -M main
git push -u origin main
```

### Check before you push

```bash
git status --porcelain | grep -i env     # must return nothing
cat .gitignore
```

Your `.gitignore` must contain at least:

```
.env
__pycache__/
*.pyc
.pytest_cache/
node_modules/
```

**If you have ever committed `.env`, do not just delete it in a new commit — it stays in
the history.** Rotate the key at platform.openai.com instead, then remove the file.

**`web/data/*.json` must be committed.** It looks like build output, but on Vercel it is
the data layer. If you gitignore it the site deploys blank.

---

## Step 2 — Connect Vercel

1. vercel.com → sign in with GitHub
2. **Add New → Project** → import `exception-desk`
3. Configure:

| Setting | Value |
|---|---|
| Framework Preset | **Other** |
| Root Directory | **`web`** |
| Build Command | *(leave empty)* |
| Output Directory | *(leave empty)* |
| Install Command | *(leave empty)* |

Setting **Root Directory to `web`** is the important one. It makes Vercel serve your
static files from there and automatically treat `web/api/agent.js` as a serverless
function at `/api/agent`.

4. **Deploy.**

You now have a URL like `exception-desk.vercel.app`. The simulator is fully playable —
scenarios, checks, phones, scoring, debrief — and the agents will report themselves
offline until the next step.

---

## Step 3 — Turn the agents on

**Project → Settings → Environment Variables**

| Name | Value | Environments |
|---|---|---|
| `OPENAI_API_KEY` | `sk-...` | Production, Preview, Development |

Optional overrides, only if you want to pin a different model:

| Name | Example |
|---|---|
| `MODEL_FAST` | `gpt-6-luna` |
| `MODEL_REASON` | `gpt-6-sol` |
| `MODEL_PROVIDER` | `openai` or `anthropic` |

Then **Deployments → ⋯ on the latest → Redeploy.** Environment variables are read at
build time, so an existing deployment will not pick up a key you added afterwards.

Verify:

```bash
curl -s -X POST https://<your-app>.vercel.app/api/agent \
  -H 'content-type: application/json' \
  -d '{"system":"Reply with the single word: ok","user":"test","tier":"fast"}'
```

- `{"text":"ok",...}` → working
- `{"error":"no_key"}` → variable not set, or you did not redeploy
- `404` → Root Directory is not set to `web`

---

## Step 4 — Protect your spending

**This is the step people skip and regret.** Your endpoint is public. Anyone who finds
the URL can POST to it and spend your money.

**platform.openai.com → Settings → Limits**

- **Hard limit: $5.** Not $50. A portfolio demo will use cents.
- **Soft limit / email alert: $2.**

The endpoint already caps `system + user` at 24,000 characters and output at 1,400
tokens, so a single call is bounded. The hard limit bounds the rest.

If you would rather not expose a key at all, see *Option B* below.

---

## Step 5 — Custom domain (optional, 5 minutes)

**Project → Settings → Domains.** A `yourname.com/exception-desk` or
`exceptiondesk.yourname.com` reads better on a resume than `*.vercel.app`. Vercel
issues the certificate automatically.

---

## Option B — Deploy with no API key at all

Perfectly respectable, and arguably the better demo: it never breaks, and it costs
nothing.

Do not set `OPENAI_API_KEY`. The agents then show:

> **AGENTS ARE OFFLINE** — No agent endpoint is answering…
> Everything else on the desk is unaffected: checks, the phones, the clock, root-cause
> scoring and the debrief are all deterministic and need no model.

That message is already written to be read by a visitor rather than a developer, and it
makes the project's central argument for you: the scoring and the correlation never
needed a model in the first place.

**Option C**, if you want the best of both: pre-generate the agent responses once
locally, cache them as JSON, and serve those. No key, no cost, no failure mode, and the
demo shows real model output. That is a half-day of work and worth it if this becomes a
centrepiece.

---

## After it is live

```bash
# any future change
make site
git add -A && git commit -m "..." && git push
```

Vercel redeploys on every push to `main` automatically.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Blank page, console shows 404 on `data/shifts.json` | `web/data/` was not committed — check `.gitignore` |
| `404` on `/api/agent` | Root Directory is not set to `web` |
| `no_key` after setting the variable | You did not redeploy |
| Intro does not play | Cached. Hard reload, or open a private window |
| Everything works locally, not deployed | You forgot `make site` before committing |

---

## A note on what you are publishing

The repo currently contains `docs/BUILD.md` and `docs/DECISIONS.md`, which are written to
you as the builder — build order, what is outstanding, what you tuned and why.

**Leave them in.** `DECISIONS.md` in particular is the most unusual thing in the
repository: a written record of what you changed, what it bought, and what it cost. Very
few portfolio projects have one, and an engineer reading your repo will notice it before
they notice the code.

`BUILD.md` is the only file that reads as a to-do list. Once the ranker lands, retitle
its step 3 from "YOURS, and the only thing left" to a description of what the heuristic
does — so the document reads as design notes rather than homework.
