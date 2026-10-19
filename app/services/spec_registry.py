import hashlib
import json
import re

from fastapi import HTTPException

REQ_RE = re.compile(r"^#{3}\s+Requirement:\s*(.+?)\s*$", re.M)
SCENARIO_RE = re.compile(r"^####\s+Scenario:\s*(.+?)\s*$", re.M)
SECTION_RE = re.compile(r"^##\s+(ADDED|MODIFIED|REMOVED)\s+Requirements\s*$", re.M)


def canonical_spec(delta: dict) -> str:
    return json.dumps(delta, sort_keys=True, ensure_ascii=False)


def spec_hash(delta: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_spec(delta).encode("utf-8")).hexdigest()


def parse_delta(md_text: str) -> dict:
    sections = {"ADDED": [], "MODIFIED": [], "REMOVED": []}
    current = None
    requirement = None
    for line in md_text.splitlines():
        m = SECTION_RE.match(line.strip())
        if m:
            current = m.group(1)
            requirement = None
            continue
        m = REQ_RE.match(line.strip())
        if m and current:
            requirement = {"name": m.group(1), "scenarios": []}
            sections[current].append(requirement)
            continue
        m = SCENARIO_RE.match(line.strip())
        if m and current and requirement is not None:
            requirement["scenarios"].append(m.group(1))
    parsed = {section: [r for r in sections[section] if r] for section in sections}
    if not any(parsed.values()) and md_text.strip():
        raise SpecValidationError("delta has no ADDED/MODIFIED/REMOVED requirement sections")
    return parsed


class SpecValidationError(HTTPException):
    def __init__(self, message: str):
        super().__init__(status_code=422, detail={"error": "SPEC_INVALID", "message": message})


SCHEMA_ARTIFACTS = {
    "proposal": {"generates": "proposal.md", "requires": []},
    "specs": {"generates": "specs/**/*.md", "requires": ["proposal"]},
    "design": {"generates": "design.md", "requires": ["specs"]},
    "security": {"generates": "security.md", "requires": ["design", "specs"]},
    "test-plan": {"generates": "test-plan.md", "requires": ["design", "security"]},
    "operations": {"generates": "operations.md", "requires": ["design"]},
    "tasks": {"generates": "tasks.md", "requires": ["design", "security", "test-plan"]},
}


def validate_artifact_order(artifacts: dict) -> None:
    if artifacts.get("tasks") and not {"design", "security", "test-plan"}.issubset(artifacts):
        raise SpecValidationError("tasks artifact published without design/security/test-plan (schema engineering-platform)")


def render_delta_md(delta: dict, title: str) -> str:
    lines = [f"# Change: {title}"]
    for section in ("ADDED", "MODIFIED", "REMOVED"):
        items = delta.get(section, [])
        if not items:
            continue
        lines.append(f"## {section} Requirements")
        for req in items:
            lines.append(f"### Requirement: {req['name']}")
            for scenario in req.get("scenarios", []):
                lines.append(f"#### Scenario: {scenario}")
    return "\n".join(lines)
