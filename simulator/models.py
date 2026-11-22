"""Authoritative, serializable models for the synthetic Northstar enterprise."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any


def to_jsonable(value: Any) -> Any:
    """Convert model values into JSON-safe primitives without losing structure."""

    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return value


@dataclass(slots=True)
class Employee:
    employee_id: str
    display_name: str
    department: str
    office: str
    device_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Device:
    device_id: str
    employee_id: str | None
    hostname: str
    office: str
    operating_system: str
    status: str
    vpn_status: str
    installed_software: dict[str, str]
    service_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Application:
    application_id: str
    name: str
    business_owner: str
    criticality: str


@dataclass(slots=True)
class Service:
    service_id: str
    name: str
    status: str
    version: str
    criticality: str
    dependency_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SoftwareDeployment:
    deployment_id: str
    application_id: str
    version: str
    status: str
    deployed_at: str
    target_device_ids: list[str]
    related_incident_id: str | None = None


@dataclass(slots=True)
class Incident:
    incident_id: str
    title: str
    description: str
    status: str
    severity: str
    service_id: str | None
    affected_device_ids: list[str]
    related_deployment_id: str | None = None


@dataclass(slots=True)
class AccessRequest:
    request_id: str
    employee_id: str
    application_id: str
    status: str
    justification: str
    requires_approval: bool


@dataclass(slots=True)
class KnowledgeArticle:
    article_id: str
    title: str
    summary: str
    tags: list[str]


@dataclass(slots=True)
class EnvironmentState:
    """The authoritative state for one isolated scenario run."""

    scenario_run_id: str
    scenario_id: str
    seed: int
    revision: int
    employees: dict[str, Employee]
    devices: dict[str, Device]
    applications: dict[str, Application]
    services: dict[str, Service]
    deployments: dict[str, SoftwareDeployment]
    incidents: dict[str, Incident]
    access_requests: dict[str, AccessRequest]
    knowledge_articles: dict[str, KnowledgeArticle]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    scenario_id: str
    name: str
    description: str
    objective: str
    profile: str
    read_only_tools: tuple[str, ...]
    restricted_actions: tuple[str, ...]
    fixture_profile: str | None = None
