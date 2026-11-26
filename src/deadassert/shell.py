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

Four things in here exist because the first version got them wrong against
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
4. A branch that fails *because a second matcher missed* does not make the
   condition the verdict. `if grep -q A f; then :; else grep -q B f; fi`
   fails only when neither pattern matches, so neither one decides the step
   on its own, and calling the condition `needs-match` is a claim the step
   does not make -- logbook#78.

Where a shape is recognised but cannot be decided, the assertion carries
`polarity="undecided"` and a sentence saying why, and `analyse` prints it
under `not decided:`. Getting that wiring wrong is quiet in a particular way:
a reason attached to an assertion still labelled `needs-match` is never
printed *and* the assertion is counted and may stand as a witness. Three of
these sentences could not print at all before logbook#77 and logbook#78.
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

#: A command that fails the step, at the start of a command list. The
#: keyword alternative is not decoration: `then` and `do` separate commands
#: exactly as `;` does, so without it the only statement in
#: `if grep -q x f; then exit 1; fi` is invisible and a working guard reads
#: as `decorative`. Found while fixing logbook#73, which is the same
#: blindness one newline further along.
_FAILING = re.compile(
    r"(?:^|[;&|{(]\s*|\b(?:then|else|do)\s+)"
    r"(?:exit\s+[1-9]|exit\s*$|false\b|return\s+[1-9])"
)
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


#: A `{` opens a brace group only in word position. `${VAR}`, `awk '{print}'`
#: and a brace expansion are all part of a word and open nothing.
_GROUP_OPEN_AFTER = " \t;&|("

#: A function definition: `name() {`, `function name {`, either with a body
#: that opens on the same line. Both forms are bash; the first is POSIX.
FUNCTION = re.compile(
    r"^(?:function\s+([A-Za-z_]\w*)\s*(?:\(\s*\))?|([A-Za-z_]\w*)\s*\(\s*\))\s*\{"
)


def group_delta(text: str) -> int:
    """Net brace-group depth across one physical line.

    Counts only a `{` or `}` that stands as a word of its own, outside quotes
    and outside `$(...)`, `${...}` and backticks. Everything that rule leaves
    out is there because of what the depth is used for: a logical line stays
    open while the depth is positive, so a stray `{` that never closes would
    swallow the rest of the script. Quote state is per line, matching
    `_strip_comment`; a brace inside a string that spans lines is the one
    thing this miscounts, and `logical_lines` falls back when the script does
    not balance.
    """
    delta = 0
    quote: str | None = None
    #: Closers owed to `$(`, `${` and backticks. A brace is only a group
    #: while this is empty.
    stack: list[str] = []
    index = 0
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
        if ch == "`":
            if stack and stack[-1] == "`":
                stack.pop()
            else:
                stack.append("`")
            index += 1
            continue
        if ch == "$" and index + 1 < len(text) and text[index + 1] in "({":
            stack.append(")" if text[index + 1] == "(" else "}")
            index += 2
            continue
        if ch == "(" and stack:
            stack.append(")")
            index += 1
            continue
        if ch in ")}" and stack and stack[-1] == ch:
            stack.pop()
            index += 1
            continue
        if not stack:
            if ch == "{" and (index == 0 or text[index - 1] in _GROUP_OPEN_AFTER):
                after = text[index + 1 : index + 2]
                if after in ("", " ", "\t"):
                    delta += 1
            elif ch == "}" and (index == 0 or text[index - 1] in " \t;&("):
                delta -= 1
        index += 1
    return delta


#: Keywords that open a compound command, and the words that close one. A `;`
#: inside one of these separates commands *inside the nested block*, not the
#: commands of the list being read, so it must not split one.
_BLOCK_OPEN = ("if", "while", "until", "for", "case", "select")
_BLOCK_CLOSE = ("fi", "done", "esac")

#: What may stand immediately before a word for that word to be a command
#: rather than an argument. Without this `echo fi` closes a block.
_COMMAND_AFTER = ";&|({"
_COMMAND_KEYWORD = re.compile(r"(?:^|[;\s&|(){])(?:then|else|elif|do|in)$")

#: A command list keeps going after `then`, `else`, `do` and `in` exactly as
#: it does after a `;` -- the reason `_FAILING` lists them too. Stripped off
#: the front of a part so the command behind one is visible to both.
LEADING_KEYWORD = re.compile(r"^(?:then|else|elif|do|in)\s+")


def _is_command(text: str, index: int) -> bool:
    """Whether the word at `index` stands where a command can start."""
    before = text[:index].rstrip()
    if not before:
        return True
    return before[-1] in _COMMAND_AFTER or bool(_COMMAND_KEYWORD.search(before))


