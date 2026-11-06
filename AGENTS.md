# AGENTS.md

Guidance for coding agents working in this repository.

## What this repo is

`mantid-python-script-lsp` is a small stateless HTTP service that answers one question about a FIA
reduction script — *"does this make sense?"* — without running it. The FIA script editors POST the
script on a debounce and get back Monaco-shaped diagnostics: syntax errors from CPython's own parser
plus jedi's coverage of what it could not reach, and undefined-name warnings from pyflakes over a list
of framework names generated from the running Mantid. See `README.md` for endpoints, the wire contract,
how to run it, and the "Measurements" section — every timeout, cap and sizing number in this repo is
derived from those measurements. Read it before making changes.

The name says LSP; the service is not one. There is no session, no incremental document state, no
WebSocket — deliberately, because a FIA pod restarts often and one broken script must not take
everyone's editor with it. Treat the name as "language-server-ish", not as a protocol commitment.

## Position in the FIA service

### Overarching FIA architecture

FIA is a multi-repository system (all checked out as siblings under the same parent workspace
directory) that automatically reduces instrument data, and also lets users browse, plot, and rerun
that data interactively:

```text
[ISIS Instrument Archive (/archive, /imat) & lastrun.txt]
        │
        ▼
  file-watcher (Kopf Operator + Per-Instrument Watcher Pods)
        │  publishes file path to RabbitMQ: `watched-files`
        ▼
  run-detection ◄──── fetches instrument rules (`GET /instrument/{inst}/specification`) ────► FIA-API
        │  ingests .nxs metadata, verifies rules, publishes JobRequest JSON
        │  to RabbitMQ: `scheduled-jobs` (or retries via `failed-watched-files`)
        ▼
  jobcontroller (job_creator) ◄──── requests PreScript (`GET /script/{inst}`) & posts Job ──► FIA-API ◄──► autoreduction-scripts
        │  spawns Kubernetes Job in cluster:
        ├──► Main container: runners (`ghcr.io/fiaisis/mantid` or `mantid-imaging`) -> writes reduced data to `/ceph`
        └──► Sidecar container: jobcontroller (job_watcher) -> updates job status/outputs in FIA-API (`PATCH /job/{id}`)

[Interactive / Web & Live Data Paths]
  scigateway (Host App) ──► frontend (FIA React Plugin)
                                ├──► fia-auth (UOWS login, JWT verify/refresh, `/experiments` RB lookup)
                                ├──► FIA-API (runs, jobs, scripts, specifications, reruns -> `watched-files` / `scheduled-jobs`)
                                │        └──► mantid-python-script-lsp (THIS REPO) — `POST /analyse`, script diagnostics
                                └──► plotting-service (h5grove NeXus/HDF5 & IMAT image serving from `/ceph` & `/imat`)

  Kafka (`<INST>_events`, `<INST>_sampleEnv`, `<INST>_runInfo`)
        └──► live-data (Live Data Operator + Mantid Live Data Processor fetching `/live-data/{inst}/script` from FIA-API)

  LLSP (Long Lived Script Processors: `LLSP-API` + RabbitMQ + Celery `LLSP-Worker` built on `ghcr.io/fiaisis/mantid`)

[Infrastructure & Deployment]
  k8s (Terraform + Ansible for OpenStack clusters, Redpanda, local Minikube)
  gitops (ArgoCD App-of-Apps & Kustomize `base`/`envs`/`variants` manifests for all FIA services)
```

### This repository's location (upstream, downstream & siblings)

- **Role**: an advisor to a human typing in a Monaco editor. Nothing else in FIA depends on this
  service; if it is down, scripts still save and jobs still run.
- **Upstream caller**: `frontend` (`../frontend`) — the only client. It calls through FIA-API, never
  directly.
- **Fronted by**: `FIA-API` (`../FIA-API`) — `POST /script/analyse` verifies the JWT, applies the size
  cap and a per-user rate limit, and forwards this service's status and body **unchanged**. The
  frontend branches on the status (`503` means "keep last diagnostics, retry", not "script is clean"),
  so a proxy that rewrites status codes or wraps the body breaks the contract even when the JSON is
  preserved.
- **Runtime dependency**: the Mantid runner image (`ghcr.io/fiaisis/mantid`, pinned by tag in
  `container/mantid_python_script_lsp.D`). This service has no database, no queue, and no cache of its
  own.
