"""Async HTTP client for the Juno API via Uptycs middleware."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from .auth import ApiKey, auth_headers

logger = logging.getLogger("juno_mcp.client")


def _extract_cursor(data: dict) -> str:
    """Extract nextCursor from HATEOAS links in API response."""
    for link in data.get("links", []):
        if link.get("rel") == "next":
            href = link.get("href", "")
            for part in href.split("&"):
                if part.startswith("cursor="):
                    return part[len("cursor="):]
    return ""


class JunoApiError(Exception):
    """Raised when the Juno API returns an error or non-JSON response."""


def _raise_for_status(resp: httpx.Response) -> None:
    """Raise ``JunoApiError`` for non-2xx responses, including the body."""
    if resp.is_success:
        return
    body = resp.text[:300]
    raise JunoApiError(
        f"Juno API error {resp.status_code} "
        f"{resp.reason_phrase} — {body}"
    )


def _parse_json(resp: httpx.Response) -> Any:
    """Parse JSON from *resp*, raising ``JunoApiError`` on failure."""
    try:
        return resp.json()
    except (ValueError, UnicodeDecodeError) as exc:
        body = resp.text[:200]
        raise JunoApiError(
            f"API returned non-JSON response "
            f"(status {resp.status_code}): {body}"
        ) from exc


class JunoClient:
    """Async client for Juno API endpoints.

    Uses a persistent ``httpx.AsyncClient`` for connection reuse
    (keep-alive / HTTP/2) and injects a fresh JWT on every request.
    """

    def __init__(self, api_key: ApiKey) -> None:
        self._key = api_key
        self._base = api_key.api_base
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            event_hooks={
                "request": [
                    self._inject_auth,
                    self._log_request,
                ],
                "response": [self._log_response],
            },
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _inject_auth(
        self, request: httpx.Request,
    ) -> None:
        request.headers.update(auth_headers(self._key))

    @staticmethod
    async def _log_request(request: httpx.Request) -> None:
        logger.debug(
            "HTTP %s %s", request.method, request.url.copy_with(query=None),
        )

    @staticmethod
    async def _log_response(
        response: httpx.Response,
    ) -> None:
        req = response.request
        logger.debug(
            "HTTP %s %s -> %s",
            req.method, req.url.copy_with(query=None), response.status_code,
        )

    async def list_investigations(
        self,
        *,
        search: str | None = None,
        limit: int = 5,
        cursor: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if search:
            params["searchTerms"] = search
        if cursor:
            params["cursor"] = cursor
        if project_id:
            params["projectId"] = project_id

        resp = await self._http.get(
            f"{self._base}/investigations",
            params=params,
        )
        _raise_for_status(resp)
        data = _parse_json(resp)
        if isinstance(data, list):
            return {"items": data, "nextCursor": ""}
        return {
            "items": data.get("items", []),
            "nextCursor": _extract_cursor(data),
        }

    async def get_investigation(
        self, investigation_id: str,
    ) -> dict[str, Any]:
        resp = await self._http.get(
            f"{self._base}/investigations"
            f"/{investigation_id}",
        )
        _raise_for_status(resp)
        return _parse_json(resp)

    async def create_investigation(
        self,
        question: str,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"question": question}
        if project_id:
            body["projectId"] = project_id

        resp = await self._http.post(
            f"{self._base}/investigations",
            json=body,
        )
        _raise_for_status(resp)
        return _parse_json(resp)

    async def delete_investigation(
        self, investigation_id: str,
    ) -> None:
        resp = await self._http.delete(
            f"{self._base}/investigations"
            f"/{investigation_id}",
        )
        _raise_for_status(resp)

    def _run_url(
        self, investigation_id: str, run_id: str,
    ) -> str:
        return (
            f"{self._base}/investigations"
            f"/{investigation_id}/runs/{run_id}"
        )

    async def get_run(
        self, investigation_id: str, run_id: str,
    ) -> dict[str, Any]:
        resp = await self._http.get(
            self._run_url(investigation_id, run_id),
        )
        _raise_for_status(resp)
        return _parse_json(resp)

    async def stream_run_events(
        self,
        investigation_id: str,
        run_id: str,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Stream SSE events for a run.

        Yields ``(event_type, data)`` tuples.
        Event types: ``connected``, ``update``, ``done``, ``error``.

        Uses a **separate** ``httpx.AsyncClient`` so that cancellation
        (e.g. from ``asyncio.wait_for``) does not corrupt the connection
        pool used by regular API calls like ``get_run``.
        """
        url = f"{self._run_url(investigation_id, run_id)}/events"
        logger.info("SSE connecting: %s", url)
        sse_http = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            event_hooks={
                "request": [self._inject_auth, self._log_request],
                "response": [self._log_response],
            },
        )
        try:
            req = sse_http.build_request("GET", url)
            resp = await sse_http.send(req, stream=True)
            try:
                _raise_for_status(resp)
                logger.info(
                    "SSE connected: %s (status %s, content-type=%s)",
                    url, resp.status_code,
                    resp.headers.get("content-type", "?"),
                )
                event_type = "message"
                data_lines: list[str] = []
                buf = ""
                async for chunk in resp.aiter_text():
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.rstrip("\r")
                        if line.startswith("event:"):
                            event_type = line[len("event:"):].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[len("data:"):].strip())
                        elif line == "":
                            if data_lines:
                                raw = "\n".join(data_lines)
                                try:
                                    payload = json.loads(raw)
                                except json.JSONDecodeError:
                                    payload = {"raw": raw}
                                logger.info("SSE event: %s", event_type)
                                yield event_type, payload
                            event_type = "message"
                            data_lines = []
            finally:
                await resp.aclose()
        finally:
            await sse_http.aclose()

    async def create_follow_up(
        self,
        investigation_id: str,
        parent_run_id: str,
        question: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "question": question,
            "parentRunId": parent_run_id,
        }
        resp = await self._http.post(
            f"{self._base}/investigations"
            f"/{investigation_id}/runs",
            json=body,
        )
        _raise_for_status(resp)
        return _parse_json(resp)

    async def publish_run(
        self, investigation_id: str, run_id: str,
    ) -> dict[str, Any]:
        url = self._run_url(investigation_id, run_id)
        resp = await self._http.put(f"{url}/publish")
        _raise_for_status(resp)
        return _parse_json(resp)

    async def unpublish_run(
        self, investigation_id: str, run_id: str,
    ) -> dict[str, Any]:
        url = self._run_url(investigation_id, run_id)
        resp = await self._http.put(f"{url}/unpublish")
        _raise_for_status(resp)
        return _parse_json(resp)

    async def list_published_runs(
        self,
        *,
        search: str | None = None,
        limit: int = 5,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if search:
            params["searchTerms"] = search
        if cursor:
            params["cursor"] = cursor

        resp = await self._http.get(
            f"{self._base}/runs/published",
            params=params,
        )
        _raise_for_status(resp)
        data = _parse_json(resp)
        if isinstance(data, list):
            return {"items": data, "nextCursor": ""}
        return {
            "items": data.get("items", []),
            "nextCursor": _extract_cursor(data),
        }

    async def list_projects(
        self,
        *,
        limit: int = 5,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor

        resp = await self._http.get(
            f"{self._base}/projects", params=params,
        )
        _raise_for_status(resp)
        data = _parse_json(resp)
        if isinstance(data, list):
            return {"items": data, "nextCursor": ""}
        return {
            "items": data.get("items", []),
            "nextCursor": _extract_cursor(data),
        }

    async def create_project(
        self, name: str, description: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        resp = await self._http.post(
            f"{self._base}/projects", json=body,
        )
        _raise_for_status(resp)
        return _parse_json(resp)

    async def delete_project(
        self, project_id: str,
    ) -> None:
        resp = await self._http.delete(
            f"{self._base}/projects/{project_id}",
        )
        _raise_for_status(resp)
