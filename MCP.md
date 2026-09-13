# MCP page

The MCP page inventories Studio's closed `MCP_ADAPTER_REGISTRY` plus native MCP
service declarations in the operator policy. It never imports another client's
MCP configuration. The bundled `project_fixture` adapter is explicitly a test
server. Policy-only entries have no executable runtime profile; the page cannot
activate them or turn policy-supplied commands/URLs into transports.

`app/mcp_inventory.py` reads configuration and maps existing tool instances to
agents. `app/mcp_health.py` performs isolated discovery probes in a bounded
background queue. `app/pg_mcp.py` renders results and polls completion without
starting another probe on a Streamlit rerun. Upstream integration is limited to
the sidebar entry and MCP origin captions in Tools.

A green status requires a valid MCP initialize response and successful tools/list
(including pagination). It does not mean a tool was executed or that all tools
are allowed. Checks never invoke tools/call, LLMs or crews. Results expire after
five minutes and are invalidated when the runtime configuration changes. Results
are shared by sessions within one Studio process and cleared on restart.

Discovery supports the existing stdio runtime adapters. Other transports and
policy-only profiles cannot receive a green status. The probe uses the same
minimal environment contract as the current runtime adapter; it does not load
credential values. Requests share a total timeout (at most 30 seconds), bounded
response size and at most ten listing pages. Each probe owns and terminates its
process group and cleans up temporary files. Raw server errors and stderr never
appear in the UI or diagnostic logs.

## Persistent operator configuration

`STUDIO_MCP_POLICY_PATH` selects the existing `studio.mcp-policy.v1` YAML file.
Local runs default to `config/mcp-policy.yaml`. The Compose fragment mounts the
host's `studio/config/` at `/etc/crewai/mcp` read-only; config and secrets are
excluded from the Docker image. Keep this host directory when updating sources.
The operator-managed files are deliberately not changed or committed by this
feature. Credentials remain references in the existing operator configuration.
The fixture's executable definition remains in the existing closed code registry.

The page is read-only: it does not change connection settings, policy permissions,
tool IDs, agent assignments or credentials. Connection activation, a tool's
presence in Tools, and assignment in Agents are distinct states. A policy entry
alone does not add an instrument to an agent. The existing registry is authoritative
when it and policy mention the same adapter.

## Updating the fork

Maintain `contentceh/local-studio` in `Contentceh/CrewAI-Studio`; fetch upstream
changes and merge them into this branch before testing and rebuilding. Do not
replace the checkout with upstream's clean files. Root deployment configuration
in the parent `crewai-panels` directory is outside this Git repository.

## Checks

Run `tests/test_mcp_health.py` and `tests/test_mcp_page.py`, together with the
existing MCP, tool ID and task/agent identity regression tests. Use disposable
storage and disable telemetry; no production database or credentials are needed.
The tests cover real subprocess discovery, failed initialization/listing,
timeouts, authentication error redaction, pagination, disabled connections,
process cleanup, stale results, background job deduplication and Streamlit reruns.