def command_parts(text: str) -> list[str]:
    """`text` split on the `;` that separate one command from the next.

    The scanning rules are `group_delta`'s, for the same reason: a `;` only
    separates commands at the top level, and not one inside quotes,
    `$(...)`, `${...}`, backticks, a `{ ... }` group, a `(...)` subshell or a
    nested `if`/`while`/`case`. Each part comes back stripped, with any
    leading `then`/`else`/`do` dropped, because a part is handed to
    `_bare_assertion` and `command_head` starts its match at the program
    name -- `then grep -q X f` reads as a command called `then`.

    Why this is a splitter and not a regex: the two shapes it has to keep
    apart are one `;` deep each.

        grep -q 'B' f; exit 1          two commands -- the exit is nobody's verdict
        grep -q 'B' f || { echo; exit 1; }   one command -- the exit is grep's verdict

    `&&` and `||` are left alone on purpose: they make what follows
    conditional on a verdict, which is the distinction `_branch_polarity`
    exists to read. A lone `&` separates commands too and is also left
    alone -- not splitting declines rather than over-claims, and nothing in
    37 workflow files backgrounds a command in a branch.
    """
    parts: list[str] = []
    start = 0
    quote: str | None = None
    #: Closers owed to `$(`, `${` and backticks, as in `group_delta`.
    stack: list[str] = []
    braces = 0
    parens = 0
    block = 0
    index = 0
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
        if ch == "`":
            if stack and stack[-1] == "`":
                stack.pop()
            else:
                stack.append("`")
            index += 1
            continue
        if ch == "$" and index + 1 < len(text) and text[index + 1] in "({":
            stack.append(")" if text[index + 1] == "(" else "}")
            index += 2
            continue
        if ch == "(" and stack:
            stack.append(")")
            index += 1
            continue
        if ch in ")}" and stack and stack[-1] == ch:
            stack.pop()
            index += 1
            continue
        if stack:
            index += 1
            continue
        if ch == "{" and (index == 0 or text[index - 1] in _GROUP_OPEN_AFTER):
            if text[index + 1 : index + 2] in ("", " ", "\t"):
                braces += 1
            index += 1
            continue
        if ch == "}" and (index == 0 or text[index - 1] in " \t;&("):
            braces -= 1
            index += 1
            continue
        if ch == "(":
            parens += 1
            index += 1
            continue
        if ch == ")":
            parens = max(parens - 1, 0)
            index += 1
            continue
        if ch.isalpha() or ch == "_":
            word = re.match(r"\w+", text[index:]).group(0)
            if _is_command(text, index):
                if word in _BLOCK_OPEN:
                    block += 1
                elif word in _BLOCK_CLOSE:
                    block -= 1
            index += len(word)
            continue
        if ch == ";" and braces <= 0 and parens <= 0 and block <= 0:
            parts.append(text[start:index])
            start = index + 1
        index += 1
    parts.append(text[start:])
    stripped = (LEADING_KEYWORD.sub("", part.strip()) for part in parts)
    return [part for part in stripped if part]


def function_spans(script: str) -> list[tuple[int, int, str]]:
    """`(first line, last line, name)` for each function definition.

    Over physical lines, so the span is usable for reporting the line a
    command inside the body is really on. An unclosed body runs to the end of
    the script: that is a malformed function, and claiming it ends earlier
    would put commands that are inside it outside.
    """
    spans: list[tuple[int, int, str]] = []
    depth = 0
    start = 0
    name = ""
    for number, raw in enumerate(script.split("\n"), 1):
        body = _strip_comment(raw).rstrip()
        if depth == 0:
            match = FUNCTION.match(body.strip())
            if match:
                start = number
                name = match.group(1) or match.group(2)
                depth = group_delta(body)
                if depth <= 0:
                    spans.append((number, number, name))
                    depth = 0
                continue
            continue
        depth += group_delta(body)
        if depth <= 0:
            spans.append((start, number, name))
            depth = 0
    if depth > 0:
        spans.append((start, len(script.split("\n")), name))
    return spans


def in_function(spans: list[tuple[int, int, str]], line: int) -> str | None:
    """The name of the function whose body holds `line`, if any."""
    for first, last, name in spans:
        if first <= line <= last:
            return name
    return None


#: A logical line is still open after one of these: the newline that follows
#: is not a command separator, it is whitespace before the next word.
_OPEN = re.compile(r"(?:\|\||&&|&|\||;|\{)$")

#: The same, for the keywords that can only be followed by a command list.
#: Only consulted inside a brace group, because at the top level an `if ...;
#: then` line is deliberately left as its own logical line for
#: `_branch_polarity` to walk.
_OPEN_KEYWORD = re.compile(r"(?:\bthen|\bdo|\belse|\bin)$")


