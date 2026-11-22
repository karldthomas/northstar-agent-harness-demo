"""Typed, read-only simulator tools for the first Harness vertical slice."""

from __future__ import annotations

from typing import Any, Callable

from .envelopes import ToolError, ToolObservation
from .models import Device, EnvironmentState, Incident, Service
from .store import (
    EntityNotFoundError,
    RunNotFoundError,
    ScenarioRun,
    SimulatorError,
    SimulatorStore,
    TransientSimulatorError,
)


def _tool_error(exc: Exception) -> ToolError:
    if isinstance(exc, SimulatorError):
        return ToolError(
            code=exc.code,
            message=str(exc),
            retryable=isinstance(exc, (RunNotFoundError, TransientSimulatorError)),
        )
    return ToolError(code="TOOL_EXECUTION_FAILED", message=str(exc), retryable=False)


class ReadOnlyToolset:
    """Expose observations without changing authoritative domain state."""

    def __init__(self, store: SimulatorStore) -> None:
        self.store = store

    def _run(
        self,
        tool_name: str,
        run_id: str,
        reader: Callable[[ScenarioRun], Any],
        *,
        request_id: str | None = None,
    ) -> ToolObservation[Any]:
        try:
            run = self.store.get_run(run_id)
            if tool_name in run.faults.unavailable_tools or "*" in run.faults.unavailable_tools:
                raise TransientSimulatorError(f"synthetic API unavailable for tool {tool_name}")
            observation = self.store.read(run_id, reader)
            revision = run.state.revision
            result = ToolObservation.success(
                tool_name=tool_name,
                scenario_run_id=run_id,
                data=observation,
                state_revision=revision,
                request_id=request_id,
            )
        except Exception as exc:  # Convert domain errors to a stable tool contract.
            revision = 0
            try:
                revision = self.store.get_run(run_id).state.revision
            except Exception:
                pass
            result = ToolObservation.failure(
                tool_name=tool_name,
                scenario_run_id=run_id,
                error=_tool_error(exc),
                state_revision=revision,
                request_id=request_id,
            )
        try:
            self.store.record_observation(run_id, result.to_dict())
        except RunNotFoundError:
            # A malformed run ID should still produce the stable tool envelope;
            # there is no run-local observation stream to append to.
            pass
        return result

    @staticmethod
    def _service(state: EnvironmentState, service_id: str) -> Service:
        try:
            return state.services[service_id]
        except KeyError as exc:
            raise EntityNotFoundError(f"service not found: {service_id}") from exc

    @staticmethod
    def _device(state: EnvironmentState, device_id: str) -> Device:
        try:
            return state.devices[device_id]
        except KeyError as exc:
            raise EntityNotFoundError(f"device not found: {device_id}") from exc

    @staticmethod
    def _incident(state: EnvironmentState, incident_id: str) -> Incident:
        try:
            return state.incidents[incident_id]
        except KeyError as exc:
            raise EntityNotFoundError(f"incident not found: {incident_id}") from exc

    def check_service_health(
        self,
        run_id: str,
        service_id: str = "svc-vpn-gateway",
        *,
        request_id: str | None = None,
    ) -> ToolObservation[dict[str, Any]]:
        def read(run: ScenarioRun) -> dict[str, Any]:
            service = self._service(run.state, service_id)
            status = "unavailable" if service_id in run.faults.unavailable_services else service.status
            evidence_conflict = service_id in run.faults.service_health_conflicts
            dependencies = [
                {
                    "service_id": dependency_id,
                    "status": run.state.services[dependency_id].status,
                }
                for dependency_id in service.dependency_ids
                if dependency_id in run.state.services
            ]
            return {
                "service_id": service.service_id,
                "name": service.name,
                "status": status,
                "observed_status": "operational" if evidence_conflict else status,
                "version": service.version,
                "criticality": service.criticality,
                "dependencies": dependencies,
                "fault_injected": service_id in run.faults.unavailable_services,
                "evidence_conflict": evidence_conflict,
            }

        return self._run("check_service_health", run_id, read, request_id=request_id)

    def get_device_diagnostics(
        self,
        run_id: str,
        device_id: str,
        *,
        request_id: str | None = None,
    ) -> ToolObservation[dict[str, Any]]:
        def read(run: ScenarioRun) -> dict[str, Any]:
            device = self._device(run.state, device_id)
            employee = run.state.employees.get(device.employee_id or "")
            conflicts = device_id in run.faults.diagnostic_conflicts
            return {
                "device_id": device.device_id,
                "hostname": device.hostname,
                "employee": {
                    "employee_id": employee.employee_id,
                    "display_name": employee.display_name,
                    "department": employee.department,
                    "office": employee.office,
                }
                if employee
                else None,
                "office": device.office,
                "operating_system": device.operating_system,
                "status": device.status,
                "vpn_status": device.vpn_status,
                "installed_software": dict(device.installed_software),
                "service_ids": list(device.service_ids),
                "open_incident_ids": [
                    incident.incident_id
                    for incident in run.state.incidents.values()
                    if incident.status == "open" and device_id in incident.affected_device_ids
                ],
                "diagnostic_conflict": conflicts,
                "observed_status": "healthy" if conflicts else device.status,
            }

        return self._run("get_device_diagnostics", run_id, read, request_id=request_id)

    def list_recent_deployments(
        self,
        run_id: str,
        *,
        application_id: str | None = None,
        limit: int = 10,
        request_id: str | None = None,
    ) -> ToolObservation[dict[str, Any]]:
        def read(run: ScenarioRun) -> dict[str, Any]:
            if limit < 1 or limit > 50:
                raise ValueError("limit must be between 1 and 50")
            deployments = [
                deployment
                for deployment in run.state.deployments.values()
                if application_id is None or deployment.application_id == application_id
            ]
            deployments.sort(key=lambda deployment: deployment.deployed_at, reverse=True)
            return {
                "application_id": application_id,
                "deployments": [
                    {
                        "deployment_id": deployment.deployment_id,
                        "application_id": deployment.application_id,
                        "version": deployment.version,
                        "status": deployment.status,
                        "deployed_at": deployment.deployed_at,
                        "target_device_count": len(deployment.target_device_ids),
                        "related_incident_id": deployment.related_incident_id,
                    }
                    for deployment in deployments[:limit]
                ],
            }

        return self._run("list_recent_deployments", run_id, read, request_id=request_id)

    def find_affected_devices(
        self,
        run_id: str,
        incident_id: str,
        *,
        limit: int = 100,
        request_id: str | None = None,
    ) -> ToolObservation[dict[str, Any]]:
        def read(run: ScenarioRun) -> dict[str, Any]:
            incident = self._incident(run.state, incident_id)
            if limit < 1 or limit > 150:
                raise ValueError("limit must be between 1 and 150")
            devices = [
                run.state.devices[device_id]
                for device_id in incident.affected_device_ids
                if device_id in run.state.devices
            ]
            return {
                "incident_id": incident_id,
                "total_affected": len(devices),
                "devices": [
                    {
                        "device_id": device.device_id,
                        "employee_id": device.employee_id,
                        "office": device.office,
                        "status": device.status,
                        "vpn_status": device.vpn_status,
                    }
                    for device in devices[:limit]
                ],
            }

        return self._run("find_affected_devices", run_id, read, request_id=request_id)

    def get_incident(
        self,
        run_id: str,
        incident_id: str,
        *,
        request_id: str | None = None,
    ) -> ToolObservation[dict[str, Any]]:
        def read(run: ScenarioRun) -> dict[str, Any]:
            incident = self._incident(run.state, incident_id)
            return {
                "incident_id": incident.incident_id,
                "title": incident.title,
                "description": incident.description,
                "status": incident.status,
                "severity": incident.severity,
                "service_id": incident.service_id,
                "affected_device_count": len(incident.affected_device_ids),
                "related_deployment_id": incident.related_deployment_id,
            }

        return self._run("get_incident", run_id, read, request_id=request_id)

    def search_knowledge(
        self,
        run_id: str,
        query: str,
        *,
        limit: int = 5,
        request_id: str | None = None,
    ) -> ToolObservation[dict[str, Any]]:
        def read(run: ScenarioRun) -> dict[str, Any]:
            terms = {term for term in query.lower().split() if term}
            if not terms:
                raise ValueError("query must contain at least one term")
            if limit < 1 or limit > 10:
                raise ValueError("limit must be between 1 and 10")
            matches: list[tuple[int, Any]] = []
            for article in run.state.knowledge_articles.values():
                haystack = " ".join([article.title, article.summary, *article.tags]).lower()
                score = sum(term in haystack for term in terms)
                if score:
                    matches.append((score, article))
            matches.sort(key=lambda item: (-item[0], item[1].article_id))
            return {
                "knowledge_schema_version": "northstar.knowledge.v1",
                "query": query,
                "articles": [
                    {
                        "article_id": article.article_id,
                        "title": article.title,
                        "summary": article.summary,
                        "tags": list(article.tags),
                        "match_score": score,
                    }
                    for score, article in matches[:limit]
                ],
            }

        return self._run("search_knowledge", run_id, read, request_id=request_id)
