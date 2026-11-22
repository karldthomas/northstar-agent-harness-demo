#!/usr/bin/env python3
"""Load and validate Northstar's deployment manifest.

The manifest is intentionally small and declarative.  It owns stack ordering,
dependencies, naming, and lifecycle-step placement.  A stack's JSON parameter
file is derived from its template path, so the two cannot drift apart in the
deployment metadata.

PyYAML is used when it is available, but it is not a deployment-host
requirement.  The checked-in manifest uses a deliberately narrow YAML shape and
has a small standard-library fallback parser for environments that only install
the documented boto3 dependency.
"""

from __future__ import annotations

import ast
import heapq
import re
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any, Iterable


DEFAULT_MANIFEST_PATH = Path(__file__).with_name("deployment.yaml")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_ALLOWED_NAME_FIELDS = {"name", "environment", "id"}


@dataclass(frozen=True)
class StackDefinition:
    """One independent CloudFormation/SAM stack in the deployment graph."""

    stack_id: str
    template_path: Path
    config_path: Path
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class LifecycleStep:
    """A named, allow-listed action positioned around stack deployment."""

    step_id: str
    handler: str
    after: tuple[str, ...]
    before: tuple[str, ...]


@dataclass(frozen=True)
class DeploymentManifest:
    """Validated deployment metadata and graph operations."""

    path: Path
    version: int
    name: str
    stack_name_pattern: str
    stacks: tuple[StackDefinition, ...]
    steps: tuple[LifecycleStep, ...]

    @property
    def stack_ids(self) -> tuple[str, ...]:
        """Return a stable topological order, preserving manifest tie order."""

        positions = {stack.stack_id: index for index, stack in enumerate(self.stacks)}
        dependencies = {stack.stack_id: set(stack.depends_on) for stack in self.stacks}
        ready = [(positions[stack_id], stack_id) for stack_id, deps in dependencies.items() if not deps]
        heapq.heapify(ready)
        order: list[str] = []
        dependents: dict[str, list[str]] = {stack.stack_id: [] for stack in self.stacks}
        for stack in self.stacks:
            for dependency in stack.depends_on:
                dependents[dependency].append(stack.stack_id)

        while ready:
            _, stack_id = heapq.heappop(ready)
            order.append(stack_id)
            for dependent in dependents[stack_id]:
                dependencies[dependent].remove(stack_id)
                if not dependencies[dependent]:
                    heapq.heappush(ready, (positions[dependent], dependent))

        # ``load_manifest`` validates this already.  Keep the guard here so a
        # manually constructed DeploymentManifest cannot silently return a
        # partial order.
        if len(order) != len(self.stacks):
            raise ValueError("deployment manifest contains a dependency cycle")
        return tuple(order)

    def definition(self, stack_id: str) -> StackDefinition:
        for stack in self.stacks:
            if stack.stack_id == stack_id:
                return stack
        choices = ", ".join(self.stack_ids)
        raise ValueError(f"unknown stack {stack_id}; choose from {choices}")

    def stack_name(self, environment: str, stack_id: str) -> str:
        self.definition(stack_id)
        try:
            return self.stack_name_pattern.format(name=self.name, environment=environment, id=stack_id)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"invalid stack_name_pattern in {self.path}: {self.stack_name_pattern!r}") from exc

    def predecessors(self, stack_id: str) -> tuple[str, ...]:
        """Return all transitive predecessors in deployment order."""

        self.definition(stack_id)
        needed: set[str] = set()

        def visit(current: str) -> None:
            for dependency in self.definition(current).depends_on:
                if dependency not in needed:
                    needed.add(dependency)
                    visit(dependency)

        visit(stack_id)
        return tuple(candidate for candidate in self.stack_ids if candidate in needed)

    def select_stack_ids(self, selected: str | None = None, start: str | None = None) -> tuple[str, ...]:
        """Select one stack, or a stack and every downstream dependency."""

        if selected and start:
            raise ValueError("--stack and --from cannot be used together")
        if selected:
            self.definition(selected)
            return (selected,)
        if not start:
            return self.stack_ids
        self.definition(start)
        selected_ids = {start}
        changed = True
        while changed:
            changed = False
            for stack in self.stacks:
                if stack.stack_id not in selected_ids and any(dependency in selected_ids for dependency in stack.depends_on):
                    selected_ids.add(stack.stack_id)
                    changed = True
        return tuple(stack_id for stack_id in self.stack_ids if stack_id in selected_ids)

    def steps_before(self, stack_id: str) -> tuple[LifecycleStep, ...]:
        self.definition(stack_id)
        return tuple(
            step
            for step in self.steps
            if stack_id in step.before and not step.after
        )

    def steps_after(self, stack_id: str, deployed: Iterable[str] = ()) -> tuple[LifecycleStep, ...]:
        self.definition(stack_id)
        deployed_ids = set(deployed)
        return tuple(
            step
            for step in self.steps
            if stack_id in step.after and set(step.after).issubset(deployed_ids | {stack_id})
        )


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return None
    if value == "[]":
        return []
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in inner.split(",") if part.strip()]
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return value


