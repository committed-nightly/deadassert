"""Assertion-shaped constructs in a `run:` script that this tool does not read.

`deadassert` reads matchers -- `grep`, `rg` -- and says which of them cannot
fail. A step can assert in other ways, and the ones it cannot see fail in
exactly the shapes it exists to report. From `logbook#69`:

    stockbox roots -c loose.toml --json > loose.json
    python3 -c "
    import json
    tight = json.load(open('trees.json'))['shapes'][0]['candidates']
    loose = json.load(open('loose.json'))['shapes'][0]['candidates']
    assert loose > tight * 5, (loose, tight)
    "

That is an assertion whose truth depends on the runner image rather than on
the thing it names -- the same defect `deadassert` reported two steps up in
the same file -- and it was invisible, because it is a Python `assert` and
not a grep. The run said `2 findings` and `32 assertions in 20 scripts`, with
nothing to say that one of those 20 scripts asserted something it had not
read. The sweep that exists to find dead assertions walked past one.

**This module does not evaluate anything.** It finds the constructs and
counts them, so that a clean run says what it covered instead of implying it
covered everything -- the same move `deadassert` already makes with `not
decided:` and with the skipped non-POSIX shells. Deciding whether a `diff`
or a `jq -e` can fail is a different question, and a bigger one; the count is
the part that stops a clean run overclaiming.

Three shapes, chosen by counting what the organisation's 38 workflow files
actually use:

* `assert` -- a language's own assert, almost always Python via `python -c`
  or a heredoc. The one from `logbook#69`.
* `diff` -- and `cmp`. This is the shape this repository's own README
  recommends over `grep`, which is exactly why it needs counting: a file
  that takes the advice ends up with a tool that reads almost none of its
  assertions and does not currently admit it.
* `jq -e` -- the one jq invocation whose exit status is a verdict.

`test "$s" -eq 0` and `[ -f x ]` are deliberately **not** counted, per the
caveat on `logbook#69`. They are the most common assertion in the org and
almost always sound -- the status *is* the assertion, with no pattern to rot
-- so counting them would put a non-zero number on nearly every file in the
org on day one and the number would stop being read. The cost of that choice
is real and it is written down in the README: a `test` whose captured status
came from the wrong command is not covered here either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import shell

#: Programs whose exit status is a verdict about two files being equal.
DIFFERS = ("diff", "cmp")

#: Interpreters whose `-c` or heredoc body can hold an `assert`.
INTERPRETERS = ("python", "python3", "node", "ruby", "perl")

#: A line that *is* an assert statement, in an embedded program. `assert` is
#: not a shell command, so a `run:` line beginning with it is inside a
#: heredoc or a quoted `-c` body by construction. Requiring whitespace or a
#: bracket after the word keeps `assert_called_once()` and a bare mention of
#: `assertions` out.
_ASSERT_LINE = re.compile(r"^assert[\s(]")

#: An interpreter and an `assert` on one physical line: `python3 -c 'assert x'`.
#: `\bassert\b` rather than a substring so that `import deadassert` -- and the
#: name of this very package -- does not match.
_ASSERT_INLINE = re.compile(rf"\b(?:{'|'.join(INTERPRETERS)})\b.*\bassert\b")

#: Comment openers to skip. `#` covers both shell and Python; `//` covers an
#: embedded node program.
_COMMENT = ("#", "//")

#: Shell keywords that can sit in front of a simple command. `if diff -q a b;
#: then` is a `diff` assertion, and without this the clause's first word is
#: `if` and the construct is missed -- which is how the first version of this
#: module read `logbook#69`'s sibling shape as absent.
_KEYWORD = re.compile(r"^\s*(?:if|elif|while|until|then|else|do|done|fi|!|\{|\()\s+")

#: Operators that end one simple command and begin the next.
_OPERATORS = ("||", "&&", ";;", ";", "|", "&")


@dataclass
class Unread:
    """One assertion-shaped construct this tool did not read."""

    #: 1-based, within the script.
    line: int
    #: One of `assert`, `diff`, `jq -e`.
    kind: str
    #: The line it was found in, stripped, as one line.
    text: str


def _asserts(script: str) -> list[Unread]:
    """`assert` statements, found on physical lines.

    Physical rather than logical lines on purpose. `shell.logical_lines`
    models a shell script, and an embedded Python program is not one: the
    `python3 -c "` that opens it leaves a quote hanging that the shell
    tokeniser would have to track across the whole body to get right. The
    line-shape test below needs none of that, and it cannot be fooled into
    reporting the wrong line number.
    """
    found: list[Unread] = []
    for number, raw in enumerate(script.split("\n"), 1):
        text = raw.strip()
        if not text or text.startswith(_COMMENT):
            continue
        if _ASSERT_LINE.match(text) or _ASSERT_INLINE.search(text):
            found.append(Unread(line=number, kind="assert", text=text))
    return found


def _jq_asserts(words: list[str]) -> bool:
    """Whether a `jq` argv asks for `--exit-status`.

    Plain `jq -r .x` is extraction and its status says only that the JSON
    parsed. `-e` is the one that makes it a verdict about the value.
    """
    for word in words[1:]:
        if word == "--exit-status":
            return True
        if word.startswith("--"):
            continue
        if word.startswith("-") and len(word) > 1 and "e" in word[1:]:
            return True
    return False


def _simple_commands(text: str) -> list[tuple[int, str]]:
    """Every simple command in a logical line, with its offset in that line.

    `shell.pipeline_parts` splits on `|` alone, which is the right split for
    deciding whether a matcher's exit status survives its position. This
    module declines to decide that (see `_commands`) and wants something
    different: every place a command *starts*, so that the `diff` in
    `if diff -q a b; then ...` is found at all. So it splits on every
    operator and then steps over any shell keyword in front of the command.
    """
    out: list[tuple[int, str]] = []
    begin = 0
    quote = None
    index = 0

    def emit(start: int, end: int) -> None:
        chunk = text[start:end]
        while True:
            match = _KEYWORD.match(chunk)
            if not match:
                break
            start += match.end()
            chunk = text[start:end]
        if chunk.strip():
            out.append((start, chunk))

    while index < len(text):
        ch = text[index]
        if quote:
            if ch == quote:
                quote = None
            index += 1
            continue
        if ch in "'\"":
            quote = ch
            index += 1
            continue
        for operator in _OPERATORS:
            if text.startswith(operator, index):
                emit(begin, index)
                index += len(operator)
                begin = index
                break
        else:
            index += 1
    emit(begin, len(text))
    return out


def _commands(script: str) -> list[Unread]:
    """`diff`, `cmp` and `jq -e` commands whose failure is not discarded.

    Two exclusions, and only two, because this is a disclosure and not a
    verdict:

    * inside `$(...)` or backticks -- a capture, feeding a variable, exactly
      as in `shell.scan`;
    * `|| true` on the logical line -- the failure is thrown away, so there
      is no assertion here to have missed.

    Everything else is counted, including a `diff` partway down a pipeline.
    Working out whether such a status reaches the step is the analysis this
    module is explicitly not doing, and guessing it away would reintroduce
    the silence the module exists to break.
    """
    found: list[Unread] = []
    programs = DIFFERS + ("jq",)
    for lineno, text in shell.logical_lines(script):
        if not any(name in text for name in programs):
            continue
        if shell.swallows(text):
            continue
        for offset, segment in _simple_commands(text):
            head = shell.command_head(segment, programs)
            if head is None:
                continue
            words = shell.split(head[0])
            if words is None:
                continue
            if shell.in_substitution(text, offset):
                continue
            if words[0].split("/")[-1] == "jq":
                if _jq_asserts(words):
                    found.append(Unread(line=lineno, kind="jq -e", text=text))
            else:
                found.append(Unread(line=lineno, kind="diff", text=text))
    return found


def scan(script: str) -> list[Unread]:
    """Every assertion-shaped construct in `script` that `shell.scan` ignores.

    Sorted by line, so the report reads in file order.
    """
    return sorted(_asserts(script) + _commands(script), key=lambda item: item.line)
