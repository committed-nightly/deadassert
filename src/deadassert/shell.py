"""Find the assertions in a shell script, and say which way round each fails.

An assertion, here, is a command whose **exit status** decides whether the CI
step passes. `grep -q 'all clear' out.txt` is one. `grep -c 'x' out.txt > n`
is not -- it is extraction, and its status is incidental to what it is for.
Telling those two apart is most of this file, and getting it wrong in the
generous direction is how a tool like this becomes something people turn off.

The distinction that the rest of `deadassert` is built on is **polarity**:

* `needs-match` -- the step fails when the pattern is *absent*. If the output
  it names changes, CI goes red and somebody fixes it. It rots loudly.
* `needs-no-match` -- the step fails when the pattern is *present*. If the
  output it names changes, the assertion keeps passing, forever, while no
  longer checking anything. It rots silently.

Three things in here exist because the first version got them wrong against
real workflow files, and each one was a false positive rather than a miss:

1. A leading `!` negates the whole **pipeline**, not the command it sits in
   front of. `! printf '%s' "$out" | grep -q X` fails the step when grep
   *does* match, and the `!` is five words away from the grep.
2. A branch can fail the step without an `exit`. Under `bash -e`, which is
   what GitHub Actions runs, a bare `grep -q X f` in an `else` is itself an
   assertion and fails the step on its own. A branch holding only that is not
   a branch that does nothing.
3. `line=$(whichrc -q | grep '\\.bash_login:') || true` is a capture, not a
   swallowed assertion. The `|| true` is correct there -- no match is an
   expected answer, and the test comes later, on the variable.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

#: Programs whose exit status answers "did this appear?".
MATCHERS = ("grep", "egrep", "fgrep", "rg")

#: Flags that mean the command is being run for its output, not its verdict.
PRODUCING = ("o", "c", "l", "L")

#: Flags that mean the command is being run for its verdict only.
QUIET = ("q", "s")

#: Polarities for which the step's pass/fail actually depends on the match.
LIVE = ("needs-match", "needs-no-match")

_FAILING = re.compile(r"(?:^|[;&|{(]\s*)(?:exit\s+[1-9]|exit\s*$|false\b|return\s+[1-9])")
_SWALLOW = re.compile(r"\|\|\s*(?:true\b|:\s*(?:;|$))")
_LONG_PRODUCING = ("--only-matching", "--count", "--files-with-matches", "--files-without-match")


@dataclass
class Assertion:
    """One matcher command whose exit status reaches the step's exit status."""

    #: 1-based, within the script.
    line: int
    #: The logical line it was found in, comments stripped, as one line.
    text: str
    program: str
    pattern: str | None
    polarity: str
    #: Fixed-string matching: `-F`, `fgrep`, or `rg -F`.
    fixed: bool
    #: Whole-line matching: `-x`.
    anchored: bool
    #: Set when the shape was recognised but not decided. Never a finding.
    undecided: str | None = None

    @property
    def live(self) -> bool:
        return self.polarity in LIVE


