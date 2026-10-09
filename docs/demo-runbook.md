# Two-system investigation runbook

This runbook uses two isolated example stacks. System A is Python + MySQL + Nginx; System B is Express + PostgreSQL + Nginx. They use different service names but share the `proxy`, `application`, and `datastore` graph roles. The demo data is disposable.

## 1. Start the workbench and clean baselines

From the project directory, copy `.env.example` to `.env` and replace both sample tokens with distinct private random values of at least 32 characters. Do not commit `.env`. Start the workbench first so it creates the private Docker network:

```sh
docker compose up --build -d
```

Start both example systems:

```sh
docker compose -f demo/system-a/compose.yaml -p atr-system-a up --build -d
docker compose -f demo/system-b/compose.yaml -p atr-system-b up --build -d
```

Wait for the database health checks, then confirm the read-only endpoints:

```sh
curl http://127.0.0.1:18080/health
curl http://127.0.0.1:18080/record
curl http://127.0.0.1:18081/health
curl http://127.0.0.1:18081/record
```

In the workbench at `http://127.0.0.1:8080`, set the operator token from `.env`. On **系统图谱**, onboard `system-a` and `system-b`, each with `compose.yaml`. The Compose project names match the explicit `-p` names above so the private collector can map runtime containers.

## 2. Create and verify a Candidate from System A

On **故障调查**, create an incident for System A with a symptom such as “外部 health 请求在应用重启后返回 502”. Select **Graph + Pattern** mode if a prior Pattern already exists; for the first run it behaves as graph-only. Start the read-only investigation and review the run timeline, current container state, logs, graph version, and evidence gate.

The supported state is deliberately gated. If the runtime is unavailable or there are fewer than two independent current observations, the result stays **unresolved**. Resolve the issue manually and start a fresh incident/run if needed; historical Pattern data never substitutes for current evidence.

After a supported run, restore the baseline and record the operator-performed repair and a concrete post-repair verification in the run panel. The workbench performs a fresh read-only GET against the exact URL in `ALLOWED_HEALTH_URLS`; the Candidate is created only when that check returns HTTP 200 and the operator confirms the record. Review the exact role constraints, symptom terms, check sequence, and digest on **知识审核**. Promote only if the diagnosis, repair, verification, and intended transfer scope are accurate; rejection is equally valid.

## 3. Inject the database failure in System A

Fault injection is an operator action and is intentionally absent from the Agent tool catalog. First record a clean baseline. Then stop A’s database and restart only the application container:

```sh
docker compose -f demo/system-a/compose.yaml -p atr-system-a stop mysql
docker compose -f demo/system-a/compose.yaml -p atr-system-a restart python_api
```

The Python service checks its required database at process startup and exits when it cannot connect. Nginx remains up and should return HTTP 502:

```sh
curl -i http://127.0.0.1:18080/health
```

Create an incident in the UI describing the observed 502 and run a read-only investigation. The collector can inspect Compose state and bounded logs only; it cannot restart services or issue SQL.

## 4. Repair A manually and record verification

Restore the database and application as the operator:

```sh
docker compose -f demo/system-a/compose.yaml -p atr-system-a up -d mysql python_api
```

Wait for MySQL health and verify both endpoints return HTTP 200 and the seeded `demo-1` record. In the supported incident, record the repair and verification result. The workbench creates a Candidate; a human reviewer then decides whether to promote it.

## 5. Test role-based transfer to System B

Before transfer, confirm B’s baseline endpoints on port 18081. Create an incident with the same failure signature on System B and choose **Graph + Pattern**. A Trusted Pattern can be suggested only when role constraints bind to B’s current graph and deterministic symptom/topology/signal gates pass. A suggested Pattern narrows the role-bound log reads; the diagnosis still depends on fresh evidence collected from B.

Run B’s negative control by repeating the database outage with B’s database and application names:

```sh
docker compose -f demo/system-b/compose.yaml -p atr-system-b stop postgres_store
docker compose -f demo/system-b/compose.yaml -p atr-system-b restart catalog_api
curl -i http://127.0.0.1:18081/health
```

The gateway should remain running and return HTTP 502. Restore B manually:

```sh
docker compose -f demo/system-b/compose.yaml -p atr-system-b up -d postgres_store catalog_api
```

Record whether the suggested Pattern’s checks were validated and useful in the investigation panel. These operator outcomes feed the validation and usefulness metrics; a suggestion alone does not count as a successful reuse.

## 6. Record a paired Graph / Graph + Pattern comparison

On **效率分析**, create an evaluation case for one system and enter its operator-known root-cause label. That label is stored only in the evaluation-case table; run APIs and model prompts do not receive it. Use the same incident symptom and graph snapshot for both arms. Create one incident in `graph_only` mode and one in `graph_pattern` mode, returning the stack to the same documented baseline before each run. Record the two run IDs, pair key, repetition and operator-scored verdicts as one atomic pair.

The UI selects the case fixture digest as the pair's reset digest. It fingerprints the checked-out source snapshot and Compose file, but it cannot prove that a live Docker volume or container was actually reset. The operator must verify the reset procedure; do not treat the digest alone as proof of an identical runtime state. The dashboard reports only recorded calls, steps, support outcomes and time measurements; it never seeds a 9 → 4 result.

## 7. Reset and stop

```sh
docker compose -f demo/system-a/compose.yaml -p atr-system-a down -v
docker compose -f demo/system-b/compose.yaml -p atr-system-b down -v
docker compose down
```

The `-v` flags on the example stacks delete only their disposable database volumes. The workbench SQLite volume is separate and is preserved by the final command. To erase workbench history too, pass `-v` to the root `docker compose down` after confirming that the history is no longer needed.

## Safety and limitations

- The API and UI publish to loopback only. The collector has no host-published port.
- The backend container has no Docker socket. The collector has Docker daemon access for fixed reads; a compromised collector could control the host daemon. Run only on a trusted local machine and do not expose its network.
- Repository onboarding is static and bounded. It never runs project code, package installs, hooks, or Compose commands.
- Fault injection and repair remain operator-only and are absent from the agent tool catalog.
- Docker Desktop is installed, but the local `desktop-linux` engine is stopped, so neither Compose stack nor the HTTP workflow was started or runtime-verified here.
- No simulated “9 checks to 4” result is seeded; efficiency numbers come from persisted runs and tool calls.
