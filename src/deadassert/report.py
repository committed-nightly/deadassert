"""Output. One block per finding, in the order you would fix them."""

from __future__ import annotations

import json
import textwrap

from .analyse import Result

HEADLINE = {
    "unwitnessed": "passes whether or not the pattern is ever produced",
    "prefix": "matches a whole sentence as a substring",
    "swallowed": "cannot fail the step",
    "masked": "cannot fail the step",
    "decorative": "cannot fail the step",
}


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _wrap(text: str, indent: str) -> str:
    return textwrap.fill(text, width=92, initial_indent=indent, subsequent_indent=indent)


def _denominator(result: Result) -> str:
    """What the verdict was measured over.

    logbook#59 and logbook#57: a verdict with no denominator cannot be told
    apart from a verdict over nothing, and this tool is itself about
    assertions that look like they are checking something and are not.
    """
    return (
        f"{_plural(len(result.assertions), 'assertion')} in "
        f"{_plural(result.scripts, 'script')} across "
        f"{_plural(len(result.files), 'workflow file')}, "
        f"{result.negated} of them negated."
    )


def nothing_to_check(result: Result) -> str:
    """Said instead of an all-clear when there were no assertions at all."""
    return (
        f"No assertions here. Read {_plural(result.scripts, 'run: script')} in "
        f"{_plural(len(result.files), 'workflow file')} and found no command whose "
        "exit status decides a step. Nothing to check."
    )


def error_lines(result: Result) -> list[str]:
    """Diagnostics, for stderr. Never part of any mode's stdout."""
    lines = []
    for path, why in result.unreadable:
        lines.append(f"deadassert: {path}: {why}")
    for where, shell_name in result.foreign:
        lines.append(f"deadassert: {where}: skipped, shell is `{shell_name}`")
    return lines


def text(result: Result, quiet: bool = False) -> str:
    if result.nothing_to_check:
        return nothing_to_check(result)

    out: list[str] = []
    if not result.findings:
        out.append("Every assertion here fails when the thing it names stops being true.")
        out.append(_denominator(result))
        if result.absent:
            out.append("")
            out.extend(_absent_block(result))
        if result.undecided:
            out.append("")
            out.extend(_undecided_block(result))
        return "\n".join(out)

    if quiet:
        for finding in result.findings:
            pattern = finding.assertion.pattern or "?"
            out.append(f"{finding.file}:{finding.line}: {finding.kind}: {pattern!r}")
        return "\n".join(out)

    for finding in result.findings:
        pattern = finding.assertion.pattern or "?"
        out.append(f"{finding.where}  {finding.kind}  {pattern!r}")
        out.append(_wrap(f"{finding.assertion.program}: {HEADLINE[finding.kind]}", "    "))
        out.append(_wrap(finding.detail, "    "))
        out.append(f"      {finding.assertion.text}")
        out.append("")

    kinds = {}
    for finding in result.findings:
        kinds[finding.kind] = kinds.get(finding.kind, 0) + 1
    breakdown = ", ".join(f"{count} {kind}" for kind, count in kinds.items())
    out.append(
        f"{_plural(len(result.findings), 'assertion')} here "
        f"{'does' if len(result.findings) == 1 else 'do'} not check what "
        f"{'it reads' if len(result.findings) == 1 else 'they read'} as checking "
        f"({breakdown})."
    )
    out.append(_denominator(result))

    if result.absent:
        out.append("")
        out.extend(_absent_block(result))
    if result.undecided:
        out.append("")
        out.extend(_undecided_block(result))
    return "\n".join(out)


def _absent_block(result: Result) -> list[str]:
    """Positive assertions whose pattern is nowhere in the `--against` file.

    Not a finding: a positive assertion that cannot match is already failing
    CI, so it is either about a different step's output or the wrong file was
    given. Worth printing either way, because it says the comparison is not
    measuring what the reader thinks.
    """
    lines = [f"not matched in {result.against}:"]
    for where, pattern in result.absent:
        lines.append(f"  {where}  {pattern!r}")
    return lines


def _undecided_block(result: Result) -> list[str]:
    lines = ["not decided:"]
    for where, why in result.undecided:
        lines.append(f"  {where}  {why}")
    return lines


def as_json(result: Result) -> str:
    payload = {
        "files": result.files,
        "scripts": result.scripts,
        "assertions": len(result.assertions),
        "negated": result.negated,
        "findings": [
            {
                "kind": finding.kind,
                "file": finding.file,
                "line": finding.line,
                "job": finding.script.job,
                "step": finding.script.label,
                "program": finding.assertion.program,
                "pattern": finding.assertion.pattern,
                "fixed": finding.assertion.fixed,
                "anchored": finding.assertion.anchored,
                "polarity": finding.assertion.polarity,
                "detail": finding.detail,
                "command": finding.assertion.text,
            }
            for finding in result.findings
        ],
        "undecided": [{"where": w, "why": y} for w, y in result.undecided],
        "foreign": [{"where": w, "shell": s} for w, s in result.foreign],
        "unreadable": [{"path": p, "why": y} for p, y in result.unreadable],
        "not_matched": [{"where": w, "pattern": p} for w, p in result.absent],
    }
    return json.dumps(payload, indent=2)
