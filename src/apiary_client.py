"""Thin async HTTP client for Apiary REST API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Config

log = logging.getLogger(__name__)


class ApiaryClient:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._base_url = config.apiary_base_url.rstrip("/")
        self._token: str = config.apiary_api_token
        self._refresh_token: str = config.apiary_refresh_token
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=30.0,
            follow_redirects=True,
        )

    # ── Auth ──────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        masked = self._token[:8] + "..." if len(self._token) > 8 else "???"
        log.debug("Using token: %s (len=%d)", masked, len(self._token))
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }

    async def refresh_auth(self) -> bool:
        """Try to refresh the API token. Returns True on success."""
        # Try login endpoint with refresh token as secret
        for endpoint, payload in [
            ("/api/v1/agents/token/refresh", {"refresh_token": self._refresh_token}),
            ("/api/v1/agents/refresh", {"refresh_token": self._refresh_token}),
            ("/api/v1/agents/login", {
                "agent_id": self._config.apiary_agent_id,
                "refresh_token": self._refresh_token,
            }),
        ]:
            try:
                resp = await self._client.post(
                    endpoint,
                    json=payload,
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                data = resp.json()
                self._token = data.get("token", self._token)
                if "refresh_token" in data:
                    self._refresh_token = data["refresh_token"]
                log.info("Apiary token refreshed via %s", endpoint)
                return True
            except httpx.HTTPStatusError:
                continue
        log.error("All refresh attempts failed")
        return False

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        """Make a request, auto-refreshing token on 401."""
        resp = await self._client.request(
            method, path, headers=self._headers(), **kwargs
        )
        if resp.status_code == 401:
            log.warning("Apiary 401 — attempting token refresh")
            if await self.refresh_auth():
                resp = await self._client.request(
                    method, path, headers=self._headers(), **kwargs
                )
        resp.raise_for_status()
        return resp

    # ── Tasks ─────────────────────────────────────────────────────────

    async def poll_tasks(self) -> list[dict[str, Any]]:
        resp = await self._request(
            "GET",
            f"/api/v1/hives/{self._config.apiary_hive_id}/tasks/poll",
            params={
                "capabilities": ",".join(self._config.apiary_capabilities),
            }
            if self._config.apiary_capabilities
            else None,
        )
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def claim_task(self, task_id: str) -> dict[str, Any]:
        hive = self._config.apiary_hive_id
        resp = await self._request(
            "PATCH",
            f"/api/v1/hives/{hive}/tasks/{task_id}/claim",
        )
        return resp.json()

    async def complete_task(self, task_id: str, result: str) -> dict[str, Any]:
        hive = self._config.apiary_hive_id
        resp = await self._request(
            "PATCH",
            f"/api/v1/hives/{hive}/tasks/{task_id}/complete",
            json={"result": {"output": result}},
        )
        return resp.json()

    async def fail_task(self, task_id: str, error: str) -> dict[str, Any]:
        hive = self._config.apiary_hive_id
        resp = await self._request(
            "PATCH",
            f"/api/v1/hives/{hive}/tasks/{task_id}/fail",
            json={"error": {"message": error}},
        )
        return resp.json()

    async def create_task(self, task_type: str, payload: dict[str, Any] | None = None,
                          target_agent_id: str | None = None,
                          target_capability: str | None = None,
                          priority: int = 2) -> dict[str, Any]:
        hive = self._config.apiary_hive_id
        body: dict[str, Any] = {"type": task_type}
        if payload:
            body["payload"] = payload
        if target_agent_id:
            body["target_agent_id"] = target_agent_id
        if target_capability:
            body["target_capability"] = target_capability
        if priority != 2:
            body["priority"] = priority
        resp = await self._request(
            "POST",
            f"/api/v1/hives/{hive}/tasks",
            json=body,
        )
        return resp.json()

    async def create_schedule(self, name: str, trigger_type: str,
                              task_type: str, task_payload: dict[str, Any] | None = None,
                              cron_expression: str | None = None,
                              interval_seconds: int | None = None,
                              run_at: str | None = None,
                              task_target_agent_id: str | None = None,
                              overlap_policy: str = "skip") -> dict[str, Any]:
        hive = self._config.apiary_hive_id
        body: dict[str, Any] = {
            "name": name,
            "trigger_type": trigger_type,
            "task_type": task_type,
            "overlap_policy": overlap_policy,
        }
        if task_payload:
            body["task_payload"] = task_payload
        if cron_expression:
            body["cron_expression"] = cron_expression
        if interval_seconds:
            body["interval_seconds"] = interval_seconds
        if run_at:
            body["run_at"] = run_at
        if task_target_agent_id:
            body["task_target_agent_id"] = task_target_agent_id
        resp = await self._request(
            "POST",
            f"/api/v1/hives/{hive}/schedules",
            json=body,
        )
        return resp.json()

    async def list_schedules(self) -> list[dict[str, Any]]:
        hive = self._config.apiary_hive_id
        resp = await self._request("GET", f"/api/v1/hives/{hive}/schedules")
        data = resp.json()
        return data.get("data", []) if isinstance(data, dict) else data

    async def delete_schedule(self, schedule_id: str) -> None:
        hive = self._config.apiary_hive_id
        await self._request("DELETE", f"/api/v1/hives/{hive}/schedules/{schedule_id}")

    async def update_progress(self, task_id: str, progress: int) -> dict[str, Any]:
        """Report task progress (0-100). Resets progress_timeout on the server."""
        hive = self._config.apiary_hive_id
        resp = await self._request(
            "PATCH",
            f"/api/v1/hives/{hive}/tasks/{task_id}/progress",
            json={"progress": progress},
        )
        return resp.json()

    async def heartbeat(self) -> None:
        await self._request("POST", "/api/v1/agents/heartbeat")

    async def update_status(self, status: str) -> None:
        """Update agent status (online/busy/idle/offline/error)."""
        await self._request("PATCH", "/api/v1/agents/status", json={"status": status})

    # ── Persona ───────────────────────────────────────────────────────

    async def get_persona_assembled(self) -> str | None:
        """Fetch the pre-assembled persona system prompt. Returns None if unavailable."""
        try:
            resp = await self._request("GET", "/api/v1/persona/assembled")
            data = resp.json()
            persona_data = data.get("data", data) if isinstance(data, dict) else {}
            return persona_data.get("prompt") or None
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                log.debug("Persona endpoint not available (404); proceeding without it")
            else:
                log.warning("Failed to fetch persona; proceeding without it", exc_info=True)
            return None
        except Exception:
            log.warning("Failed to fetch persona; proceeding without it", exc_info=True)
            return None

    async def get_persona_version(self, known_version: int | None = None) -> dict[str, Any]:
        """Check the server-assigned persona version. Lightweight poll-friendly call."""
        try:
            params: dict[str, Any] = {}
            if known_version is not None:
                params["known_version"] = known_version
            resp = await self._request("GET", "/api/v1/persona/version", params=params or None)
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                log.debug("Persona version endpoint not available (404)")
            else:
                log.warning("Failed to check persona version", exc_info=True)
            return {}
        except Exception:
            log.warning("Failed to check persona version", exc_info=True)
            return {}

    async def update_persona_memory(
        self,
        content: str,
        message: str | None = None,
        mode: str = "append",
    ) -> dict[str, Any]:
        """Update the MEMORY document in the active persona.

        Args:
            content: The text to write.
            message: Optional changelog message.
            mode: 'append' (default), 'prepend', or 'replace'.
        """
        body: dict[str, Any] = {"content": content, "mode": mode}
        if message:
            body["message"] = message
        resp = await self._request("PATCH", "/api/v1/persona/memory", json=body)
        return resp.json()

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._client.aclose()
