"""Broker-agnostic MCP client core (streamable-HTTP JSON-RPC).

MCP is a standard protocol, so the plumbing — initialize handshake, session-id
header, SSE/JSON response parsing, tools/call — is identical for every broker's
server. Broker specifics (endpoint URL, auth headers, tool names) live in thin
subclasses: kite_mcp.KiteMCPClient, upstox_mcp.UpstoxMCPClient, ...
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import requests

_PROTOCOL = "2025-03-26"


class MCPError(RuntimeError):
    pass


class NotLoggedIn(MCPError):
    """The MCP session exists but isn't authorised yet."""


class MCPHTTPClient:
    """Minimal MCP-over-HTTP client. Subclasses set `url`, may add auth headers
    via `_auth_headers()`, and persist the session id under `session_meta_key`."""

    session_meta_key: str = ""

    def __init__(self, store=None, url: str = ""):
        self.url = url
        self.store = store
        self.session_id: Optional[str] = None
        self._rpc_id = 0
        if store is not None and self.session_meta_key:
            self.session_id = store.get_meta(self.session_meta_key)

    # -- hooks for subclasses ------------------------------------------------
    def _auth_headers(self) -> dict[str, str]:
        return {}

    # -- plumbing ------------------------------------------------------------
    def _post(self, payload: dict, timeout: int = 12) -> Optional[dict]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._auth_headers(),
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        resp = requests.post(self.url, json=payload, headers=headers, timeout=timeout)
        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sid and sid != self.session_id:
            self.session_id = sid
            if self.store is not None and self.session_meta_key:
                self.store.set_meta(self.session_meta_key, sid)
        if resp.status_code == 401:
            raise NotLoggedIn(f"MCP 401: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise MCPError(f"MCP HTTP {resp.status_code}: {resp.text[:200]}")
        return self._parse_body(resp)

    @staticmethod
    def _parse_body(resp: requests.Response) -> Optional[dict]:
        text = resp.text or ""
        if not text.strip():
            return None
        if "text/event-stream" in resp.headers.get("Content-Type", ""):
            last = None
            for line in text.splitlines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data:
                        try:
                            msg = json.loads(data)
                            if "result" in msg or "error" in msg:
                                last = msg
                        except json.JSONDecodeError:
                            continue
            return last
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def _rpc(self, method: str, params: Optional[dict] = None) -> Any:
        self._rpc_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method}
        if params is not None:
            payload["params"] = params
        msg = self._post(payload)
        if msg is None:
            raise MCPError(f"empty MCP response for {method}")
        if "error" in msg:
            raise MCPError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result")

    def _notify(self, method: str) -> None:
        try:
            self._post({"jsonrpc": "2.0", "method": method})
        except MCPError:
            pass

    # -- session -------------------------------------------------------------
    def connect(self) -> dict:
        self.session_id = None
        result = self._rpc("initialize", {
            "protocolVersion": _PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "portfolio-pulse", "version": "0.1.0"},
        })
        self._notify("notifications/initialized")
        return result.get("serverInfo", {})

    def ensure_session(self) -> None:
        if self.session_id:
            try:
                self._rpc("tools/list")
                return
            except MCPError:
                pass
        self.connect()

    # -- tools ---------------------------------------------------------------
    def list_tools(self) -> list[dict]:
        return list(self._rpc("tools/list").get("tools", []))

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> str:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        chunks = [c.get("text", "") for c in result.get("content", [])
                  if c.get("type") == "text"]
        text = "\n".join(chunks).strip()
        if result.get("isError"):
            if re.search(r"log\s*in|login|session|authoris|authoriz|unauthor", text, re.I):
                raise NotLoggedIn(text[:300])
            raise MCPError(text[:300])
        return text

    @staticmethod
    def _json(text: str) -> Any:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"[\[{].*[\]}]", text, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        raise MCPError(f"unparseable tool reply: {text[:200]}")