- **Deliberately not a dependency**: `../autoreduction-scripts`. Facility reduction code is not
  installed here, and that turns out to cost almost nothing: pyflakes does not resolve modules, so
  `import isis_powder` and `from isis_powder.routines.common import isis_num` produce no diagnostics at
  all — the imported name is bound by the import statement itself. Only a *star* import of facility code
  changes the picture, and the rule under "Hard rules" covers it. Facility code changing must not be able
  to take this service down or change its answers.
- **Sibling repositories to check when changing contracts**: `../FIA-API` (mirrors `AnalyseRequest`,
  forwards the response) and `../frontend` (mirrors `Diagnostic`/`AnalyseResponse` in TypeScript). See
  "The wire contract" below.

### Core technologies

- **Python**: the version is *not* a preference. `requires-python` in `pyproject.toml` must match the
  interpreter in the pinned runner image, because "does this script make sense" is answered by that
  interpreter and its packages. Check both before creating an environment.
- **FastAPI** + **uvicorn**: ASGI app in `script_lsp/app.py`; the image listens on port 80 and runs as
  root, like the other FIA services.
- **Pydantic v2**: the wire contract in `script_lsp/models.py`.
- **jedi**: name resolution and syntax errors, and later the completion/hover layer.
- **pyflakes**: undefined-name linting over a generated preamble.
- **Mantid**: imported only inside the runner image, only at warm-up, only off the event loop. It is
  not a dev or CI dependency — the unit suite runs without it.
- **pytest** (+ `pytest-random-order`, `pytest-env`, `pytest-cov`, `httpx`, `pytest-asyncio`), **Ruff**
  (lint and format), **Mypy** (`--strict`).
- **Process pool** (`concurrent.futures`, `forkserver`): where analysis actually runs. See
  "Where analysis runs".

## Repository structure

```text
script_lsp/                    Application source
  app.py                       FastAPI app: lifespan, warm-up gate, /healthz and /ready
  warmup.py                    Imports the framework in a thread, proves it with a real algorithm,
                               generates the framework names, starts and warms the analysis pool
  pool.py                      forkserver process pool: sizing from the cgroup quota, worker initializer
  models.py                    THE wire contract — canonical, see "The wire contract" below
  positions.py                 Every coordinate convention in one place; converts to Monaco's
  preamble.py                  Framework names as a bindable source block + star-import neutralisation
  syntax.py                    compile() + jedi merged into diagnostics
  lint.py                      pyflakes filtered to undefined names, mapped back to the user's file
  analyse.py                   POST /analyse

test/                          Pytest suite, mirroring the package. Mantid is not needed to run it.
container/                     Dockerfile (mantid_python_script_lsp.D), FROM a pinned runner tag
spike/                         Measurement scripts whose output is README.md's "Measurements"
                               section; spike/corpus/ is generated and gitignored — see "The corpus"
.github/workflows/             tests.yml (PR checks), build-and-push.yml (main only, edits ../gitops)
README.md                      Endpoints, contract ownership, measurements, configuration, how to run
pyproject.toml, mypy.ini       Dependencies and ruff/mypy/pytest config
```

### Continuous integration

`.github/workflows/` holds two workflows, and what runs where is deliberate:

| workflow | when | what |
| --- | --- | --- |
| `tests.yml` | every push and PR | `pytest` with branch coverage, then `ruff format --check`, `ruff check`, `mypy --strict`. Plain Python, no Mantid — the suite stubs the framework, which is only possible because nothing imports it at module scope. |
| `build-and-push.yml` | push to `main` only | Builds `container/mantid_python_script_lsp.D`, pushes to `ghcr.io/fiaisis/mantid-python-script-lsp`, then commits the new **digest** into the staging manifest in `../gitops`, like every other FIA service. |

Consequences an agent should know:

- The image build is not in the PR checks on purpose: a Mantid base image measured in gigabytes does not
  belong on every push to a branch. A change to the Dockerfile or the dependency pins therefore has to be
  verified **locally** by building the image before merging — CI will not catch it on a branch.
- The publish workflow edits `../gitops`, so it fails until that component's staging manifest exists at
  `components/mantid-python-script-lsp/envs/staging/mantid-python-script-lsp.yml` with a container named
  `mantid-python-script-lsp`. Creating those manifests is a prerequisite for a green first run on `main`.
