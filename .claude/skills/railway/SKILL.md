---
name: railway
description: Operate the Railway deployment (sdr-console + MongoDB services) via Railway's public GraphQL API — check deploy status, read build/runtime logs, list/set/delete service variables, trigger redeploys and restarts. Use when the user asks about the Railway deploy, deploy logs, environment variables in prod, or wants a redeploy — the Railway CLI is NOT available in cloud Claude sessions; this skill replaces it.
---

# Railway

Operate this project's Railway deployment from any Claude session through Railway's
public GraphQL API (`https://backboard.railway.com/graphql/v2`). The Railway CLI
lives only on the user's local machine — **never assume `railway` (the CLI) exists**;
use the script below instead. Deploys still ship the normal way (merge to `main` →
Railway auto-deploys); this skill is for observing and operating what's already there.

## Auth — one token, two kinds

Set ONE of these (in `.env` locally, or as a session/service variable):

- **`RAILWAY_PROJECT_TOKEN` (recommended):** Railway dashboard → the project →
  Settings → Tokens → create a token for the target environment (production).
  Scoped to that one project + environment, sent as the `Project-Access-Token`
  header — least privilege, no account access.
- **`RAILWAY_API_TOKEN`:** Account Settings → Tokens (account/workspace-wide,
  `Authorization: Bearer`). Then also set `RAILWAY_PROJECT_ID` unless the token
  sees exactly one project.

Optional context vars: `RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT_ID` (defaults to
the token's environment, or the environment named `production`), `RAILWAY_SERVICE`
(default service name/id for service-scoped commands, e.g. `sdr-console`).

If no token is configured, don't guess — ask the user to mint a project token
(click-by-click above) and paste it, or fall back to giving them dashboard
instructions as before.

## Commands

```bash
R=.claude/skills/railway/scripts/railway_client.py     # pure stdlib — no pip installs

python3 $R whoami                       # validate token + resolved project/environment
python3 $R status                       # latest deployment per service (status, age, URL, commit)
python3 $R deployments --service sdr-console --limit 10
python3 $R logs                         # runtime logs of the latest deployment
python3 $R logs --build --limit 200     # build logs (Docker build failures live here)
python3 $R logs --deployment <id>       # a specific deployment
python3 $R vars                         # variable NAMES + masked values (safe default)
python3 $R vars --shared                # environment-level shared variables
python3 $R set AISDR_SYNC_HOUR=1        # upsert variable(s); NAME=VALUE, repeatable
python3 $R unset SOME_VAR               # delete variable(s)
python3 $R redeploy                     # redeploy the service's latest deployment
python3 $R restart                      # restart the running containers (no rebuild)
python3 $R cancel --deployment <id>     # cancel an in-progress deployment
```

Global flags: `--project` / `--environment` (ids, override env vars), `--service`
(name or id; defaults to `RAILWAY_SERVICE`, or the only service), `--json`
(machine-readable summary as the LAST stdout line — repo convention).
`--self-test` runs offline checks without a token.

## Gotchas

- **Variable changes do NOT take effect until the next deploy.** After `set`/`unset`,
  run `redeploy` (the CLI prints this reminder). Never redeploy without being asked
  if the user only wanted to stage variables.
- **Don't print secrets.** `vars` masks values by default; only use `--show-values`
  when the user explicitly needs a value, and prefer checking a single name.
  Never write real values into files that get committed.
- **`redeploy` re-runs the LATEST deployment (same commit).** To ship new code,
  merge to `main` instead — Railway auto-deploys from GitHub.
- **Reference variables** like `MONGO_URL=${{MongoDB.MONGO_URL}}` come back
  RESOLVED from the API's `variables` query. When *setting* one, write the literal
  `${{Service.VAR}}` template string.
- **The MongoDB service's data and the `/app/data` volume are production state** —
  nothing in this skill touches them, and `unset`/`redeploy` on the wrong service
  can still break prod. Confirm service + variable names with `status`/`vars`
  before mutating anything.
- **GraphQL errors arrive with HTTP 200** — the client surfaces them as
  `Railway GraphQL error: ...`. "Not Authorized" usually means the token kind is
  wrong for the query (e.g. `me` doesn't work with a project token) or the token
  doesn't cover that project/environment.
- **Schema drift:** if a query fails naming an unknown field, check
  https://docs.railway.com/reference/public-api and fix the GraphQL document in
  `railway_client.py` (documents are the `Q_*`/`M_*` constants at the top).
- The public API has a daily request budget per account — the client retries
  429/5xx with backoff, but don't poll `status` in a tight loop.

## Extending

Add new operations as methods on `RailwayClient` (auth + retry + GraphQL error
handling come free), a `Q_*`/`M_*` document constant, and a `cmd_*` + subparser
entry. Keep it stdlib-only and keep `--self-test` passing offline.
