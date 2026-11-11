# mantid-python-script-lsp

Server-side Python analysis for the FIA script editors: it is what lets the reduction-script and
live-data-script editors in [`frontend`](../frontend) show real squiggles on Mantid scripts.

This is a stateless HTTP facade over Python's own parser plus the analysis environment the runners
already have — **not** a language server. There is no session, no LSP socket and no document state:
each request carries a script, and the response is a list of diagnostics.

## Why this exists as a service

The scripts users write here are not ordinary Python. The runner brings roughly 1,800 names into scope
without an import — every algorithm, plus `mtd`, `config`, `ConfigService` — because it does
`from mantid.simpleapi import *`, and most of those algorithm functions do not exist as objects until
the framework generates them at runtime. Facility helpers arrive a second way, by explicit import
(`import reduction_utils`, `from isis_powder... import ...`), from code this service deliberately does
not install. A linter in the browser can see syntax; it can see none of that, and a checker that
reports `Undefined name 'Load'` on every real script is worse than no checker at all. The only
environment that can answer those questions correctly is the one that runs the scripts — the Mantid
runner image — so the check runs there.

Diagnostics are **advisory**. Nothing here blocks Save or "Rerun with changes"; the frontend treats a
slow or unreachable service as "unknown" and gets out of the way.

## What gets reported, and what deliberately does not

| | reported? | why |
| --- | --- | --- |
| Anything `compile()` rejects | error | it is the runner's own parser: this script cannot run, whatever the framework provides |
| Syntax errors `compile()` reaches too late | error, from jedi | a brace opened at line 144 is the mistake; CPython only notices at 149 |
| A name no one provides | warning | `BinMD` typed as `BinMQ` — the check nothing else in FIA can do |
| Unused imports, unused variables, style | never | measured as 2-3 to 35 messages per real script; commentary on someone's analysis code, in the colour reserved for broken code |
| Any name in a script that does not parse | never | half-typed code produces an undefined name per name; the syntax error is the whole answer |
| Anything at all in a script that star-imports code we do not model | never | see below |

The last row is the one worth explaining. A script may do `from isis_powder import *`, pulling in names
from facility code this service deliberately does not install, so their existence cannot be checked. Left
in place, pyflakes hedges every unresolved name in that script as `ImportStarUsage`, which is not
reported — the service says nothing about the whole script rather than inventing three faults in it and
missing the real one. Rewriting that import away reported `undefined name 'load_grouping_file'` on a
script that runs correctly. Silence about what cannot be known beats being confidently wrong: one false
positive costs the credibility of every warning beside it.

## Position in FIA

```text
frontend (Monaco Python editors)
      │  POST /script/analyse   { content, contentHash, instrument }
      ▼
FIA-API  — authenticates, caps size, rate-limits, caches, 5 s upstream timeout
      │  POST /analyse
      ▼
this service — compile()'s verdict, jedi's syntax coverage, pyflakes undefined names
              over a preamble generated from the running framework
```

The frontend never talks to this service directly: routing through FIA-API keeps auth, rate limiting
and the size cap in one place, and means this service never has to know who a user is.

## Positions

Every position in the request and response is **1-based lines and 1-based columns, counted in UTF-16
code units** — Monaco's own coordinate system, not an LSP import of it. Its typings are explicit:
`IPosition.lineNumber` "starts at 1", and `IPosition.column` is described as "the first character in a
line is between column 1 and column 2". Diagnostic fields deliberately mirror `IMarkerData`
(`startLineNumber`, `startColumn`, `endLineNumber`, `endColumn`, `severity`, `message`, `source`) so the
frontend is a field copy rather than a translation layer, and `endColumn` is exclusive the way Monaco
treats it.

Four parsers disagree about what a column is, which is why `script_lsp/positions.py` exists as the single
place that knows: CPython reports code-point offsets, jedi reports code-point columns, pyflakes reports
UTF-8 **byte** offsets, and Monaco wants UTF-16 units. For `x = "😀😀" + class`, CPython's offset for
`class` is 12 and the squiggle belongs at columns 14–19.

## Who holds a copy of the contract

`script_lsp/models.py` is canonical. Three repositories hold a copy of it, and that duplication is
deliberate: it keeps this service deployable on its own, and it turns a breaking change into a
three-repository change rather than a runtime surprise.

| repository | what it holds |
| --- | --- |
| here | `AnalyseRequest`, `Diagnostic`, `AnalyseResponse` — canonical |
| `../FIA-API` | `AnalyseRequest` only, for validation and rate-limit keying; the response is passed through untouched |
| `../frontend` | a TypeScript mirror of `Diagnostic` / `AnalyseResponse` |

`severity` is `error` or `warning`, and those map onto Monaco's marker severities — red and orange.
There is no third level: a grey squiggle for something the user cannot act on trains them to ignore the
two that matter.

## Endpoints

