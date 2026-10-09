# Implementation status

This is a local prototype for system-graph-first troubleshooting with operator-reviewed reusable Patterns. The backend, collector, four-page workbench, SQLite history, paired-evaluation entry flow, and two example Compose stacks are present.

## Delivered

- Static onboarding reads the registered Compose and source files, records provenance and graph versions, and keeps unresolved conflicts visible.
- The collector exposes fixed, bounded Docker reads. Backend tool calls are allowlisted, redacted, persisted before execution, and linked to evidence records.
- `/api/v1/` reads and writes require the operator bearer token except for the minimal health endpoint. Collector and backend redact common authorization headers, bearer/basic tokens, secret-valued fields, credentialed URLs, JWT-shaped tokens, and common AWS access keys before persistence/model use.
- Diagnosis reports go through a deterministic evidence gate. A report remains unresolved unless it cites at least two distinct current tool calls bound to the exact target service or a declared dependency, includes a time-windowed failure signal on that scope, names a current graph node, and has no open graph conflict. The gate checks identity, scope, freshness, and signal presence; the support/refute label is model-attributed, so this prototype cannot prove that the cited observation semantically supports the hypothesis.
- Pattern capture requires an operator-confirmed diagnosis and repair plus a successful allowlisted health GET. Reviews bind to the exact Pattern digest; changed system snapshots stale the prior binding.
- Ground truth is stored in the evaluation-case table and omitted from normal case, run, and metrics responses. Paired runs must target the case system, use separate runs and matching modes, and use the case fixture digest as their reset digest.
- A compatible Trusted Pattern can narrow log collection to checks bound to current graph service roles before synthesis; those logs remain current evidence. The UI records Pattern validation/usefulness outcomes and displays run-derived metrics and paired evaluations without seeded performance figures.
- System A (FastAPI + MySQL + Nginx) and System B (Express + PostgreSQL + Nginx) have separate operator-controlled failure and repair procedures.

## Limits

- The run takes a Compose status snapshot and bounded logs for up to eight containers, then performs up to three model-directed, allowlisted follow-up reads. Each iteration records its proposed and executed action, resulting evidence, and reviewer outcome. A Pattern can narrow the initial log targets. The loop is bounded to container logs/process summaries; it does not yet include network diagnostics or adaptive tool discovery.
- Model capability probing and a general network/TCP tool are not implemented. The current integrations use JSON-mode LiteLLM, fixed collector reads, and exact-URL allowlisted health GETs.
- A fixture digest fingerprints checked-in source and Compose inputs; it cannot prove the live container or volume was reset. The operator must verify the baseline manually.
- Source inspection produces bounded code claims, but source-derived dependency edges and comprehensive reconciliation across code, deployment, and runtime claims remain partial.
- Docker Desktop is installed, but its local `desktop-linux` engine is stopped and reports a WSL vsock dead-network error. The Compose stacks and HTTP workflow were not started or runtime-verified.

## Static verification performed

- Python files compiled with `compileall`.
- The frontend TypeScript check and production bundle completed with `npm run build`.
- The Python dependency lock passed `uv lock --check`.
- The root and both demo Compose YAML files parsed with PyYAML.

No test suite was run. Static checks do not establish that container startup, network connectivity, or UI-to-API flows work in a live Docker environment.
