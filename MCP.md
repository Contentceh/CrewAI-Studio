# MCP page

The MCP page inventories Studio's closed `mcp-profiles.yaml` registry plus the
native MCP runtime. It never imports another client's MCP configuration. The
bundled `project_fixture` adapter is explicitly a test server. Only registry
profiles can provide executable transports; UI data cannot provide commands,
URLs, headers, or credentials.

`app/mcp_inventory.py` reads configuration and maps existing tool instances to
agents. `app/mcp_health.py` performs isolated discovery probes in a bounded
background queue. `app/pg_mcp.py` renders results and polls completion without
starting another probe on a Streamlit rerun. Upstream integration is limited to
the sidebar entry and MCP origin captions in Tools.

A green status requires a valid MCP initialize response and successful tools/list
(including pagination). Checks never invoke tools/call, LLMs or crews. Results
expire after five minutes and are invalidated when the runtime configuration
changes. An enabled profile exposes every tool advertised by its server; no
tool-name, resource, capability, or destructive-action filter is applied.

Discovery supports project-owned stdio profiles. Native health resolves the
profile id through the same strict command/args/environment builder as runtime
spawn; it does not accept commands from UI state. Requests share a total timeout
(at most 30 seconds), bounded response size and at most ten listing pages. Each
probe owns and terminates its process group. Raw server errors and stderr never
appear in the UI or diagnostic logs.

## Persistent operator configuration

`STUDIO_MCP_POLICY_PATH` selects the strict `studio.mcp-profiles.v1` YAML file.
Local runs default to `config/mcp-profiles.yaml`. The Compose fragment mounts the
host's `studio/config/` at `/etc/crewai/mcp` read-only; config and secrets are
excluded from the Docker image. Keep this host directory when updating sources.
Credentials are file references only and are loaded into a minimal child
environment immediately before spawn. Values are never rendered, serialized, or
logged.

The page is read-only: it does not change connection settings, policy permissions,
tool IDs, agent assignments or credentials. Connection activation, a tool's
presence in Tools, and assignment in Agents are distinct states. A profile record
alone does not add an instrument to an agent. The native profile registry is
authoritative when it and the legacy runtime inventory mention the same id.

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

The browser regression is `tests/browser_mcp.cjs`. Run with `playwright-core`
available to Node and `STUDIO_MCP_TEST_URL` pointing to an isolated Studio instance
(with a trailing slash). `STUDIO_MCP_SCREENSHOT` optionally saves a screenshot.
`STUDIO_MCP_TEST_CREATE_TOOL=1` adds a fixture instance to that test database;
leave it unset for a read-only inventory check. The test triggers only MCP
health discovery and never starts a crew.