| endpoint | meaning |
| --- | --- |
| `GET /healthz` | Process up. Independent of Mantid on purpose: a slow warm-up must not look like a crash loop. |
| `GET /ready` | 200 once the framework has answered a real algorithm *and* every analysis worker has completed a probe; 503 with the reason until then. |
| `POST /analyse` | Diagnostics for one script. See `script_lsp/models.py` for the shape. |

`/analyse` answers `200` (analysed), `422` (rejected: over the byte cap, or no `content_hash`) or `503`
(still warming up). FIA-API forwards the status and body unchanged and the frontend branches on the
status: `503` means keep the previous squiggles and retry shortly, not "this script is clean".

Measured in the built image, against the real reduction-script corpus described under
[Measuring](#measuring):

| request | result |
| --- | --- |
| `MERLIN.py` — 11 KB, 286 lines | `parses: true`, **0 diagnostics** |
| `MERLIN_star.py` — the same script, framework imported with `*` | **0 diagnostics** (naive pyflakes reports 14 here) |
| `MERLIN_x8.py` — 90 KB, 2,302 lines, beyond anything real | 0 diagnostics |
| `MERLIN_broken.py` | 2 diagnostics: the cause at 144 from jedi, the effect at 149 from `compile()` |
| 199,999 bytes | 200 |
| 240,000 bytes | 422, before anything parses it |

The corpus is generated from scripts pulled off a live FIA API — instrument configurations, sample
environment details and archive paths that are not ours to publish — so it lives in `spike/corpus/`, is
gitignored, and never appears in a PR or an issue. A fresh checkout has no corpus: tests use synthetic
scripts shaped like real ones, and the real-corpus check is run locally by whoever holds a script. The
requirement it enforces is that a clean real script produces **zero** diagnostics.

## Measurements

These are the numbers the deployment and the editor were sized from. Reproduce with
`spike/bench.py` and `spike/make_corpus.py` in the built image; when you change anything that affects
the cost of an analysis — a base-image bump, a new parser, the preamble, the pool — re-run them rather
than editing the tables.

**Warm-up, once per process:** `import mantid` is 174 ms warm and 1,064 ms cold; `from mantid.simpleapi
import *` is 2.2 s warm, and 10 s once with a cold page cache. Steady-state RSS is 260 MB, peaking at
291 MB. Container start to `/ready` 200 is 3.7–5.1 s including the generated names and the probe
analysis in every worker.

**Per request:** cost is linear at about **0.12 ms per KB** — 1.2 ms median for the real 11 KB script,
11 ms for 90 KB. Importing Mantid changes none of it (467 ms before the import, 461 ms after, over the
same corpus).

**Concurrency, and why the analysis runs in processes:** the same 11 KB script analysed 24 times, in one
process:

| execution model | total | throughput |
| --- | --- | --- |
| one at a time | 373 ms | 64 /s |
| 24 threads (what was written first) | 1,747 ms | **13.7 /s** |
| 8 worker processes | 97 ms | **247 /s** |

Threads are 4.6x slower than doing the work serially: `ast.parse` and pyflakes never release the GIL, so
they thrash it instead of overlapping. Processes give 18x the threadpool. The property that makes the
pool cheap is that request-time analysis never touches Mantid — it needs only the 90 lines of preamble
text, so workers cost tens of megabytes rather than the parent's quarter-gigabyte.

Trust those three rows; be sceptical of latencies measured over HTTP on a developer machine, which moved
between 27 ms and 108 ms for the same configuration across runs. The in-process comparisons reproduced to
a few percent. Size the Deployment from the real cluster, not from these.

**jedi's first completion costs 1.9 s** in a process (6.8 ms afterwards, and 26 MB for the index).
`/analyse` does not pay it — `get_syntax_errors()` only parses — but `/complete` will, and it belongs in
warm-up rather than in a user's first keystroke.

### What they decide

- `startupProbe` needs a ceiling around 60 s: a normal cold start is under 6 s, but a cold page cache
  plus a CPU-throttled node is not, and killing a pod for warming slowly turns slow starts into restart
  loops.
- `resources.requests` of 1 CPU / 1 Gi with a 2 Gi limit: 260 MB steady, 291 MB peak, and the rest is
  headroom for jedi and pyflakes on a large script.
- The 200 KB cap bounds the worst case near 25 ms of CPU against FIA-API's 5 s upstream timeout — two
  orders of magnitude of headroom, so that timeout guards against a wedged process, not a big script.
- **The 1,000 ms editor debounce is not a server constraint.** At the cap a check costs ~25 ms. The
  debounce is there for request volume and for not moving squiggles under a cursor; if typing feels
  sluggish, 300 ms is affordable without touching this service.
- One uvicorn worker per pod, scaling by pods. `uvicorn --workers N` would pay N warm-ups to share
  nothing that pods don't already share, and make readiness ambiguous.

## Configuration

| variable | default | what it does |
| --- | --- | --- |
| `ANALYSIS_WORKERS` | one per core of the pod's CPU quota, else `min(4, cores)` | How many analysis processes to run. Workers above the CPU quota get CFS-throttled in ~100 ms bursts, which turns a throughput question into a latency spike. |
| `MAX_SCRIPT_BYTES` | 200,000 | The request size cap, in **bytes**. It must be set to the same value as FIA-API's own cap or one of them starts rejecting scripts the other accepted. |
| `DIAGNOSTIC_LIMIT` | 100 | How many diagnostics come back, ordered by position, with `truncated: true` beyond it. |

A value that is not a positive integer is logged and ignored rather than honoured: a typo in a manifest
must not produce a service that rejects every script or reports nothing.

## Base image

`container/mantid_python_script_lsp.D` is `FROM ghcr.io/fiaisis/mantid:<tag>`, pinned to a runner tag,
and the dependency pins in `pyproject.toml` have to be satisfiable by that image's interpreter. This
repository is therefore implicitly coupled to `../runners`: moving the tag changes which names a script
may use, what warm-up costs, and which algorithms exist. It is a deliberate act with a corpus check and a
re-measurement, never a rebuild side effect.

One consequence to keep an eye on: the pins here resolve pydantic 2.13.4, which *downgrades* the 2.14.2
that comes in the runner image. Nothing observable broke, but the base image is not ours to quietly
change — if a future bump makes that matter, the question is whether this service should pin pydantic at
all or inherit the base's.

Not yet measured, and worth knowing before sizing anything: behaviour under sustained load with the pool
in place, the 200 KB cap-sized script at full concurrency, and jedi with its completion dictionary built.

## Repository structure

```text
script_lsp/
  app.py                        FastAPI app: lifespan, warm-up gate, /healthz and /ready
  warmup.py                     Imports the framework, proves it, generates the names, starts the pool
  pool.py                       Where the analysis runs: a forkserver process pool, sized from the cgroup
  models.py                     THE wire contract - canonical, see "Who holds a copy of the contract"
  positions.py                  CPython/jedi/pyflakes coordinates to Monaco's (UTF-16, 1-based)
  preamble.py                   Framework names made visible to static analysis, and the offset they add
  syntax.py                     `compile()` + jedi merged into diagnostics
  lint.py                       pyflakes, filtered to undefined names, mapped back to the script
  analyse.py                    `POST /analyse`
test/                           Pytest suite, mirroring the package
container/                      Dockerfile (`mantid_python_script_lsp.D`), built FROM a pinned runner tag
spike/                          The measurement scripts behind "Measurements" above
pyproject.toml                  Dependencies (pinned), ruff and pytest config
mypy.ini                        mypy --strict, with the C-extension imports exempt
```

## Local development

The unit suite does not need Mantid — the framework is stubbed — but the running service does, and
`requires-python` has to match the interpreter in the runner image. A conda env on the host is the
practical loop for tests and linting:

```shell
mamba create -n mantid-python-script-lsp python=3.13 -y
pip install -e '.[all]'

pytest test --random-order --random-order-bucket=global
ruff check . && ruff format .
mypy --strict script_lsp
```

`script_lsp` is at 100% statement and branch coverage and the two exceptions to that are marked
`# pragma: no cover` with a reason (they need the framework). New code is expected to come in covered;
the parts that cannot be, run in the image instead.

Do not run pytest/ruff/mypy with the repo bind-mounted into a container: they write caches into the
working tree as root and the next host run fails with "attempt to write a readonly database". Use a
container only when Mantid is genuinely required.

### Build and run the image

```shell
docker build -f container/mantid_python_script_lsp.D -t mantid-python-script-lsp:local .
docker run --rm -p 8080:80 mantid-python-script-lsp:local   # the image listens on 80, like other FIA services

curl -i localhost:8080/healthz    # always 200: the process is up
curl -i localhost:8080/ready      # 503 while the framework is warming, then 200 with the seconds it took
```

Kubernetes owns liveness and readiness — there is deliberately no `HEALTHCHECK` in the image, because a
Docker-level check can mask a framework that is still warming.

Merging to `main` builds and publishes the image, then pins the resulting digest into this service's
staging manifest in [`../gitops`](../gitops) — the same route every other FIA service takes. Because the
base image is far too large to build on every branch push, CI's PR checks are the unit suite, ruff and
mypy only: **changes to the Dockerfile or the dependency pins must be built locally before merging**,
because nothing on a branch will tell you they broke.

### Measuring

```shell
python spike/make_corpus.py /path/to/a/real/script.py   # writes spike/corpus/ (gitignored)
python spike/bench.py                                   # inside the image: it needs the framework
```

`spike/` is kept rather than discarded because the tables above are derived from it: anything that
changes the cost of an analysis means re-running it and updating this README.
