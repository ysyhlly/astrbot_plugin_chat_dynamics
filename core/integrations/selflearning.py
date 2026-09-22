"""Read-only Self Learning Hub v1 client for pipeline-bypassing requests.

``context`` returns only social/jargon/few_shots background text (4096 chars each).
Callers must treat that text as untrusted background, never as system instructions.
No long-term memory, ingest, learning or native-hook orchestration lives here.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit, urlunsplit

import aiohttp

# The one host rule every credential-carrying integration client applies.
from .net_policy import is_loopback_host as _is_loopback_host


class _HubError(Exception):
    pass


class SelfLearningHubClient:
    BASE = "/api/hub/v1"
    MAX_BODY = 256 * 1024

    def __init__(self, url: str = "", api_key: str = "", timeout: float = 2.0):
        self.timeout = max(0.01, min(float(timeout), 30.0))
        self._generation = 0
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self.configure(url, api_key)

    def configure(self, url: str, api_key: str) -> None:
        """Invalidate changed configuration; preserve discovery on routine refresh."""
        if self._closed:
            # A closed client must not claim to be configured while every request
            # short-circuits to the fallback.
            return
        if (url and getattr(self, "_configured_input", None) == (url, api_key) and self._url):
            return
        self._configured_input = (url, api_key)
        self._generation += 1
        for task in tuple(self._tasks):
            task.cancel()
        self._url = ""
        self._key = str(api_key or "")
        self._capabilities: dict[str, bool] = {}
        self._status = "missing"
        self._detail = "not_configured"
        if not url:
            return
        try:
            parts = urlsplit(str(url).strip())
            if (parts.scheme not in {"http", "https"} or not parts.hostname
                    or parts.username is not None or parts.password is not None
                    or parts.query or parts.fragment
                    or parts.path.rstrip("/") not in {"", self.BASE}):
                raise ValueError
            _ = parts.port
            if self._key and parts.scheme == "http" and not _is_loopback_host(parts.hostname or ""):
                # The key would travel in clear text to a remote host. Loopback
                # hubs keep working, and a keyless remote hub stays usable.
                self._status, self._detail = "degraded", "insecure_cleartext"
                return
            self._url = urlunsplit((parts.scheme, parts.netloc, self.BASE, "", ""))
        except (ValueError, TypeError):
            self._status, self._detail = "degraded", "invalid_url"
            return
        self._status, self._detail = "configured", "not_discovered"

    def snapshot(self) -> dict:
        return {"configured": bool(self._url), "available": self._status == "available",
                "status": self._status, "detail": self._detail,
                "error_code": self._detail if self._status == "degraded" else "",
                "capabilities": dict(self._capabilities)}

    async def _request(self, session, method, path, payload=None):
        async with session.request(method, self._url + path, json=payload,
                                   allow_redirects=False) as response:
            if response.status in {401, 403}:
                raise _HubError("unauthorized")
            if 300 <= response.status < 400:
                raise _HubError("redirect_rejected")
            if response.status != 200:
                raise _HubError("http_error")
            body = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                body.extend(chunk)
                if len(body) > self.MAX_BODY:
                    raise _HubError("response_too_large")
            try:
                envelope = json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                raise _HubError("invalid_json") from None
            if (not isinstance(envelope, dict) or envelope.get("success") is not True
                    or not isinstance(envelope.get("data"), dict)):
                raise _HubError("invalid_envelope")
            return envelope["data"]

    async def _discover(self, session):
        manifest = await self._request(session, "GET", "/manifest")
        endpoints = manifest.get("endpoints")
        if (manifest.get("version") != "v1" or not isinstance(endpoints, list)
                or not any(isinstance(item, dict) and item.get("method") == "POST"
                           and item.get("path") == self.BASE + "/context"
                           for item in endpoints)):
            raise _HubError("unsupported_contract")
        status = await self._request(session, "GET", "/status")
        capabilities = status.get("capabilities")
        if status.get("healthy") is not True:
            raise _HubError("unhealthy")
        if not isinstance(capabilities, dict):
            raise _HubError("invalid_capabilities")
        result = {name: capabilities.get(name) is True
                  for name in ("social_context", "jargon", "database")}
        if not any(result.values()):
            raise _HubError("context_unavailable")
        return result

    async def _run(self, operation, fallback):
        if not self._url or self._closed:
            return fallback
        generation = self._generation

        async def exchange():
            headers = {"Authorization": "Bearer " + self._key} if self._key else {}
            async with aiohttp.ClientSession(headers=headers, trust_env=False) as session:
                return await operation(session)

        async def perform():
            try:
                result, capabilities = await asyncio.wait_for(exchange(), timeout=self.timeout)
                if generation != self._generation or self._closed:
                    return fallback
                self._capabilities = capabilities
                self._status, self._detail = "available", "ready"
                return result
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError, aiohttp.ClientError, OSError, _HubError, ValueError) as exc:
                if generation == self._generation and not self._closed:
                    self._status = "degraded"
                    self._detail = (str(exc) if isinstance(exc, _HubError) else
                                    "timeout" if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                                    else "transport_error")
                return fallback

        task = asyncio.create_task(perform())
        self._tasks.add(task)
        try:
            return await task
        finally:
            self._tasks.discard(task)

    async def discover(self) -> bool:
        async def operation(session):
            capabilities = await self._discover(session)
            return True, capabilities
        return await self._run(operation, False)

    async def context(self, *, group_id: str, user_id: str, query: str) -> dict[str, str]:
        if (not isinstance(group_id, str) or not group_id.strip() or len(group_id) > 256
                or not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 256
                or not isinstance(query, str)):
            return {}

        async def operation(session):
            capabilities = self._capabilities
            if self._status != "available":
                capabilities = await self._discover(session)
            data = await self._request(session, "POST", "/context", {
                "group_id": group_id, "user_id": user_id, "query": query[:8192],
                "include": {"social": True, "jargon": True, "few_shots": True, "v2": False},
                "top_k": 4,
            })
            # The Hub contract requires the response to echo the scope it was
            # asked about. Without that echo, another conversation's background
            # data is indistinguishable from this one's, so a **missing** echo is
            # treated exactly like a mismatch rather than trusted. Values are
            # compared as text so a Hub that echoes a numeric id still matches.
            for key, expected in (("group_id", group_id), ("user_id", user_id)):
                if str(data.get(key) or "") != expected:
                    raise _HubError("scope_mismatch")
            result = {}
            parts = data.get("parts", [])
            if isinstance(parts, list):
                for part in parts[:16]:
                    if not isinstance(part, dict):
                        continue
                    kind, content = part.get("type"), part.get("content")
                    if (isinstance(kind, str) and kind in {"social", "jargon", "few_shots"}
                            and isinstance(content, str)):
                        result[kind] = content[:4096]
            shots = data.get("few_shots")
            if "few_shots" not in result and isinstance(shots, list):
                result["few_shots"] = "\n\n".join(s[:1024] for s in shots[:4] if isinstance(s, str))[:4096]
            return result, capabilities
        return await self._run(operation, {})

    async def close(self) -> None:
        self._closed = True
        self._generation += 1
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.difference_update(tasks)
        self._capabilities = {}
        self._status, self._detail = "missing", "closed"
