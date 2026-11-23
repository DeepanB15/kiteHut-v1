"""
KiteHut MCP server.

Uses FastMCP (mcp 1.x).  ``KiteHutMCPServer`` exposes the read-only
``kitehut_*`` knowledge tools plus a read-only subset of the existing agent
tools.  Tools that modify files, run commands or change git state are only
registered with ``allow_agent_tools=True`` (CLI: ``kitehut mcp --allow-agent-tools``).

``RepoLensMCPServer`` is a deprecated compatibility subclass that keeps the old
behavior (every registered tool).
"""

import logging
import typing
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from app.tools.registry import create_default_tool_registry
from app.agent.permissions.manager import PermissionManager
from app import __version__ as KITEHUT_VERSION

logger = logging.getLogger("kitehut.mcp.server")

#: Existing agent tools that cannot modify files, run commands or change git state.
READ_ONLY_AGENT_TOOLS = frozenset({
    "read_file", "search_files", "search_code", "list_files", "git_status", "git_diff", "git_log",
    "find_symbol", "get_repo_structure", "get_dependencies", "get_git_diff", "get_git_log",
    "graph_find_symbol", "graph_callers", "graph_callees", "graph_imports", "graph_dependents",
    "graph_impact", "graph_context", "verify_claim",
})


_JSON_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _json_type_to_python(json_type: Optional[str]):
    """Map a JSON-schema type name to a Python type for MCP signatures."""
    return _JSON_TYPE_MAP.get(json_type or "", Any)


def _parse_structured_output(output: Optional[str]):
    """Return parsed JSON for tools that emit structured output, else None."""
    if not isinstance(output, str):
        return None
    stripped = output.lstrip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        import json

        return json.loads(output)
    except (TypeError, ValueError):
        return None