def logical_lines(script: str) -> list[tuple[int, str]]:
    """The script as logical lines, each with the 1-based line it starts on.

    Joins backslash continuations and lines left open by a trailing `|`, `||`,
    `&&`, `;` or `{`, because an assertion is routinely written across two
    physical lines and its polarity lives on the second one.

    A brace group holds the line open until it closes, and the newlines inside
    it are joined as the command separators they are -- logbook#73. Before
    that, `cmd || {` joined one line and stopped, so

        echo hi | grep -qxF X || {
            echo "no"
            exit 1
          }

    came out as `echo hi | grep -qxF X || { echo "no"`, the `exit 1` was never
    seen, and a working assertion was reported as `decorative`. Joining with
    `; ` rather than a space is the whole fix: `_FAILING` looks for an `exit`
    that starts a command, and `"no" exit 1` does not.

    A script whose braces do not balance gets the old line-at-a-time
    behaviour. That is the conservative direction -- an unclosed `{`, whether
    in a heredoc or from a brace this miscounts, would otherwise swallow every
    line after it.
    """
    lines = script.split("\n")
    bodies = [_strip_comment(raw).rstrip() for raw in lines]
    groups = sum(group_delta(body) for body in bodies) == 0

    out: list[tuple[int, str]] = []
    buffer = ""
    start = 0
    depth = 0
    separate = False
    for number, body in enumerate(bodies, 1):
        stripped = body.strip()
        if not buffer:
            if not stripped:
                continue
            start = number
        if body.endswith("\\"):
            buffer += body[:-1].strip() + " "
            if groups:
                depth += group_delta(body[:-1])
            continue
        if separate and stripped:
            buffer += "; "
        separate = False
        buffer += stripped
        if groups:
            depth += group_delta(body)
        if _OPEN.search(buffer) or (depth > 0 and _OPEN_KEYWORD.search(buffer)):
            buffer += " "
            continue
        if depth > 0:
            separate = True
            continue
        out.append((start, buffer.strip()))
        buffer = ""
        depth = 0
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


