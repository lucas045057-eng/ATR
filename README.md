# Adaptive Troubleshooting Runtime

A local, system-understanding-first SRE investigation demo. The app builds a sourced System Graph, reports disagreement across source / deployment / runtime claims, records evidence before reaching a supported diagnosis, and reuses reviewed troubleshooting Patterns across different systems.

## Safety boundary

The API and UI bind to `127.0.0.1`. All `/api/v1/` routes except the minimal health route require the configured `OPERATOR_TOKEN`; the collector uses a separate `COLLECTOR_TOKEN`. The backend has no Docker socket. A private-network collector exposes only fixed Docker reads; restart, writes, arbitrary commands and unregistered tools are not available to the agent. The collector has Docker daemon access, so a compromised collector can control the host. Run it only on a trusted local machine and do not expose its network. Sample service credentials are for the isolated demo only.

Onboarding reads repositories already located under `demo/` or `targets/`. Clone external repositories yourself before onboarding; the app will not fetch an arbitrary URL. The app never runs project source, dependency installers, hooks, build scripts or Compose commands during onboarding.

## Start

1. Copy `.env.example` to `.env` and replace both tokens with private values of at least 32 characters. Set `DOCKER_SOCKET_PATH` if your Docker Desktop / Linux setup uses a different engine socket.
2. Put a pre-cloned, read-only repository under `targets/` (or use `demo/system-a` and `demo/system-b`).
3. Start the application with `docker compose up --build` from this directory.
4. Open `http://127.0.0.1:8080`. The API health route is `http://127.0.0.1:8000/api/v1/health`. Enter the operator token in the UI access settings.

The container runtime must support mounting its Linux Docker Engine socket into the collector. The collector route is not published to the host. On this development machine Docker Desktop is installed, but its `desktop-linux` engine is stopped; the Compose workflow has not been runtime-verified.

## Model configuration

Configure `MODEL_PROVIDER`, `MODEL_BASE_URL`, `MODEL_API_KEY` and `MODEL_NAME` in `.env`. The runtime requests JSON-mode responses through LiteLLM and falls back to an explicitly unresolved offline report if the provider is absent or returns an invalid response. Configure exact read-only URLs in `ALLOWED_HEALTH_URLS`; the demo defaults point at the two private proxy service names. Keep credentials in `.env`, which is ignored by Git.

## Demo

Follow [`docs/demo-runbook.md`](docs/demo-runbook.md) to onboard both stacks, investigate A, manually repair it, review the resulting Pattern, migrate that Pattern to B, and run B's negative control. The 9 → 4 check example is a goal, not seeded output. The dashboard only reports recorded runs.

## Prototype scope

The diagnosis path collects one Compose status snapshot and bounded logs for up to eight containers, then runs a planner/reviewer loop with up to three further allowlisted log or process reads. A compatible Trusted Pattern can narrow initial log collection to role-bound checks, and it cannot establish a diagnosis without fresh evidence. See [`docs/implementation-status.md`](docs/implementation-status.md) for delivered scope and verification limits.

The approved design and implementation plan are available beside this project in the parent `outputs/` directory.
