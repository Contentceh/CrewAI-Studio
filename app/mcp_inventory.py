"""Read-only inventory of this Studio's runtime adapters and operator policy."""

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from mcp_profiles import ProfileError, load_profiles

SERVICE_NAMES = {
    "codex_websearch": "Codex — поиск в интернете",
    "perplexity_websearch": "Perplexity — поиск в интернете",
    "neo4j": "Neo4j", "github": "GitHub", "nocodb": "NocoDB",
    "qdrant": "Qdrant", "supabase": "Supabase", "prometheus": "Prometheus",
}

POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "mcp-profiles.yaml"


@dataclass(frozen=True)
class Connection:
    adapter_id: str
    name: str
    transport: str
    enabled: bool
    registered: bool
    approved_tools: tuple = ()
    config: dict = field(default_factory=dict, repr=False)
    native: bool = False

    @property
    def fingerprint(self):
        payload = [self.adapter_id, self.enabled, self.registered, self.config]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def load_inventory(registry=None, policy_path=None):
    if registry is None:
        from mcp_tools import MCP_ADAPTER_REGISTRY
        registry = MCP_ADAPTER_REGISTRY
    path = Path(policy_path or os.getenv("STUDIO_MCP_POLICY_PATH", POLICY_PATH))
    warning = None
    try:
        profiles = load_profiles(path)
    except FileNotFoundError:
        warning = "Файл MCP-профилей отсутствует. Показаны только runtime-адаптеры."
        profiles = {}
    except (OSError, ProfileError):
        warning = "Не удалось прочитать строгий MCP-реестр. Показаны только runtime-адаптеры."
        profiles = {}
    connections = {}
    for adapter_id, profile in profiles.items():
        connections[adapter_id] = Connection(
            adapter_id, profile.get("display_name", SERVICE_NAMES.get(adapter_id, adapter_id)),
            profile.get("transport", "stdio"), profile.get("enabled", False) is True,
            True, (), dict(profile), True,
        )
    # Legacy custom adapters remain available only when no native profile owns
    # the id. Native project-owned profiles are authoritative on collisions.
    for adapter_id, config in registry.items():
        if adapter_id in connections:
            continue
        connections[adapter_id] = Connection(
            adapter_id, "MCP Fixture — тестовый сервер" if adapter_id == "project_fixture" else adapter_id,
            config.get("transport", "Не задан"), config.get("enabled", True) is True,
            True, tuple(), dict(config), native=False,
        )
    return list(connections.values()), warning


def tool_origin(tool):
    if getattr(tool, "name", None) == "MCPFixtureTool":
        return "project_fixture", "fixture.read"
    params = getattr(tool, "parameters", {})
    if isinstance(params, dict) and params.get("adapter_id") and params.get("remote_tool_name"):
        return params["adapter_id"], params["remote_tool_name"]
    return None


def tool_rows(connection, tools, agents, advertised=None):
    names = set(connection.approved_tools) | set(advertised or ())
    instances = {}
    assignments = {}
    for tool in tools:
        origin = tool_origin(tool)
        if origin and origin[0] == connection.adapter_id:
            names.add(origin[1])
            instances.setdefault(origin[1], []).append(tool.tool_id)
    for agent in agents:
        for tool in getattr(agent, "tools", ()):
            origin = tool_origin(tool)
            if origin and origin[0] == connection.adapter_id:
                names.add(origin[1])
                assignments.setdefault(origin[1], set()).add(f"{agent.role} ({agent.id})")
    return [{
        "Инструмент": name,
        "Объявлен сервером": ("Да" if name in advertised else "Нет") if advertised is not None else "Не проверено",
        "Доступен full-access профилю": "Да" if connection.enabled and connection.registered else "Нет",
        "В Tools": ", ".join(instances.get(name, ())) or "Не добавлен",
        "Агенты": ", ".join(sorted(assignments.get(name, ()))) or "Не назначен",
    } for name in sorted(names)]
