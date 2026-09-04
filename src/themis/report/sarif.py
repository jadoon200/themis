"""SARIF output, so findings land on the diff instead of in a log.

A reviewer reading a build log has to hold a finding in their head and go looking for
the line. GitHub, Bitbucket's code-insights API and every IDE that speaks SARIF put it
on the line itself, which is where the decision is actually made.

Two things here are deliberate and both concern honesty about severity:

SARIF's `level` has four values and none of them is "critical". Mapping critical and
high both to `error` would erase the distinction the whole severity model exists to
draw, so the finding's own severity is carried in `properties` and in the message, and
`level` is left to mean what SARIF says it means — whether this should fail a build.

Rules are declared with their full text in `rules`, so a viewer can show what the check
is and why it exists rather than only that it fired. A finding whose rule cannot be
explained in the place it appears is a finding people learn to dismiss.
"""

from __future__ import annotations

import json
from typing import Any

from themis.models import Finding, Severity

SCHEMA = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/schemas/sarif-schema-2.1.0.json"
)
VERSION = "2.1.0"

# SARIF has error / warning / note / none. `critical` has no home there, so it maps to
# error alongside high and keeps its real value in properties — losing it silently
# would make a report that distinguishes them look like one that does not.
_LEVEL: dict[Severity, str] = {
    Severity.CRITICAL: "error",
    Severity.HIGH: "error",
    Severity.MEDIUM: "warning",
    Severity.LOW: "note",
    Severity.INFO: "note",
}


def _rule_descriptor(finding: Finding) -> dict[str, Any]:
    return {
        "id": finding.rule_id,
        "name": finding.rule_id,
        "shortDescription": {"text": finding.title},
        "fullDescription": {"text": finding.consequence},
        "help": {"text": finding.suggestion or finding.consequence},
        "properties": {"family": finding.family},
        "defaultConfiguration": {"level": _LEVEL.get(finding.severity, "warning")},
    }


def _result(finding: Finding) -> dict[str, Any]:
    evidence = finding.evidence
    message = f"{finding.title}. {finding.consequence}"
    if finding.suggestion:
        message += f" Suggested: {finding.suggestion}"

    result: dict[str, Any] = {
        "ruleId": finding.rule_id,
        "level": _LEVEL.get(finding.severity, "warning"),
        "message": {"text": message},
        "properties": {
            "severity": finding.severity.value,
            "confidence": finding.confidence.value,
            "family": finding.family,
            "model": evidence.model_name,
            "blastRadius": list(finding.blast_radius),
        },
    }
    if finding.suppressed_reason:
        # SARIF models this natively, so a suppressed finding travels as suppressed
        # rather than being dropped — the viewer decides whether to show it.
        result["suppressions"] = [{"kind": "external", "justification": finding.suppressed_reason}]

    if evidence.file_path:
        location: dict[str, Any] = {
            "physicalLocation": {
                "artifactLocation": {"uri": evidence.file_path},
                # SARIF requires a region with a positive line number. Most findings
                # are about a model rather than a line, so line 1 stands for the file
                # and the real location is the model named in the message.
                "region": {"startLine": evidence.line or 1},
            }
        }
        result["locations"] = [location]
    return result


def render(findings: list[Finding], *, tool_version: str = "0.1.0") -> str:
    """A SARIF 2.1.0 log for one review."""
    seen: dict[str, dict[str, Any]] = {}
    for finding in findings:
        seen.setdefault(finding.rule_id, _rule_descriptor(finding))

    log: dict[str, Any] = {
        "$schema": SCHEMA,
        "version": VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "THEMIS",
                        "informationUri": "https://github.com/jadoon200/themis",
                        "version": tool_version,
                        "rules": [seen[key] for key in sorted(seen)],
                    }
                },
                "results": [_result(finding) for finding in findings],
            }
        ],
    }
    return json.dumps(log, indent=2)