def _parse_minimal_yaml(text: str) -> dict[str, Any]:
    """Parse the constrained manifest shape without a third-party package."""

    result: dict[str, Any] = {}
    section: str | None = None
    current: dict[str, Any] | None = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if indent == 0:
            if ":" not in line:
                raise ValueError(f"invalid manifest line {line_number}: {raw_line!r}")
            key, value = line.split(":", 1)
            if value.strip():
                result[key.strip()] = _scalar(value)
                section = None
                current = None
            else:
                section = key.strip()
                result[section] = []
                current = None
            continue
        if indent == 2 and line.startswith("- ") and section in {"stacks", "steps"}:
            current = {}
            result[section].append(current)
            key, value = line[2:].split(":", 1)
            current[key.strip()] = _scalar(value)
            continue
        if indent == 4 and current is not None and ":" in line:
            key, value = line.split(":", 1)
            current[key.strip()] = _scalar(value)
            continue
        raise ValueError(f"unsupported manifest structure on line {line_number}: {raw_line!r}")
    return result


def _read_document(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        payload = _parse_minimal_yaml(text)
    else:
        payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ValueError(f"deployment manifest must contain a mapping: {path}")
    return payload


def _as_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"manifest field {field} must be a non-empty string")
    return value.strip()


def _as_string_list(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"manifest field {field} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _relative_path(root: Path, value: Any, *, field: str) -> Path:
    relative = Path(_as_string(value, field=field))
    if relative.is_absolute():
        raise ValueError(f"manifest field {field} must be relative to {root}")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"manifest field {field} escapes the manifest directory: {relative}") from exc
    return resolved


def _validate_name_pattern(pattern: str, path: Path) -> None:
    try:
        fields = list(Formatter().parse(pattern))
    except ValueError as exc:
        raise ValueError(f"invalid stack_name_pattern in {path}: {pattern!r}") from exc
    for _, field_name, format_spec, conversion in fields:
        if field_name not in _ALLOWED_NAME_FIELDS or format_spec or conversion:
            raise ValueError(
                f"stack_name_pattern may only use {{{', '.join(sorted(_ALLOWED_NAME_FIELDS))}}}: {pattern!r}"
            )


def _topological_order(stacks: tuple[StackDefinition, ...]) -> tuple[str, ...]:
    manifest = DeploymentManifest(
        path=Path("<memory>"),
        version=1,
        name="manifest",
        stack_name_pattern="{id}",
        stacks=stacks,
        steps=(),
    )
    return manifest.stack_ids


