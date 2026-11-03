"""Reading workflow files into the `run:` scripts an assertion can live in.

The one thing to know before touching this file, and it is the same thing in
every tool in this family: in YAML 1.1, which is what PyYAML implements, the
bare word `on` is a **boolean**. The key every workflow file on GitHub starts
with does not come back as `"on"`, it comes back as `True`.

What this module adds over the equivalent in `blankout`, `coldcache`,
`groupclash`, `deadtrigger` and `ungranted` is a line number for every `run:`
script. Those five all report at the level of a step, so a step's name is
enough to find the thing again. A finding here is one command inside a script
that may be forty lines long, and `ci.yml` on its own is not a location. So
the file is read twice: `safe_load` for the values, and `compose` for the node
marks, which is the only place PyYAML will tell you where a scalar started.

The marks need one correction. For a block scalar (`run: |`), PyYAML's
`start_mark` is on the `|` itself, so the script's first line is the line
*after* the mark. For a plain or quoted scalar the mark is on the content, so
the first line is the mark's own line. `_script_first_line` is that
distinction and nothing else; getting it wrong puts every finding in the file
off by one, which is worse than having no line number at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

WORKFLOW_DIR = ".github/workflows"
WORKFLOW_SUFFIXES = (".yml", ".yaml")

#: `runs-on` label prefixes whose default shell is pwsh rather than bash.
#: GitHub picks the shell from the runner when a step does not say.
WINDOWS_PREFIXES = ("windows", "win-")

#: Shells whose scripts this tool will read. A `run:` under pwsh or python is
#: a script in another language; it is skipped and counted, never guessed at.
POSIX_SHELLS = ("bash", "sh", "dash", "zsh", "ksh")


class WorkflowError(ValueError):
    """A file that could not be read as a workflow."""


@dataclass
class Script:
    """One `run:` script, and where it came from."""

    workflow: str
    job: str
    step_index: int
    step_name: str | None
    shell: str
    body: str
    #: 1-based line in the workflow file holding the script's own first line.
    first_line: int

    @property
    def label(self) -> str:
        return self.step_name or f"steps[{self.step_index}]"

    @property
    def posix(self) -> bool:
        """Whether the script is in a shell whose syntax this tool reads.

        A `shell:` of `bash -e {0}` or `/bin/bash --noprofile` counts: GitHub
        lets you give a whole command line there, and the first word is the
        interpreter.
        """
        word = self.shell.strip().split()[0] if self.shell.strip() else "bash"
        return os.path.basename(word) in POSIX_SHELLS

    def line_of(self, script_line: int) -> int:
        """Turn a 1-based line within the script into one in the workflow file."""
        return self.first_line + script_line - 1


@dataclass
class Workflow:
    """One workflow file, flattened to the scripts it runs."""

    path: str
    name: str
    scripts: list[Script] = field(default_factory=list)
    #: Scripts skipped because their shell is not a POSIX one, as
    #: `(step label, shell)`. Reported, so a skip never reads as a pass.
    foreign: list[tuple[str, str]] = field(default_factory=list)


def is_workflow_path(path: str) -> bool:
    return path.endswith(WORKFLOW_SUFFIXES)


def find_workflows(root: str) -> list[str]:
    """Every workflow file under `root`, sorted, by path relative to it."""
    directory = os.path.join(root, WORKFLOW_DIR)
    if not os.path.isdir(directory):
        return []
    found = []
    for entry in sorted(os.listdir(directory)):
        full = os.path.join(directory, entry)
        if os.path.isfile(full) and is_workflow_path(entry):
            found.append(os.path.join(WORKFLOW_DIR, entry))
    return found


def _defaults_shell(block: Any) -> Any:
    if isinstance(block, dict):
        run = block.get("run")
        if isinstance(run, dict):
            return run.get("shell")
    return None


def _windows(runs_on: Any) -> bool:
    labels = runs_on
    if isinstance(labels, str):
        labels = [labels]
    elif isinstance(labels, dict):
        labels = labels.get("labels") or []
    elif not isinstance(labels, list):
        return False
    return any(isinstance(x, str) and x.lower().startswith(WINDOWS_PREFIXES) for x in labels)


def _shell_for(step_shell: Any, job_defaults: Any, wf_defaults: Any, windows: bool) -> str:
    """The shell a step's script actually runs under.

    A step's own `shell:` wins, then the job's `defaults.run.shell`, then the
    workflow's, then the runner's default -- which is bash everywhere except
    Windows, where it is pwsh.
    """
    for candidate in (step_shell, job_defaults, wf_defaults):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return "pwsh" if windows else "bash"


def _script_first_line(node: yaml.Node) -> int:
    """The 1-based file line holding the first line of a scalar's content.

    PyYAML marks a block scalar at its `|` or `>` indicator, with the content
    starting on the next line; a plain or quoted scalar is marked at the
    content itself. `node.style` is `|` or `>` for the former and `None`,
    `'`, or `"` for the latter.
    """
    line = node.start_mark.line + 1
    if node.style in ("|", ">"):
        return line + 1
    return line


def _run_marks(text: str) -> dict[tuple[str, int], int]:
    """`(job id, step index) -> first content line` for every `run:` scalar.

    Walked over the node graph rather than the loaded document because the
    loaded document has thrown the marks away. Keyed on position so it can be
    joined to the `safe_load` pass without depending on the two walks
    producing the same objects.
    """
    marks: dict[tuple[str, int], int] = {}
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return marks
    if not isinstance(root, yaml.MappingNode):
        return marks

    def value_for(mapping: yaml.Node, key: str) -> yaml.Node | None:
        if not isinstance(mapping, yaml.MappingNode):
            return None
        for k, v in mapping.value:
            if isinstance(k, yaml.ScalarNode) and k.value == key:
                return v
        return None

    jobs = value_for(root, "jobs")
    if not isinstance(jobs, yaml.MappingNode):
        return marks
    for job_key, job_body in jobs.value:
        if not isinstance(job_key, yaml.ScalarNode):
            continue
        steps = value_for(job_body, "steps")
        if not isinstance(steps, yaml.SequenceNode):
            continue
        for index, step in enumerate(steps.value):
            run = value_for(step, "run")
            if isinstance(run, yaml.ScalarNode):
                marks[(job_key.value, index)] = _script_first_line(run)
    return marks


def load(root: str, relative_path: str) -> Workflow:
    """Read one workflow file. Raises `WorkflowError` on anything unreadable."""
    full = os.path.join(root, relative_path)
    try:
        with open(full, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise WorkflowError(f"{relative_path}: {exc}") from exc
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowError(f"{relative_path}: not valid YAML: {exc}") from exc

    if doc is None:
        raise WorkflowError(f"{relative_path}: empty file")
    if not isinstance(doc, dict):
        raise WorkflowError(f"{relative_path}: top level is not a mapping")

    workflow = Workflow(path=relative_path, name=str(doc.get("name") or relative_path))
    marks = _run_marks(text)
    wf_shell = _defaults_shell(doc.get("defaults"))

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return workflow

    for job_id, body in jobs.items():
        if not isinstance(body, dict):
            continue
        job_shell = _defaults_shell(body.get("defaults"))
        windows = _windows(body.get("runs-on"))
        steps = body.get("steps")
        if not isinstance(steps, list):
            continue
        for index, raw in enumerate(steps):
            if not isinstance(raw, dict):
                continue
            run = raw.get("run")
            if not isinstance(run, str):
                continue
            script = Script(
                workflow=relative_path,
                job=str(job_id),
                step_index=index,
                step_name=raw.get("name") if isinstance(raw.get("name"), str) else None,
                shell=_shell_for(raw.get("shell"), job_shell, wf_shell, windows),
                body=run,
                first_line=marks.get((str(job_id), index), 0),
            )
            if script.posix:
                workflow.scripts.append(script)
            else:
                workflow.foreign.append((script.label, script.shell))

    return workflow