- Image scanning, scorecards, CodeQL and the corpus-in-CI job are not written yet. When they are, copy the
  sibling repositories' workflow files rather than inventing a house style — they are already consistent
  across FIA, and Dependabot's `action-packages` group expects the SHA-pinned actions both workflows here
  use.
- Dependabot is not configured for this repository yet.

## pyproject.toml — read this first

`pyproject.toml` is the single source of truth for dependencies, extras, and tool config:

- Runtime dependencies are pinned exactly (`==`); keep new ones pinned the same way.
- Extras: `formatting` (ruff, mypy), `test` (pytest + plugins, `httpx`), `all`. `pip install .[all]`
  (escape the brackets in zsh: `.\[all\]`).
- `requires-python` must track the runner image's interpreter, not the other way round.
- `version` here is the only place a version is written: `script_lsp/app.py` reads it back from the
  installed package's metadata rather than repeating a literal, so a release cannot advertise the wrong
  number.
- Ruff carries the FIA-API rule set (bandit, bugbear, pylint, pyupgrade, pathlib, `line-length = 120`).
  Check `[tool.ruff.lint.ignore]` and `per-file-ignores` before "fixing" an intentional exclusion —
  `spike/corpus` is excluded because linting facility scripts would report problems in someone else's
  analysis code, and `warmup.py` is allowed deferred imports because Mantid exists only in the image.
- Mypy config is in `mypy.ini` (`--strict`, with the C-extension framework and pyflakes exempt).
- `pytest-random-order` is required: tests must not depend on execution order.

## The wire contract

`script_lsp/models.py` is canonical. Three repositories hold a copy of it, and that duplication is
deliberate — it is what keeps this service deployable on its own, and it makes a breaking change a
three-repo change rather than a runtime surprise:

| repo | what it holds |
| --- | --- |
| here | `AnalyseRequest`, `Diagnostic`, `AnalyseResponse` — canonical |
| `../FIA-API` | `AnalyseRequest` only, for validation and rate-limit keying; the response is passed through |
| `../frontend` | a TypeScript mirror of `Diagnostic`/`AnalyseResponse` |

Rules that fall out of this:

- **Diagnostics are advisory.** Nothing may turn a diagnostic into a blocked save or a blocked rerun.
  A script this service cannot understand must never be reported as broken.
- **The `content_hash` round-trip is not decoration.** The editor discards any response whose hash does
  not match the buffer it sent. Never echo back a hash the service invented.
- **Positions are Monaco's**: 1-based lines, 1-based columns, UTF-16 code units, `end_column`
  exclusive. CPython reports code-point offsets, jedi reports code-point columns, pyflakes reports
  UTF-8 **byte** offsets. That is four conventions; `positions.py` is the only place allowed to know
  about them. Do not convert coordinates anywhere else.
- **Severity is a colour the user sees, not a log level.** `error` and `warning` map onto Monaco's marker
  severities — red and orange. `info` is deliberately absent from the `Literal`: a grey squiggle for
  something nobody can act on trains users to ignore the two levels that mean something. When the marker
  bridge is built in `../frontend`, that mapping is where the two contracts meet, and the same note
  belongs in that repository's `AGENTS.md`.
- **Ranges must be non-empty.** Monaco draws nothing for a zero-width range, so a range where start
  equals end is widened by one character.
- `parses: false` does **not** mean "plus one warning" — linting a non-parsing script produces an
  undefined name per name, so lint is skipped and one true syntax error is returned.

## Hard rules for this service

Things an agent will otherwise get wrong, learned from measurements in this repo:

- **Report only names whose every possible source is known.** A name is called undefined only if the
  script, the builtins, and the modules the preamble binds account for everything it could arrive from.
  `neutralise_star_imports` therefore rewrites star imports of *modelled* modules only: blanking
  `from isis_powder import *` claims its names were supplied, and measured on a script in that shape it
  produced three "undefined name" reports for facility helpers on code that runs correctly. Left alone,
  pyflakes hedges each unresolved name as `ImportStarUsage`, which is not reported — so such a script
  gets no undefined-name checking, including for genuine typos. That loss is accepted, tested
  (`test_that_silence_covers_a_typo_too_and_that_is_a_decision_not_an_oversight`) and not to be "fixed"
  by making the linter confident again.
- **Never import Mantid on a request path, or at module import time.** The import is not thread-safe
  alongside an asyncio loop and shows up as a hang, not an error. It happens once, in a thread, at
  warm-up (`warmup.py`), and the workers inherit the result. If you find yourself wanting
  `import mantid` in `syntax.py`/`lint.py`/`analyse.py`, the design has changed.
