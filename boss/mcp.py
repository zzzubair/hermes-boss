"""Streamable HTTP MCP without SDK dependencies or unsafe mutation retries."""
import json
import socket
import urllib.error
import urllib.request

from .common import BossError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class MCP:
    def __init__(self, url, token, redact, timeout=30):
        self.url, self.token, self.redact, self.timeout = url, token, redact, timeout
        self.session, self.protocol, self.sequence, self.tools = None, "2025-03-26", 0, None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def rpc(self, method, params, notification=False):
        self.sequence += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = self.sequence
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers.update({"Mcp-Session-Id": self.session, "MCP-Protocol-Version": self.protocol})
        request = urllib.request.Request(self.url, json.dumps(payload).encode(), headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                session = response.headers.get("mcp-session-id")
                if session:
                    self.session = session
                if notification:
                    return {}
                if response.headers.get_content_type() == "text/event-stream":
                    result = self._sse(response, payload["id"])
                else:
                    result = json.load(response)
        except urllib.error.HTTPError as exc:
            # Never echo response bodies, URLs or headers: upstream may echo tokens.
            hint = "check token and host URL; boss status to reconcile before retrying mutations"
            raise BossError("transport", f"MCP HTTP {exc.code} during {method}", hint) from None
        except (OSError, socket.timeout, ValueError) as exc:
            raise BossError("transport", f"MCP {method}: {self.redact(exc)}",
                            "boss status; reconcile before retrying mutations") from None
        if not isinstance(result, dict) or result.get("id") != payload["id"]:
            raise BossError("protocol", f"invalid MCP response for {method}")
        if "error" in result:
            raise BossError("t3", f"{method}: {self.redact(result['error'])}")
        if not isinstance(result.get("result"), dict):
            raise BossError("protocol", f"missing or invalid MCP result for {method}")
        return result["result"]

    @staticmethod
    def _sse(response, request_id):
        data = []
        for raw in response:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif not line and data:
                item = json.loads("\n".join(data))
                data = []
                if item.get("id") == request_id:
                    return item
        raise ValueError("SSE stream ended before the response")

    def connect(self):
        result = self.rpc("initialize", {"protocolVersion": self.protocol, "capabilities": {},
                                         "clientInfo": {"name": "boss", "version": "0.1.0"}})
        self.protocol = result.get("protocolVersion", self.protocol)
        if not self.session:
            raise BossError("protocol", "initialize omitted mcp-session-id")
        self.rpc("notifications/initialized", {}, notification=True)
        self.tools = {}
        cursor = None
        while True:
            result = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            for tool in result.get("tools", []):
                self.tools[tool["name"]] = tool["inputSchema"]
            next_cursor = result.get("nextCursor")
            if not next_cursor:
                break
            if next_cursor == cursor:
                raise BossError("protocol", "tools/list repeated its cursor")
            cursor = next_cursor

    def call(self, name, **arguments):
        if self.tools is None:
            self.connect()
        schema = self.tools.get(name)
        if schema is None:
            raise BossError("capability", f"T3 does not expose {name}", "check host and T3 version")
        missing = set(schema.get("required", [])) - arguments.keys()
        unknown = arguments.keys() - schema.get("properties", {}).keys()
        if missing or (unknown and schema.get("additionalProperties") is False):
            raise BossError("schema", f"{name} schema mismatch", "check T3 version")
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise BossError("t3", f"{name}: {self.redact(result.get('content', []))}")
        data = result.get("structuredContent")
        if data is None:
            for block in result.get("content", []):
                if block.get("type") == "text":
                    try:
                        data = json.loads(block["text"])
                        break
                    except ValueError:
                        continue
        if not isinstance(data, dict):
            raise BossError("protocol", f"{name} returned no structured object")
        return self.redact.data(data)
