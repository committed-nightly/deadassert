"""Turning assertions into findings.

Four kinds, and the first one is why this tool exists.

`unwitnessed`
    A `needs-no-match` assertion -- one that fails the step when its pattern
    *appears* -- where nothing else in the workflows establishes that the
    pattern can appear at all. `if grep -q 'merge-upstream' findings.txt;
    then exit 1; fi` is a promise that a particular finding kind stays out of
    the output. Rename that finding kind in the tool and the assertion keeps
    passing, forever, protecting nothing. Its positive twin would have gone
    red the same afternoon.

`prefix`
    A `needs-match` assertion matching a whole sentence as a *substring*.
    `grep -qF 'No rules here.' out.txt` keeps passing when the line becomes
    `No rules here. Read 5 files.` -- which is exactly the edit people make
    when they add a count to a summary line. This is the only kind that rests
    on a guess about intent, and `--against` replaces the guess with the
    output.

`swallowed`, `masked`, `decorative`
    The assertion's exit status never reaches the step's: discarded by
    `|| true`, thrown away by its position in a pipeline, or used only to
    pick which branch prints something.

The witness rule is deliberately generous, because a false positive here
costs more than a miss. A pattern counts as witnessed if any `needs-match`
assertion anywhere in the files scanned asserts that pattern, *or* asserts a
longer string containing it -- asserting `no rules here, so nothing to check`
proves `nothing to check` can appear. The scope is every file scanned rather
than the job, since the vocabulary of a tool's output is a property of the
tool and not of the job that ran it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import shell, workflows
from .shell import Assertion
from .workflows import Script, WorkflowError


KIND_ORDER = ("unwitnessed", "prefix", "swallowed", "masked", "decorative")


class CorpusError(ValueError):
    """Nowhere to read workflows from."""


@dataclass
class Finding:
    kind: str
    script: Script
    assertion: Assertion
    #: The sentence explaining this finding, built where the facts are.
    detail: str

    @property
    def file(self) -> str:
        return self.script.workflow

    @property
    def line(self) -> int:
        return self.script.line_of(self.assertion.line)

    @property
    def where(self) -> str:
        return f"{os.path.basename(self.file)}:{self.line}"


@dataclass
class Result:
    root: str
    #: Workflow files read.
    files: list[str] = field(default_factory=list)
    scripts: int = 0
    assertions: list[tuple[Script, Assertion]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    #: Assertions recognised but not decided, as `(where, why)`.
    undecided: list[tuple[str, str]] = field(default_factory=list)
    #: `run:` scripts skipped for their shell, as `(where, shell)`.
    foreign: list[tuple[str, str]] = field(default_factory=list)
    #: Files that would not parse, as `(path, why)`.
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    #: The `--against` file, when one was given.
    against: str | None = None
    #: Patterns `--against` showed matching nothing in the output.
    absent: list[tuple[str, str]] = field(default_factory=list)

    @property
    def live(self) -> list[tuple[Script, Assertion]]:
        return [(s, a) for s, a in self.assertions if a.live]

    @property
    def negated(self) -> int:
        return sum(1 for _, a in self.assertions if a.polarity == "needs-no-match")

    @property
    def nothing_to_check(self) -> bool:
        return not self.assertions


def _is_expansion(pattern: str | None) -> bool:
    """Whether a pattern's value depends on something the shell fills in.

    `$var`, `${var}`, `$(cmd)` and backticks all mean the characters in the
    workflow file are not the characters grep is given, so nothing here can
    say what the assertion matches. A literal `\\$` is not an expansion.
    """
    if pattern is None:
        return False
    return bool(re.search(r"(?<!\\)[$`]", pattern))


def _witnesses(assertions: list[tuple[Script, Assertion]]) -> list[str]:
    """Patterns positively asserted somewhere, which prove a string can appear."""
    return [a.pattern for _, a in assertions if a.polarity == "needs-match" and a.pattern]


def _witnessed(pattern: str, witnesses: list[str]) -> bool:
    """Whether any positive assertion establishes `pattern` can appear.

    Containment counts in one direction only. A positive assertion on a
    *longer* string containing this pattern proves the pattern appears. A
    positive assertion on a *shorter* one does not: `grep -qF 'checked'`
    passing says nothing about `not checked:` ever being printed.
    """
    return any(pattern in witness for witness in witnesses)


def _reads_as_sentence(pattern: str) -> bool:
    """Whether a pattern reads as a whole line of output rather than a fragment.

    Narrow on purpose, and every clause of it is here because a wider version
    fired on a substring match that was deliberate. Measured against the 160
    positive assertions in this organisation's own workflows:

    * **Ends with a full stop.** `'.dockerignore line 1 (.git)'` ends with a
      bracket and is a column of a finding line, not a line.
    * **Starts with a capital or a digit.** `'submodules. Read 3 gitlinks and
      3 .gitmodules entries.'` starts mid-word because it is the tail of
      `Found 3 declarations ... across 3 submodules. Read ...`; a pattern
      beginning lower-case has a line in front of it by construction.
    * **Four words or more.** `'Nothing to check.'` is the end of a longer
      sentence in the tool that prints it, and matching it as a substring is
      the only thing that would work.
    * **Unanchored.** A pattern with `^` or `$` in it is already being
      matched as a line, by hand.

    This is the one judgement in `deadassert` that is about what somebody
    meant rather than about what the shell does, which is why `--against`
    exists: given real output, the same question is decided by comparison.
    """
    text = pattern.strip()
    if not text.endswith("."):
        return False
    if text.startswith("^") or text.endswith("$"):
        return False
    if not (text[0].isupper() or text[0].isdigit()):
        return False
    return len(text.split()) >= 4


def _as_regex(assertion: Assertion) -> re.Pattern | None:
    """The assertion's pattern as a Python regex, or None if not translatable."""
    if assertion.pattern is None:
        return None
    if assertion.fixed:
        body = re.escape(assertion.pattern)
    else:
        # Basic and extended REs share enough with Python's for the operators
        # that turn up in CI assertions; anything else is left undecided.
        if re.search(r"\\[0-9({]|\[\[:", assertion.pattern):
            return None
        body = assertion.pattern
    if assertion.anchored:
        body = f"(?:{body})\\Z"
        return re.compile(f"\\A{body}")
    try:
        return re.compile(body)
    except re.error:
        return None