- **Keep analysis off the event loop.** Analysis is CPU-bound and holds the GIL: measured at 13.7
  analyses/s on Starlette's threadpool versus 64/s serial and 247/s in 8 processes — threads are
  *worse than doing nothing*. Work goes through `pool.py`. `/healthz` staying honest depends on
  this.
- **Do not invent framework names.** The list of names a script is allowed to use comes from `dir()` on
  live modules at warm-up. A made-up name (there was a candidate called `Configurator`, which does not
  exist in Mantid 7) suppresses a real warning forever and teaches users to ignore the squiggle.
- **Neutralise star imports; do not delete them.** `from mantid.simpleapi import *` makes pyflakes stop
  reporting real typos. It is replaced line-for-line with an equal-length comment so every other line
  number survives. If you change that rewriting, the line-number round-trip test in
  `test/test_preamble.py` must fail.
- **Readiness means the whole path.** `/ready` waits for a real algorithm to have run, the
  names to be generated, and a probe analysis to have completed in every pool worker — `ProcessPoolExecutor`
  spawns lazily, and an unwarmed worker pays jedi's import inside a user's keystroke.
- **Never run the analysis fallback in-process.** If the pool is gone, answer `503`; do not quietly
  analyse on the loop to stay available.
- **Size caps count bytes, and both sides must mean the same thing by them.** `MAX_SCRIPT_BYTES` is
  compared against `len(content.encode())`, not `len(content)`, and FIA-API enforces the same number
  against the same definition — a script of 200,000 *characters* of multi-byte text is bigger than the
  cap. The two values are separately configured, so changing one without the other produces a service
  that rejects scripts its proxy already accepted. The same caveat is recorded in `../FIA-API/AGENTS.md`
  for whoever adds the route.
- **Probe paths are the FIA names.** `/healthz` for liveness, `/ready` for readiness — the same pair
  every other component answers and every probe in `../gitops` points at. Renaming them is a deployment
  break, not a cosmetic choice; `test/test_health.py` pins it.
- **Byte-size caps are checked before parsing**, and the limit is a memory decision, not a style one.

## The corpus

`spike/corpus/` is generated by `spike/make_corpus.py` from real reduction scripts pulled off a live
FIA API, and is **gitignored**. It contains facility analysis code — instrument names, archive paths,
working configurations — so it stays out of the repository even though the generator is checked in.

- Do not commit it, paste it into a PR, an issue, or a test fixture.
- CI must not depend on it. The corpus test uses synthetic fixtures shaped like real scripts
  (star import, `mtd`, chained `ConfigRegistry()` calls, `GlobalInput.Get()`); the real-corpus check
  runs locally against a script the developer holds.
- Variants are `<name>.py`, `_small`, `_x2`, `_x8` (scale, for latency), `_broken` (a deliberate
  mistake), `_star` (framework imported with `*`, the common case in real scripts).
- The requirement the corpus exists to enforce: **a clean real script produces zero diagnostics.**
  Anything else is a bug in this service, not advice for the user.

## Measurements

`README.md`'s "Measurements" section is derived from `spike/`, not hand-written. When you change
anything that affects cost — a base image bump, a new parser, the preamble, the pool — re-run the spike
scripts **in the image** and update those tables. Do not edit a number to match new code.

Two traps, both measured:

- The laptop numbers are noisy: the same HTTP configuration measured 27 ms and 108 ms on successive
  runs, and a `/healthz` control in a 24-process burst moved between 20 ms and 84 ms depending on
  machine load. In-process numbers reproduce to a few percent. Quote the in-process comparison in
  conclusions and treat HTTP figures as order-of-magnitude.
- `os.cpu_count()` reports the host's cores, not the pod's cgroup quota. Pool sizing reads
  `cpu.max` / `cpu.cfs_quota_us` for that reason.

## Environment & running commands

These instructions are deliberately generic: do not assume any machine already has a conda env, a
kubectl context, or this repo's image built — check what is actually available before running
anything, and don't hardcode names you happened to see on one machine into scripts or docs.

Prefer `conda` when it is available; fall back to a `venv` only if conda is not installed
(`command -v conda` on Linux/macOS, `where conda` on Windows — never assume either way).

1. Check for an existing env for this project: `conda env list` (env names vary per machine — look for
   one matching the repo name).
   ```shell
   conda run -n <env-name> <command>
   ```