def _branch_polarity(lines: list[tuple[int, str]], index: int) -> tuple[str, str | None]:
    """For an `if` at `index`, which branch can fail the step.

    A branch "can fail" if it holds an explicit `exit N`/`false`/`return N`,
    or a bare matcher assertion, which under `bash -e` fails the step by
    itself. Rule 2 in this module's docstring: leaving the second case out
    made a real `if`/`else` in `whichrc` look decorative.

    *How* a branch fails is tracked separately, because it changes what the
    condition means -- rule 4, and logbook#78. A branch that fails on its own
    makes the condition the whole verdict. A branch that fails only when a
    *second* matcher misses makes the two patterns alternatives, and then
    neither one decides the step by itself.

    `elif` is the same walk with two corrections, both logbook#82. An `elif`
    is *the subject* when the matcher is in its own condition: its branch is
    its own `then`, and the walk has to start already inside the enclosing
    block, because `depth` is counted from the tokens on each line and the
    `if` in `elif` is preceded by `el` rather than a separator -- so it was
    never counted, the walk broke on its first line, and the function fell
    through to `decorative` over no branches at all.

    An `elif` is *part of the else-path* when the matcher is in an earlier
    condition in the same chain. A later condition holding a matcher gates
    everything the else-path can do, so a failure reached past one is not a
    failure "no matcher gets a say in" -- it is logbook#78's either-or with
    the second matcher one line further away. `if A; then :; elif B; then
    exit 1; fi` exits 1 only when `A` misses *and* `B` matches, which is not
    what `needs-match` claims about `A`.

    Polarity is what this condition's status does to the step *when it is
    reached*, which is the convention everywhere else here: a matcher nested
    inside another `if` is read the same way, and an `elif` is gated by the
    conditions above it exactly as nesting gates that one.
    """
    subject = lines[index][1]
    #: An `elif` subject is already inside its enclosing `if`. Seeding this
    #: is the whole of logbook#82's first half: counted from the line's own
    #: tokens it stays 0, and `if depth <= 0: break` then ends the walk
    #: before a single branch has been read.
    depth = 1 if subject.startswith("elif") else 0
    #: Whether the walk ever got inside the block it is about. A walk that
    #: did not has established nothing, and must not report `decorative` --
    #: logbook#77's lesson one function up, made an invariant here.
    entered = depth >= 1
    branch = "then"
    #: Whether the branch can fail the step at all.
    fails = {"then": False, "else": False}
    #: Whether it has a way of failing that no matcher gets a say in.
    alone = {"then": False, "else": False}
    #: Set once a later `elif` condition holds a matcher: from there on the
    #: else-path is gated by it, so nothing it does is unconditional.
    gated = False

    def note(which: str, part: str) -> None:
        # Asked per command, not per part. A branch body is a command list,
        # and both questions below are about one command: `grep -q B f`
        # followed by `exit 1` fails whatever `B` does, while
        # `grep -q B f || { exit 1; }` fails only when `B` misses, and read
        # as one blob those two are indistinguishable -- logbook#81.
        for piece in command_parts(part):
            asserts = _bare_assertion(piece)
            if asserts:
                fails[which] = True
            if _FAILING.search(piece):
                fails[which] = True
                # An `exit` reached through `|| { ... }` on a matcher is that
                # matcher's verdict, not an unconditional failure. Neither is
                # one reached past an `elif` condition that holds a matcher.
                unconditional = not asserts and not (which == "else" and gated)
                alone[which] = alone[which] or unconditional

    for offset, (_, text) in enumerate(lines[index:]):
        for token in re.findall(r"(?:^|[;\s])(if|fi)(?=[;\s]|$)", text):
            depth += 1 if token == "if" else -1
            # Asked per token, not per line: a one-line `if ... fi` opens and
            # closes the block before the line ends, so a depth read after the
            # line says the walk was never inside it. That shape is live, and
            # `TestOneLineIf` caught this guard getting it wrong.
            entered = entered or depth >= 1
        # `offset` keeps the subject line out of this: an `elif` subject is
        # its own `then` branch, and flipping on it would credit everything
        # it does to an `else` that has not started.
        if offset and depth == 1 and re.match(r"^(?:else|elif)\b", text):
            branch = "else"
            condition = re.match(r"^elif\s+(.*?)(?:;\s*then\b|$)", text)
            if condition and _bare_assertion(condition.group(1)):
                gated = True
        body = text
        head = re.match(r"^(?:el)?if\s+(.*?)(?:;\s*then\b|$)", text)
        if head and text.startswith(("if", "elif")):
            # The condition is not the branch; a matcher in it is the subject.
            body = text[head.end(1) :]
        # A one-line `if` carries both branches in `body`, and crediting an
        # `exit` to the wrong one inverts the polarity -- worse than the
        # `decorative` it replaces. Split before deciding.
        inline = re.split(r";\s*(?:else|elif)\b", body, maxsplit=1)
        if len(inline) == 2:
            for part, which in zip((piece.strip() for piece in inline), ("then", "else")):
                note(which, part)
        else:
            note(branch, body)
        if depth <= 0:
            break
    if depth > 0:
        return "undecided", "the `if` has no matching `fi` in this script"
    if fails["then"] and not fails["else"]:
        if not alone["then"]:
            return "undecided", (
                "the matcher is one side of an either-or: the `then` branch asserts too, "
                "so a match here is not on its own what fails the step"
            )
        return "needs-no-match", None
    if fails["else"] and not fails["then"]:
        if not alone["else"]:
            return "undecided", (
                "the matcher is one side of an either-or: the `else` branch asserts too, "
                "so the step fails only when neither pattern matches"
            )
        return "needs-match", None
    if fails["then"] and fails["else"]:
        return "undecided", "either branch of the `if` can fail the step"
    if not entered:
        return "undecided", "the `if` structure could not be read, so no branch was examined"
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
    """Every matcher command in `script` whose status reaches the step.

    A matcher inside a function body is not one of them, and is skipped here
    rather than read -- logbook#66. Whether such a matcher decides the step
    depends on where the function is *called*: bare under `set -e` its
    verdict reaches the step, inside an `if` condition it does not, and an
    uncalled function asserts nothing at all. This module does not work that
    out, so it declines, and `unread.scan` announces what was declined. What
    it must not do is the thing it did before: read some of them, by accident,
    on whichever body lines happened to start with a matcher.
    """
    lines = logical_lines(script)
    spans = function_spans(script)
    found: list[Assertion] = []

    for index, (lineno, text) in enumerate(lines):
        if not any(name in text for name in MATCHERS):
            continue
        if in_function(spans, lineno):
            continue

        polarity: str | None = None
        undecided: str | None = None
        condition = text

        head = re.match(r"^(?:el)?if\s+(.*?)(?:;\s*then\b|$)", text)
        loop = re.match(r"^(?:while|until)\s+(.*?)(?:;\s*do\b|$)", text)
        if head and text.startswith(("if", "elif")):
            condition = head.group(1)
            polarity, undecided = _branch_polarity(lines, index)
        elif loop:
            # The keyword has to come off the front, exactly as the `if` path
            # takes its condition out. Left on, `_matcher_head` reads `while`
            # as the program, no matcher is found, no `Assertion` is appended,
            # and the sentence built on the next line is thrown away with the
            # loop variable -- logbook#77.
            condition = loop.group(1)
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