class KiteHutMCPServer:
    """MCP server for KiteHut knowledge (and, optionally, agent tools)."""

    #: Overridden by the deprecated RepoLens subclass to keep old behavior.
    _default_allow_agent_tools = False
    _server_name = "KiteHut"
    _discovery_name = "KiteHut MCP"
    _include_knowledge_tools = True

    def __init__(self, repository_path: Optional[str] = None, allow_agent_tools: Optional[bool] = None):
        from app.knowledge.safe_io import resolve_repository_root

        self.mcp = FastMCP(self._server_name)
        self.repository_path = repository_path or "."
        self.allow_agent_tools = self._default_allow_agent_tools if allow_agent_tools is None else allow_agent_tools
        self.registry = create_default_tool_registry()
        self.permission_manager = PermissionManager()
        self._knowledge_tool_names: List[str] = []
        if self._include_knowledge_tools:
            self._register_knowledge_tools(resolve_repository_root(self.repository_path))
        self._register_all()

    def _register_knowledge_tools(self, root) -> None:
        import inspect

        from app.mcp.knowledge_tools import TOOL_SPECS, KnowledgeTools

        impl = KnowledgeTools(str(root))
        for tool_name, method_name, description in TOOL_SPECS:
            method = getattr(impl, method_name)
            # knowledge_tools uses postponed annotations; resolve them to real types for FastMCP.
            hints = typing.get_type_hints(method)
            params = [
                p.replace(annotation=hints.get(p.name, Any))
                for p in inspect.signature(method).parameters.values()
            ]
            sig = inspect.Signature(params)

            def make(m=method):
                def wrapper(**kwargs):
                    return m(**{k: v for k, v in kwargs.items() if v is not None})
                return wrapper

            fn = make()
            fn.__name__ = tool_name
            fn.__doc__ = description
            fn.__signature__ = sig
            fn.__annotations__ = {p.name: p.annotation for p in params}
            fn.__annotations__["return"] = Dict[str, Any]
            self.mcp.tool(name=tool_name, description=description)(fn)
            self._knowledge_tool_names.append(tool_name)

    def _register_all(self):
        """Register the selected agent tools as MCP tools."""
        for tool in self.registry.list_tools():
            if not self.allow_agent_tools and tool.name not in READ_ONLY_AGENT_TOOLS:
                continue
            self.mcp.tool()(self._wrap_tool(tool))

    def _wrap_tool(self, tool):
        """Create an MCP-callable wrapper for a registered tool.

        The wrapper exposes the tool's JSON schema as its own signature so the
        MCP runtime can build a correct pydantic argument model. It must not
        use ``functools.wraps``: that sets ``__wrapped__``, and
        ``inspect.signature`` would then follow through to ``tool.run`` whose
        unannotated ``**kwargs`` breaks pydantic model creation.
        """
        import inspect

        schema = tool.get_parameter_schema()
        properties = schema.get("properties", {}) or {}
        required = set(schema.get("required", []) or [])

        parameters = []
        for name, prop in properties.items():
            annotation = _json_type_to_python(prop.get("type"))
            if name in required:
                parameters.append(
                    inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation)
                )
            else:
                parameters.append(
                    inspect.Parameter(
                        name, inspect.Parameter.KEYWORD_ONLY, annotation=Optional[annotation], default=None
                    )
                )

        signature = inspect.Signature(parameters)

        def wrapper(**kwargs) -> Dict[str, Any]:
            args = {k: v for k, v in kwargs.items() if v is not None}
            # The server's repository path is the only valid target: a tool
            # argument must not be able to widen the workspace scope.
            args["repository_path"] = self.repository_path
            result = self.registry.execute(
                tool_name=tool.name,
                arguments=args,
                repository_path=self.repository_path,
                permission_manager=self.permission_manager,
                task_id="mcp_task",
            )
            if result.success:
                response: Dict[str, Any] = {
                    "success": True,
                    "output": result.output,
                    "duration_ms": result.duration_ms,
                }
                structured = _parse_structured_output(result.output)
                if structured is not None:
                    response["data"] = structured
                return response
            return {"success": False, "error": result.error, "duration_ms": result.duration_ms}

        wrapper.__name__ = tool.name
        wrapper.__doc__ = tool.description
        wrapper.__signature__ = signature
        wrapper.__annotations__ = {
            p.name: p.annotation for p in parameters
        }
        wrapper.__annotations__["return"] = Dict[str, Any]
        return wrapper

    def get_registered_tool_names(self) -> List[str]:
        agent = [
            t.name for t in self.registry.list_tools()
            if self.allow_agent_tools or t.name in READ_ONLY_AGENT_TOOLS
        ]
        return self._knowledge_tool_names + agent

    def list_tool_specs(self) -> List[Dict[str, Any]]:
        """Function-style specs for exactly the tools this server registers."""
        exposed = set(self.get_registered_tool_names())
        specs = [
            spec for spec in self.registry.get_tool_schemas()
            if spec.get("function", {}).get("name") in exposed
        ]
        for tool in self.mcp._tool_manager.list_tools():
            if tool.name in self._knowledge_tool_names:
                specs.append({"type": "function", "function": {
                    "name": tool.name, "description": tool.description, "parameters": tool.parameters}})
        return specs

    def discover(self) -> Dict[str, Any]:
        """Discovery payload: server info + available tools."""
        return {
            "server": self._discovery_name,
            "version": KITEHUT_VERSION,
            "repository_path": self.repository_path,
            "allow_agent_tools": self.allow_agent_tools,
            "tool_count": len(self.get_registered_tool_names()),
            "tools": self.get_registered_tool_names(),
        }

    def run_server(self, transport: str = "stdio"):
        self.mcp.run(transport=transport)


class RepoLensMCPServer(KiteHutMCPServer):
    """Deprecated: the former RepoLens server, behavior unchanged.

    Exposes exactly the original agent tool set (including tools that write files and
    run commands) and none of the ``kitehut_*`` knowledge tools.  New code and the
    ``kitehut mcp`` command use :class:`KiteHutMCPServer`.
    """

    _default_allow_agent_tools = True
    _include_knowledge_tools = False
    _server_name = "RepoLens Agent Tools"
    _discovery_name = "RepoLens MCP"  # legacy identity preserved for existing clients


# Convenience factory for external callers
mcp_server: Optional[KiteHutMCPServer] = None


def get_mcp_server(repository_path: Optional[str] = None) -> KiteHutMCPServer:
    """Deprecated legacy factory: returns the legacy-behavior server (see RepoLensMCPServer).

    Use ``KiteHutMCPServer(repository_path)`` directly for the read-only KiteHut server.
    """
    global mcp_server
    if mcp_server is None or (repository_path and mcp_server.repository_path != repository_path):
        mcp_server = RepoLensMCPServer(repository_path=repository_path)
    return mcp_server