2. If conda exists but no env does:
   ```shell
   conda create -n mantid-python-script-lsp python=<version-from-pyproject.toml> -y
   conda run -n mantid-python-script-lsp pip install -e '.[all]'
   ```
3. Only without conda, use a virtualenv (`.venv/bin/activate` on POSIX,
   `.venv\Scripts\Activate.ps1` on Windows), then `pip install -e '.[all]'`.

The suite does not need Mantid. The service does: run it with `uvicorn script_lsp.app:app` only in a
place where `import mantid` works (inside the image), and remember `/ready` stays `503` until
warm-up finishes.

```shell
conda run -n <env-name> pytest test --random-order --random-order-bucket=global
conda run -n <env-name> ruff check . && conda run -n <env-name> ruff format .
conda run -n <env-name> mypy --strict script_lsp

docker build -f container/mantid_python_script_lsp.D -t mantid-python-script-lsp:local .
docker run --rm -p 8080:80 mantid-python-script-lsp:local
```

**Do not run pytest/ruff/mypy with the repo bind-mounted into a container.** They write caches into the
working tree as root, and the next host run fails with "attempt to write a readonly database" or
refuses to overwrite root-owned `.pytest_cache`/`.ruff_cache`/`.mypy_cache`. Run the checks in a host
env, and use a container only when Mantid is genuinely required (in which case copy the repo into the
image's own filesystem, or clean the caches afterwards).

### Configuration

| variable | what it does |
| --- | --- |
| `ANALYSIS_WORKERS` | Number of analysis processes. Unset: one per core of the pod's CPU quota, falling back to `min(4, cores)`. A non-numeric or sub-1 value falls back to the quota, never to zero workers. |
| `MAX_SCRIPT_BYTES` | Request size cap in bytes (default 200,000). Must equal FIA-API's cap — see the caveat under "The wire contract". |
| `DIAGNOSTIC_LIMIT` | Diagnostics returned per request (default 100), ordered by position, with `truncated: true` beyond it. |

All three are read once at import and a value that cannot be honoured is logged and ignored rather than
applied: a typo in a Deployment must not produce a pod that rejects every script.

## Kubernetes commands

Available kubectl contexts vary per machine/operator — always run `kubectl config get-contexts` first
and see what is actually configured. Never rely on the current context; always pass `--context`
explicitly, at the **end** of the command:

```shell
kubectl get pods -n fia --context <context-name>
kubectl logs deploy/mantid-python-script-lsp -n fia --context <context-name>
```

Double- and triple-check the context name before running anything against a production-like cluster.

## Managing issues

There is no local `.github/ISSUE_TEMPLATE/`; GitHub serves the org-wide default from `fiaisis/.github`.
Use it rather than inventing a format:

```shell
gh issue create --template Issue
```

Check `gh issue list` / `gh issue view <number>` first to avoid duplicates.

## Branch naming

Branches use snake_case descriptions of the work. No category prefixes like `feature/` or `fix/`.

- **Format**: `description_of_work` (e.g. `add_analysis_pool`, `handle_star_imports`).

## Commit messages

`main`'s history is squash merges: one commit per PR, titled `<PR title> (#<PR number>)`. So the PR
title is what survives permanently — but individual commit messages still become the squash body, so
write each one as a focused unit of work in the imperative mood, with a *why* in the body for anything
non-trivial. In this repo especially: a change that reverses an earlier decision (threadpool → process
pool is the example) should cite the measurement that forced it, because the previous approach looks
reasonable to anyone reading the code alone.

## Requirements for code changes

Only run tests, linters, and type checkers when they are **relevant to the files you actually
changed** — skip them for `.md`-only changes. When you do change Python, dependencies, or config:

1. **Add or update tests** in the mirroring `test/` file for any new or changed behaviour. Tests must
   pass without Mantid: stub `script_lsp.warmup.load_framework` (see `test/conftest.py`) rather than
   skipping.
2. **Verify against the real corpus locally** if you touched anything that produces diagnostics —
   `spike/corpus/MERLIN*.py` must still report zero diagnostics, and a deliberate typo must still be
   caught at its real position. A change that makes the corpus clean by making the linter mute has
   failed.
3. **Run the checks** (`ruff check .`, `ruff format .`, `mypy --strict script_lsp`, `pytest test
   --random-order`) and fix everything reported.
4. **Keep `script_lsp` covered.** It is at 100% statements *and* branches; the only exclusions are
   `load_framework` and `preamble.build`, both marked `# pragma: no cover` with the reason, because they
   cannot run without the framework and run in the image instead. Add coverage with the code rather than
   lowering the bar; if something genuinely cannot be covered outside the image, say so in the pragma
   comment.
4. **Re-run measurements** in the image and update README's "Measurements" tables if the change affects
   cost or concurrency.

## Pull request process

1. **Local validation** before opening a PR (for `.py`/`pyproject.toml`/workflow changes):
   ```shell
   conda run -n <env-name> pytest test --random-order --random-order-bucket=global
   conda run -n <env-name> ruff check .
   conda run -n <env-name> ruff format .
   conda run -n <env-name> mypy --strict script_lsp
   ```
   Plus the image build and a real request against it if the change affects the analysis path — unit
   tests cannot catch "Mantid does not import in the deployed image", which is this service's most
   expensive class of failure.
2. **PR title**: clear, concise, normal grammar (e.g. `Neutralise star imports before linting`). Not
   snake_case.
3. **PR description**: the org-wide template from `fiaisis/.github` — `Closes # <Issue Number>` then
   `## Description`. Ask the user for the issue number if it isn't evident, or confirm there is none.
4. **AI-assistance label — apply it yourself, every time.** A pull request you opened was made with the
   assistance of AI, so label it `AI` as you create it rather than leaving it for a human to notice:
   ```shell
   gh pr create --label AI ...                       # at creation
   gh pr edit <number> --add-label AI                # if the PR is already open
   ```
   The repository carries the same label set as `../FIA-API` (`AI`, `dependencies`, `github_actions`,
   `python`, `waiting_on_something` plus the GitHub defaults), so nothing normally needs creating. Check
   with `gh label list -R fiaisis/mantid-python-script-lsp` first, and if a label genuinely is missing,
   create it with FIA-API's exact name, colour and description rather than inventing one — a label with a
   different colour per repository is a label nobody can scan at a glance.
5. **Contract changes**: open the PRs in `../FIA-API` and `../frontend` alongside, and say in each
   description which direction the change is compatible in, so the three can be merged in an order
   that never breaks an older client.

## Handling Dependabot PRs

Dependabot is not configured for this repo yet. When it is added, expect the two dangerous categories,
and neither is a routine bump:

- **The runner base image tag** (`ARG RUNNER_TAG` in `container/mantid_python_script_lsp.D`): changing
  it changes which names scripts may use, what pyflakes knows, and the cost of warm-up. Bump it
  deliberately, with the corpus check and a fresh set of measurements.
- **jedi / pyflakes**: they define what gets reported. Run the corpus check; a "cleaner" result from a
  pyflakes upgrade usually means it has stopped seeing something.

For ordinary dependency and GitHub-Action bumps: `gh pr diff <number>`, then `pytest`, `ruff check`,
`mypy --strict`, and `gh pr checks <number>`; approve and `gh pr merge <number> --merge` (add
`--admin` if branch protection blocks it), close superseded PRs, and finally re-run the suite on
`main`.

## General working practices

- Prefer the `read` tool over `sed`/`cat` one-liners for inspecting files, and `write`/`edit` over
  `cat <<EOF`, `sed -i`, or Python one-liners for changing them.
- Ask the user when a task is ambiguous — especially anything that would change the wire contract, the
  advisory-only guarantee, the corpus policy, the pinned runner tag, or whether facility code should be
  reachable from this service. Those are decisions with cross-repo consequences, not implementation
  details.
- Verify behaviour by measurement before asserting it, and say which you did. This repo has already had
  three plausible-looking assumptions found false in the real image: that `AlgorithmManager` was usable
  for warm-up (it raises on this build), that `Configurator` was a framework global (it does not exist),
  and that a threadpool was adequate for CPU-bound work (it measured slower than serial).
- Keep README's endpoint, contract and measurement sections in the same commit as the code they
  describe.

## Keeping this document up to date

If a change makes anything here inaccurate — a new endpoint or environment variable, a changed
directory layout, a new or renamed CI workflow, a Dependabot grouping, a change to the wire contract or
the corpus policy, a different way to run tests, or a new hard-won measurement about how Mantid, jedi,
pyflakes or the pool behave — update the relevant section in the same change. Treat it like
`README.md`: documentation that goes stale is worse than none, and the next agent will trust it.
