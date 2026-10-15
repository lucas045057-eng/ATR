# Authorization flow simplification

2026-10-15; baseline: `816b0c912d87609e914cb71e4e04ab186fbca4bc`.

Removed the authorization-basis, inventory-approval-basis, endpoint-mapping-basis,
and SSH permission-attestation text boxes. Routine profile and pattern reviews
submit their explicit decision without opening a reason prompt. Revocation uses
a confirmation dialog without requesting a written explanation. Connection
timeouts and output limits use existing defaults under collapsed advanced settings.

The authenticated actor, timestamp, selected object, identity rule, decision,
and source evidence remain in audit records. Existing API clients can still
provide bounded notes; blank, omitted, and short notes are accepted. Historical
notes remain readable. No generated declaration claims that a human verified
server settings.

SSH access still requires explicit authorization, credentials, a pinned host key,
a non-root account, and a matching connector mode. Object scope, current identity,
read-only tool policies, version checks, and explicit stable-object selection
remain enforced. Written conflict resolutions and repair verification retain
their substantive content requirements. No database migration is required.

Validation covers blank-note API requests and audit records, both SSH modes,
password sessions, unchanged access gates, actual endpoint mapping, browser
onboarding without review prompts, object authorization, stable-rule upgrades,
unknown-object approval, and revocation without a written reason. The focused
backend run passed 159 tests with two independently known baseline failures in
legacy investigation/model expectations. Frontend build and scoped Ruff checks
passed; browser results and deployment evidence are recorded with the delivery.
