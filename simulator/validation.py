"""Integrity checks for connected synthetic enterprise fixtures."""

from __future__ import annotations

from .models import EnvironmentState


def validate_environment(state: EnvironmentState) -> list[str]:
    """Return relationship errors without mutating the supplied state."""

    errors: list[str] = []
    for employee in state.employees.values():
        for device_id in employee.device_ids:
            device = state.devices.get(device_id)
            if device is None:
                errors.append(f"{employee.employee_id} references missing device {device_id}")
            elif device.employee_id != employee.employee_id:
                errors.append(f"{device_id} points to {device.employee_id}, not {employee.employee_id}")

    for device in state.devices.values():
        if device.employee_id and device.employee_id not in state.employees:
            errors.append(f"{device.device_id} references missing employee {device.employee_id}")
        for service_id in device.service_ids:
            if service_id not in state.services:
                errors.append(f"{device.device_id} references missing service {service_id}")

    for service in state.services.values():
        for dependency_id in service.dependency_ids:
            if dependency_id not in state.services:
                errors.append(f"{service.service_id} references missing dependency {dependency_id}")

    for deployment in state.deployments.values():
        if deployment.application_id not in state.applications:
            errors.append(f"{deployment.deployment_id} references missing application {deployment.application_id}")
        for device_id in deployment.target_device_ids:
            if device_id not in state.devices:
                errors.append(f"{deployment.deployment_id} references missing device {device_id}")
        if deployment.related_incident_id and deployment.related_incident_id not in state.incidents:
            errors.append(
                f"{deployment.deployment_id} references missing incident {deployment.related_incident_id}"
            )

    for incident in state.incidents.values():
        if incident.service_id and incident.service_id not in state.services:
            errors.append(f"{incident.incident_id} references missing service {incident.service_id}")
        if incident.related_deployment_id and incident.related_deployment_id not in state.deployments:
            errors.append(
                f"{incident.incident_id} references missing deployment {incident.related_deployment_id}"
            )
        for device_id in incident.affected_device_ids:
            if device_id not in state.devices:
                errors.append(f"{incident.incident_id} references missing device {device_id}")

    for request in state.access_requests.values():
        if request.employee_id not in state.employees:
            errors.append(f"{request.request_id} references missing employee {request.employee_id}")
        if request.application_id not in state.applications:
            errors.append(f"{request.request_id} references missing application {request.application_id}")

    return errors

