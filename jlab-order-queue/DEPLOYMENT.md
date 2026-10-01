# DEPLOYMENT.md — jlab_order_queue

Targets the **non-production** `jlab-dev` Canvas instance
(`https://jlab-dev.canvasmedical.com`) only. Nothing here deploys to
production — do not run these commands with `--host` pointed at a
production instance without separate, explicit sign-off.

All commands below assume you're in this project's root
(`canvas-orders/jlab-order-queue/`), the directory containing
`pyproject.toml` and the `jlab_order_queue/` package (which itself contains
`CANVAS_MANIFEST.json`).

## 1. One-time CLI setup (skip if already done for this machine)

The Canvas CLI (`canvas`) is pulled in automatically via this project's
`pyproject.toml` (`canvas[test-utils]`), so `uv sync` already gives you a
working `canvas` command inside `.venv` — no separate global install needed:

```bash
uv sync
uv run canvas --version
```

Authenticate the CLI to `jlab-dev` by adding a section to
`~/.canvas/credentials.ini` (create the file if it doesn't exist):

```ini
[jlab-dev]
client_id=<your OAuth client id for jlab-dev>
client_secret=<your OAuth client secret for jlab-dev>
```

Use a backend OAuth application registered on `jlab-dev`
(`https://jlab-dev.canvasmedical.com/auth/applications/`) — the same kind
of client `order-poller`/`billing-poller` already use in this workspace,
though a dedicated one for this plugin is fine too. Source:
`docs.canvasmedical.com/sdk/canvas_cli/`.

## 2. Pre-flight validation

Run these **before** installing — they catch manifest/handler-resolution
problems and disallowed-import/sandbox issues without touching the
instance at all:

```bash
uv run canvas validate-manifest jlab_order_queue
uv run canvas validate jlab_order_queue
```

Both have already been run against this plugin as part of building it —
`canvas validate` initially caught a real bug (frozen dataclasses in
`logic/queue.py` crashing the plugin sandbox when combined with `from
__future__ import annotations`; fixed, see that file's module comment and
the `canvas-sandbox-dataclass-gotcha` memory note). Re-run them yourself
after any further code change — mypy and the mocked test suite do **not**
catch this class of sandbox-loading issue; only the real sandbox loader
does.

Both should report success for every handler
(`jlab_order_queue.handlers.lab_order_queue:LabOrderQueueHandler`,
`jlab_order_queue.handlers.release_task:ReleaseDeferredOrders`,
`jlab_order_queue.api.queue_api:QueueAPI`) and both applications.

## 3. Install

```bash
uv run canvas install jlab_order_queue --host jlab-dev
```

This packages, uploads, installs, and **enables** the plugin in one step
(`--enable` is the default). It re-runs the same pre-flight checks from
step 2 before uploading, so a validation failure aborts here with no
partial install.

On this **first** install, Canvas auto-provisions the `custom_data`
namespace (`jlab__order_queue`) this plugin declares in its manifest —
including generating the `namespace_read_write_access_key` secret. Confirm
it landed:

```bash
uv run canvas config list jlab_order_queue --host jlab-dev
```

Expect:

```
namespace_read_write_access_key  [set]  (sensitive)
```

(You do not set this value yourself — Canvas generates it. If it shows
`[not set]`, something went wrong with namespace provisioning; re-run
`canvas install` or check `canvas logs` for the install step.)

## 4. Confirm it's running

```bash
uv run canvas logs --host jlab-dev --plugin jlab_order_queue --since 10m
```

Watch for:
- No import/load errors for any of the three handlers.
- `[jlab_order_queue] ...` structured log lines once you place a test lab
  order (see TEST_PLAN.md) — `order detected`, `queue decision`, etc.

```bash
uv run canvas list --host jlab-dev
```

Confirm `jlab_order_queue` shows up, enabled.

## 5. Confirm the applications registered

In the Canvas UI on `jlab-dev`: **Settings → Plugins_IO → Applications**
(`/admin/plugin_io/application/`). You should see two entries named
**"JLab Order Queue"** — one `global` scope, one `patient_specific` scope.

Neither needs **"Open on load"** enabled for this plugin to work — that
setting only controls automatic opening, not whether the app drawer icon
appears. Leave it off unless you specifically want the queue to pop open
automatically (and if you do, enable it on **at most one** of the two —
see the SDK's own warning about multiple apps in the same scope both
having it enabled).

## 6. UAT

Follow `TEST_PLAN.md` for exact step-by-step scenarios using test patients.
Do this **before** considering the plugin ready for real orders — in
particular, verify the Sign button actually disables/re-enables as
expected in the live UI (the Command Validation effect's UI behavior is
the one piece of this plugin that can only be confirmed by actually looking
at a note in the browser, not by any automated test).

## 7. Updating the plugin after a code change

Re-run the same install command — it's idempotent and updates the existing
installation in place:

```bash
uv run canvas validate jlab_order_queue
uv run canvas install jlab_order_queue --host jlab-dev
```

## 8. Rolling back / disabling

```bash
uv run canvas disable jlab_order_queue --host jlab-dev   # stop it, keep it installed
uv run canvas uninstall jlab_order_queue --host jlab-dev # remove it entirely
```

**Note on `uninstall`:** per Canvas's own documented `CustomModel`
constraints, the underlying database table (and its namespace) are **not**
dropped by an uninstall — "tables can be added but never dropped via the
SDK." Uninstalling stops the plugin's code from running; it does not erase
the queue history it already wrote. `disable` is the safer choice if you
expect to reinstall later and want that history intact either way.

## What this deployment does **not** do

- Does not touch production. Every command above targets `--host jlab-dev`
  explicitly; there is no default host configured that could accidentally
  point elsewhere.
- Does not modify Canvas database tables directly — everything here goes
  through `canvas install`'s own packaging/migration pipeline.
- Does not pre-populate any secret/config value beyond what Canvas
  generates automatically for the `custom_data` namespace. There is
  nothing else to configure — this plugin has no API keys or external
  endpoints.