def _prefix_detail(
    assertion: Assertion, lines: list[str] | None, against: str | None
) -> str | None:
    """Why this positive assertion is weaker than it reads, or None if it isn't.

    Without `--against`, the sentence heuristic, which is about intent.

    With `--against`, the pattern is compared to real output lines, and the
    case worth finding is the one that first version of this function got
    backwards. Three things a fixed, unanchored pattern `P` can be:

    * **A whole line.** `P` equals some line exactly. The assertion behaves
      like a whole-line match today and will silently stop being one the
      first time anything is appended. Reported whatever `P` looks like,
      because here the output has settled the question that
      `_reads_as_sentence` only guesses at.
    * **The opening of a longer line.** Some line *starts with* `P` and has
      more after it. The append has **already happened** and the assertion
      did not notice -- it is passing over a line it does not name in full.
      This is the stronger finding of the two, and the heuristic cannot see
      it at all. Still gated on the pattern reading as a sentence, because a
      short leading fragment like `'examples/a.txt:3:'` is a prefix match
      somebody meant.
    * **Inside a line.** `P` occurs, but never at the start. A deliberate
      substring match, and no finding. This is what `--against` is for:
      it takes findings away.
    """
    pattern = assertion.pattern
    if pattern is None or assertion.anchored or not assertion.fixed:
        return None

    if lines is None:
        if not _reads_as_sentence(pattern):
            return None
        return (
            "the pattern reads as a whole line of output, but -F without -x "
            "matches it as a substring, so the assertion survives anything "
            "appended to that line"
        )

    wanted = pattern.strip()
    where = os.path.basename(against or "the output")
    if any(line.strip() == wanted for line in lines):
        return (
            f"the pattern is a whole line of {where}, but -F without -x matches it "
            "as a substring, so the assertion survives anything appended to that line"
        )
    grown = [
        line.strip() for line in lines if line.strip().startswith(wanted) and line.strip() != wanted
    ]
    if grown and _reads_as_sentence(pattern):
        return (
            f"in {where} that line reads {grown[0]!r} -- the pattern is its opening, "
            "so this assertion is already passing over a line it does not name in "
            "full, which is the edit it was written to catch"
        )
    return None