def _strip_comment(line: str) -> str:
    """Drop a trailing `#` comment, respecting quotes.

    Naive splitting on `#` would cut `grep -qF '^not checked: #3'` in half and
    silently change the pattern being reported.
    """
    out: list[str] = []
    quote = None
    for index, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (index == 0 or line[index - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out)


def logical_lines(script: str) -> list[tuple[int, str]]:
    """The script as logical lines, each with the 1-based line it starts on.

    Joins backslash continuations and lines left open by a trailing `|`, `||`,
    `&&`, `;` or `{`, because an assertion is routinely written across two
    physical lines and its polarity lives on the second one.
    """
    out: list[tuple[int, str]] = []
    buffer = ""
    start = 0
    for number, raw in enumerate(script.split("\n"), 1):
        body = _strip_comment(raw).rstrip()
        stripped = body.strip()
        if not buffer:
            if not stripped:
                continue
            start = number
        if body.endswith("\\"):
            buffer += body[:-1].strip() + " "
            continue
        buffer += stripped
        if re.search(r"(?:\|\||&&|&|\||;|\{)$", buffer):
            buffer += " "
            continue
        out.append((start, buffer.strip()))
        buffer = ""
    if buffer.strip():
        out.append((start, buffer.strip()))
    return out


def _substitution_spans(text: str) -> list[tuple[int, int]]:
    """Character spans covered by `$(...)` or backticks.

    A matcher inside one of these is feeding a variable, not deciding the
    step. `<(...)` is deliberately not included: a process substitution is an
    argument, and the matcher outside it is still the assertion.
    """
    spans: list[tuple[int, int]] = []
    quote = None
    stack: list[int] = []
    index = 0
    while index < len(text):
        ch = text[index]
        if quote == "'":
            if ch == "'":
                quote = None
            index += 1
            continue
        if ch == "'" and quote is None:
            quote = "'"
            index += 1
            continue
        if ch == '"' and quote in (None, '"'):
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if ch == "`":
            if stack:
                spans.append((stack.pop(), index))
            else:
                stack.append(index)
            index += 1
            continue
        if ch == "$" and index + 1 < len(text) and text[index + 1] == "(":
            stack.append(index)
            index += 2
            continue
        if ch == ")" and stack:
            spans.append((stack.pop(), index))
            index += 1
            continue
        index += 1
    return spans


def _in_substitution(text: str, offset: int) -> bool:
    return any(a <= offset <= b for a, b in _substitution_spans(text))


def pipeline_parts(text: str) -> list[tuple[int, str]]:
    """Split on a `|` that is neither `||` nor inside quotes. Offsets kept."""
    parts: list[tuple[int, str]] = []
    buffer = ""
    begin = 0
    quote = None
    index = 0
    while index < len(text):
        ch = text[index]
        if quote:
            buffer += ch
            if ch == quote:
                quote = None
            index += 1
            continue
        if ch in "'\"":
            quote = ch
            buffer += ch
            index += 1
            continue
        if ch == "|":
            if index + 1 < len(text) and text[index + 1] == "|":
                buffer += "||"
                index += 2
                continue
            parts.append((begin, buffer))
            buffer = ""
            index += 1
            begin = index
            continue
        buffer += ch
        index += 1
    parts.append((begin, buffer))
    return parts


def command_head(segment: str, programs: tuple[str, ...] = MATCHERS) -> tuple[str, bool] | None:
    """A simple command whose program is one of `programs`, and whether `!`-negated.

    `programs` is a parameter because `unread` asks the same question of
    `diff` and `jq` that this module asks of `grep`, and the fiddly part --
    leading `VAR=value` assignments, `env`/`command` prefixes, a path to the
    program, and a `!` that belongs to the pipeline -- is the same either way.
    """
    text = segment.strip()
    negated = False
    while text.startswith("!"):
        negated = True
        text = text[1:].strip()
    # Step over leading `VAR=value` assignments and `command`/`env` prefixes.
    while True:
        match = re.match(r"(?:[A-Za-z_]\w*=\S*|command|env)\s+", text)
        if not match:
            break
        text = text[match.end() :]
    match = re.match(r"([A-Za-z_][\w./-]*)", text)
    if not match:
        return None
    if match.group(1).split("/")[-1] not in programs:
        return None
    return text, negated


def _matcher_head(segment: str) -> tuple[str, bool] | None:
    """The matcher command in a simple command, and whether `!`-negated."""
    return command_head(segment, MATCHERS)


def swallows(text: str) -> bool:
    """Whether `text` discards a command's failure with `|| true` or `|| :`."""
    return bool(_SWALLOW.search(text))


def in_substitution(text: str, offset: int) -> bool:
    """Whether `offset` in `text` falls inside `$(...)` or backticks."""
    return _in_substitution(text, offset)


def split(text: str) -> list[str] | None:
    """`shlex.split`, or None when the text does not tokenise."""
    return _split(text)


def parse_matcher(words: list[str]) -> tuple[str | None, bool, bool, bool, bool]:
    """`(pattern, fixed, anchored, quiet, producing)` from a matcher argv."""
    pattern: str | None = None
    fixed = words[0].split("/")[-1] == "fgrep"
    anchored = quiet = producing = False
    expecting = False
    index = 1
    while index < len(words):
        word = words[index]
        if expecting:
            pattern = word if pattern is None else pattern
            expecting = False
            index += 1
            continue
        if word == "--":
            index += 1
            if pattern is None and index < len(words):
                pattern = words[index]
                index += 1
            continue
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            if name in ("--fixed-strings",):
                fixed = True
            elif name in ("--line-regexp",):
                anchored = True
            elif name in ("--quiet", "--silent"):
                quiet = True
            elif name in _LONG_PRODUCING:
                producing = True
            elif name == "--regexp":
                if "=" in word:
                    pattern = word.split("=", 1)[1] if pattern is None else pattern
                else:
                    expecting = True
            index += 1
            continue
        if word.startswith("-") and len(word) > 1:
            letters = word[1:]
            if "F" in letters:
                fixed = True
            if "x" in letters:
                anchored = True
            if any(flag in letters for flag in QUIET):
                quiet = True
            if any(flag in letters for flag in PRODUCING):
                producing = True
            if "e" in letters:
                tail = letters[letters.index("e") + 1 :]
                if tail:
                    pattern = tail if pattern is None else pattern
                else:
                    expecting = True
            index += 1
            continue
        if pattern is None:
            pattern = word
        index += 1
    return pattern, fixed, anchored, quiet, producing


def _redirects_stdout(segment: str) -> bool:
    """Whether a simple command sends its stdout somewhere other than a pipe."""
    return bool(re.search(r"(?<![0-9<>])>{1,2}\s*\S", segment))


def _branch_polarity(lines: list[tuple[int, str]], index: int) -> tuple[str | None, str | None]:
    """For an `if` at `index`, which branch can fail the step.

    A branch "can fail" if it holds an explicit `exit N`/`false`/`return N`,
    or a bare matcher assertion, which under `bash -e` fails the step by
    itself. Rule 2 in this module's docstring: leaving the second case out
    made a real `if`/`else` in `whichrc` look decorative.
    """
    depth = 0
    branch = "then"
    fails = {"then": False, "else": False}
    for _, text in lines[index:]:
        for token in re.findall(r"(?:^|[;\s])(if|fi)(?=[;\s]|$)", text):
            depth += 1 if token == "if" else -1
        if depth == 1 and (re.match(r"^(?:else|elif)\b", text) or re.search(r";\s*else\b", text)):
            branch = "else"
        body = text
        head = re.match(r"^(?:el)?if\s+(.*?)(?:;\s*then\b|$)", text)
        if head and text.startswith(("if", "elif")):
            # The condition is not the branch; a matcher in it is the subject.
            body = text[head.end(1) :]
        if _FAILING.search(body) or _bare_assertion(body):
            fails[branch] = True
        if depth <= 0:
            break
    if depth > 0:
        return None, "the `if` has no matching `fi` in this script"
    if fails["then"] and not fails["else"]:
        return "needs-no-match", None
    if fails["else"] and not fails["then"]:
        return "needs-match", None
    if fails["then"] and fails["else"]:
        return None, "either branch of the `if` can fail the step"
    return "decorative", None


def _bare_assertion(text: str) -> bool:
    """Whether `text` holds a matcher run for its verdict, under `set -e`."""
    for _, segment in pipeline_parts(text):
        head = _matcher_head(segment)
        if head is None:
            continue
        words = _split(head[0])
        if words is None:
            continue
        _, _, _, quiet, producing = parse_matcher(words)
        if quiet and not producing:
            return True
    return False


def _split(text: str) -> list[str] | None:
    try:
        return shlex.split(text)
    except ValueError:
        return None


def scan(script: str) -> list[Assertion]:
    """Every matcher command in `script` whose status reaches the step."""
    lines = logical_lines(script)
    found: list[Assertion] = []

    for index, (lineno, text) in enumerate(lines):
        if not any(name in text for name in MATCHERS):
            continue

        polarity: str | None = None
        undecided: str | None = None
        condition = text

        head = re.match(r"^(?:el)?if\s+(.*?)(?:;\s*then\b|$)", text)
        if head and text.startswith(("if", "elif")):
            condition = head.group(1)
            polarity, undecided = _branch_polarity(lines, index)
        elif re.match(r"^(?:while|until)\b", text):
            undecided = "the matcher is a loop condition, not a verdict"
            polarity = "undecided"

        if polarity is None:
            parts = re.split(r"\s(\|\||&&)\s", condition, maxsplit=1)
            if len(parts) == 3:
                condition, operator, consequence = parts
                if operator == "||":
                    if _SWALLOW.search(f"|| {consequence}"):
                        polarity = "swallowed"
                    elif _FAILING.search(consequence) or _bare_assertion(consequence):
                        polarity = "needs-match"
                    else:
                        polarity = "decorative"
                elif _FAILING.search(consequence) or _bare_assertion(consequence):
                    polarity = "needs-no-match"
                else:
                    polarity = "decorative"
            else:
                # A bare command under `bash -e`: a non-zero status ends the step.
                polarity = "needs-match"

        negated_pipeline = False
        condition = condition.strip()
        while condition.startswith("!"):
            negated_pipeline = not negated_pipeline
            condition = condition[1:].strip()

        parts = pipeline_parts(condition)
        for position, (offset, segment) in enumerate(parts):
            head_match = _matcher_head(segment)
            if head_match is None:
                continue
            command, negated_command = head_match
            words = _split(command)
            if words is None:
                continue
            pattern, fixed, anchored, quiet, producing = parse_matcher(words)

            # Rule 1 and 3: not every matcher is an assertion.
            if producing:
                continue
            if _in_substitution(text, offset + segment.index(command.split()[0])):
                continue
            if not quiet and (_redirects_stdout(segment) or position != len(parts) - 1):
                continue

            this = polarity
            if negated_command != negated_pipeline:
                this = {"needs-match": "needs-no-match", "needs-no-match": "needs-match"}.get(
                    this, this
                )
            why = undecided
            if position != len(parts) - 1 and this in LIVE:
                this = "masked"
                why = "its exit status is discarded by the rest of the pipeline"

            found.append(
                Assertion(
                    line=lineno,
                    text=text,
                    program=words[0].split("/")[-1],
                    pattern=pattern,
                    polarity=this or "undecided",
                    fixed=fixed,
                    anchored=anchored,
                    undecided=why,
                )
            )
    return found