def load_manifest(path: Path | str = DEFAULT_MANIFEST_PATH) -> DeploymentManifest:
    """Read, validate, and return a deployment manifest."""

    manifest_path = Path(path).expanduser().resolve()
    payload = _read_document(manifest_path)
    version = payload.get("version")
    if version != 1:
        raise ValueError(f"unsupported deployment manifest version {version!r}; expected 1")
    name = _as_string(payload.get("name"), field="name")
    stack_name_pattern = _as_string(payload.get("stack_name_pattern"), field="stack_name_pattern")
    _validate_name_pattern(stack_name_pattern, manifest_path)

    raw_stacks = payload.get("stacks")
    if not isinstance(raw_stacks, list) or not raw_stacks:
        raise ValueError("manifest field stacks must be a non-empty list")
    root = manifest_path.parent
    stacks: list[StackDefinition] = []
    seen_stack_ids: set[str] = set()
    for index, raw_stack in enumerate(raw_stacks):
        if not isinstance(raw_stack, dict):
            raise ValueError(f"manifest stacks[{index}] must be a mapping")
        stack_id = _as_string(raw_stack.get("id"), field=f"stacks[{index}].id")
        if not _IDENTIFIER.fullmatch(stack_id):
            raise ValueError(f"invalid stack id {stack_id!r}")
        if stack_id in seen_stack_ids:
            raise ValueError(f"duplicate stack id {stack_id}")
        seen_stack_ids.add(stack_id)
        template_path = _relative_path(root, raw_stack.get("template"), field=f"stacks[{index}].template")
        if not template_path.exists():
            raise ValueError(f"missing stack template for {stack_id}: {template_path}")
        config_path = template_path.with_suffix(".json")
        if not config_path.exists():
            raise ValueError(f"missing derived stack configuration for {stack_id}: {config_path}")
        depends_on = _as_string_list(raw_stack.get("depends_on", []), field=f"stacks[{index}].depends_on")
        if stack_id in depends_on:
            raise ValueError(f"stack {stack_id} cannot depend on itself")
        stacks.append(StackDefinition(stack_id, template_path, config_path, depends_on))

    stack_ids = {stack.stack_id for stack in stacks}
    for stack in stacks:
        unknown = set(stack.depends_on) - stack_ids
        if unknown:
            raise ValueError(f"stack {stack.stack_id} depends on unknown stack(s): {', '.join(sorted(unknown))}")
    stack_tuple = tuple(stacks)
    _topological_order(stack_tuple)

    raw_steps = payload.get("steps", [])
    if not isinstance(raw_steps, list):
        raise ValueError("manifest field steps must be a list")
    steps: list[LifecycleStep] = []
    seen_step_ids: set[str] = set()
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            raise ValueError(f"manifest steps[{index}] must be a mapping")
        step_id = _as_string(raw_step.get("id"), field=f"steps[{index}].id")
        handler = _as_string(raw_step.get("handler"), field=f"steps[{index}].handler")
        if step_id in seen_step_ids:
            raise ValueError(f"duplicate lifecycle step id {step_id}")
        seen_step_ids.add(step_id)
        after = _as_string_list(raw_step.get("after", []), field=f"steps[{index}].after")
        before = _as_string_list(raw_step.get("before", []), field=f"steps[{index}].before")
        unknown_refs = (set(after) | set(before)) - stack_ids
        if unknown_refs:
            raise ValueError(
                f"lifecycle step {step_id} references unknown stack(s): {', '.join(sorted(unknown_refs))}"
            )
        if not after and not before:
            raise ValueError(f"lifecycle step {step_id} must specify after and/or before")
        steps.append(LifecycleStep(step_id, handler, after, before))

    manifest = DeploymentManifest(
        path=manifest_path,
        version=int(version),
        name=name,
        stack_name_pattern=stack_name_pattern,
        stacks=stack_tuple,
        steps=tuple(steps),
    )
    positions = {stack_id: index for index, stack_id in enumerate(manifest.stack_ids)}
    for step in manifest.steps:
        if step.after and step.before:
            if max(positions[stack_id] for stack_id in step.after) >= min(positions[stack_id] for stack_id in step.before):
                raise ValueError(f"lifecycle step {step.step_id} is placed across an invalid stack boundary")
    return manifest


__all__ = [
    "DEFAULT_MANIFEST_PATH",
    "DeploymentManifest",
    "LifecycleStep",
    "StackDefinition",
    "load_manifest",
]