def analyse(
    root: str,
    paths: list[str] | None = None,
    against: str | None = None,
) -> Result:
    """Read the workflows under `root` and report the assertions that cannot fail."""
    if paths:
        relative = []
        for path in paths:
            if not os.path.isfile(path):
                raise CorpusError(f"{path}: not a file")
            relative.append(os.path.relpath(path, root) if root != "." else path)
    else:
        relative = workflows.find_workflows(root)
        if not relative:
            raise CorpusError(
                f"{os.path.join(root, workflows.WORKFLOW_DIR)}: no workflow files here"
            )

    result = Result(root=root, against=against)

    output_lines: list[str] | None = None
    if against is not None:
        with open(against, encoding="utf-8") as handle:
            output_lines = handle.read().split("\n")

    for rel in relative:
        try:
            workflow = workflows.load(root, rel)
        except WorkflowError as exc:
            result.unreadable.append((rel, str(exc)))
            continue
        result.files.append(rel)
        for label, shell_name in workflow.foreign:
            result.foreign.append((f"{os.path.basename(rel)} {label}", shell_name))
        for script in workflow.scripts:
            result.scripts += 1
            for assertion in shell.scan(script.body):
                where = f"{os.path.basename(rel)}:{script.line_of(assertion.line)}"
                if assertion.polarity == "undecided":
                    result.undecided.append(
                        (where, assertion.undecided or "the shape was not recognised")
                    )
                    continue
                if _is_expansion(assertion.pattern):
                    # `grep -qF "$clean" out.txt` is a real assertion, but its
                    # pattern is whatever the variable held at the time. Judging
                    # it on the characters `$clean` would be judging a string
                    # that is never matched against anything.
                    result.undecided.append(
                        (where, "the pattern is a shell expansion, so its value is not known here")
                    )
                    continue
                result.assertions.append((script, assertion))

    witnesses = _witnesses(result.assertions)

    for script, assertion in result.assertions:
        if assertion.polarity == "needs-no-match":
            if assertion.pattern is None:
                continue
            if _witnessed(assertion.pattern, witnesses):
                continue
            result.findings.append(
                Finding(
                    kind="unwitnessed",
                    script=script,
                    assertion=assertion,
                    detail=(
                        "this passes when the pattern is absent, and nothing in "
                        "these workflows asserts that it is ever present -- so it "
                        "also passes if the pattern stops being produced at all"
                    ),
                )
            )
        elif assertion.polarity == "needs-match":
            detail = _prefix_detail(assertion, output_lines, against)
            if detail:
                result.findings.append(
                    Finding(kind="prefix", script=script, assertion=assertion, detail=detail)
                )
            if output_lines is not None:
                regex = _as_regex(assertion)
                if regex and not any(regex.search(line) for line in output_lines):
                    result.absent.append(
                        (
                            f"{os.path.basename(script.workflow)}:{script.line_of(assertion.line)}",
                            assertion.pattern or "",
                        )
                    )
        elif assertion.polarity in ("swallowed", "masked", "decorative"):
            detail = {
                "swallowed": "its failure is discarded, so the step passes either way",
                "masked": "its exit status is discarded by the rest of the pipeline, "
                "so the step passes either way",
                "decorative": "it chooses which branch runs, and neither branch can fail the step",
            }[assertion.polarity]
            result.findings.append(
                Finding(
                    kind=assertion.polarity,
                    script=script,
                    assertion=assertion,
                    detail=detail,
                )
            )

    result.findings.sort(key=lambda f: (KIND_ORDER.index(f.kind), f.file, f.line))
    return result
