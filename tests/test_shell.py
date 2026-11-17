"""Polarity, and the three shapes that made the first version wrong."""

from __future__ import annotations

import pytest

from deadassert import shell


def one(script: str) -> shell.Assertion:
    found = shell.scan(script)
    assert len(found) == 1, f"expected one assertion, got {found}"
    return found[0]


def polarity(script: str) -> str:
    return one(script).polarity


class TestPolarity:
    def test_bare_command_needs_a_match(self):
        # Under `bash -e`, which is what Actions runs, a non-zero status here
        # ends the step.
        assert polarity("grep -qF 'all clear' out.txt") == "needs-match"

    def test_bang_needs_no_match(self):
        assert polarity("! grep -qF 'error' out.txt") == "needs-no-match"

    def test_or_exit_needs_a_match(self):
        assert polarity("grep -qF 'x' out.txt || { echo no; exit 1; }") == "needs-match"

    def test_and_exit_needs_no_match(self):
        assert polarity("grep -qF 'x' out.txt && { echo bad; exit 1; }") == "needs-no-match"

    def test_if_then_exit_needs_no_match(self):
        script = "if grep -qF 'oops' out.txt; then\n  echo bad >&2\n  exit 1\nfi"
        assert polarity(script) == "needs-no-match"

    def test_if_not_then_exit_needs_a_match(self):
        script = "if ! grep -qF 'good' out.txt; then\n  echo missing\n  exit 1\nfi"
        assert polarity(script) == "needs-match"

    def test_else_exit_needs_a_match(self):
        script = "if grep -qF 'good' out.txt; then\n  :\nelse\n  exit 1\nfi"
        assert polarity(script) == "needs-match"

    def test_or_true_is_swallowed(self):
        assert polarity("grep -qF 'x' out.txt || true") == "swallowed"

    def test_or_colon_is_swallowed(self):
        assert polarity("grep -qF 'x' out.txt || :") == "swallowed"

    def test_if_with_no_failing_branch_is_decorative(self):
        script = "if grep -qF 'x' out.txt; then\n  echo found\nelse\n  echo absent\nfi"
        assert polarity(script) == "decorative"

    def test_status_discarded_by_pipeline_is_masked(self):
        assert polarity("grep -qF 'x' out.txt | tee -a log") == "masked"


class TestPipelineNegation:
    """The `!` binds to the pipeline, not to the command it precedes.

    `! printf '%s' "$out" | grep -q X` fails the step when grep *does* match,
    and the `!` is nowhere near the grep. Reading it as unnegated classified
    three real assertions in this organisation's workflows backwards.
    """

    def test_bang_in_front_of_a_pipeline_reaches_the_last_command(self):
        script = "if ! printf '%s\\n' \"$out\" | grep -qx 'done.'; then\n  exit 1\nfi"
        assert polarity(script) == "needs-match"

    def test_without_the_bang_the_same_pipeline_is_the_other_way(self):
        script = "if printf '%s\\n' \"$out\" | grep -qx 'done.'; then\n  exit 1\nfi"
        assert polarity(script) == "needs-no-match"

    def test_bang_on_the_grep_itself_still_works(self):
        assert polarity("cat out.txt | ! grep -qF 'x'") == "needs-no-match"


class TestNotAnAssertion:
    """A matcher run for its output is not a matcher run for its verdict."""

    def test_output_flag_is_extraction(self):
        script = "man githooks | col -b | grep -oE '^ [a-z-]*$' | sort > /tmp/from-man"
        assert shell.scan(script) == []

    def test_count_flag_is_extraction(self):
        assert shell.scan("grep -c 'x' out.txt > n.txt") == []

    def test_files_with_matches_is_extraction(self):
        assert shell.scan("grep -rl 'TODO' src > files.txt") == []

    def test_command_substitution_is_a_capture(self):
        # The `|| true` here is correct: no match is an expected answer and
        # the test comes later, on the variable.
        script = "line=$(whichrc --home \"$HOME\" -q | grep '\\.bash_login:') || true"
        assert shell.scan(script) == []

    def test_backtick_substitution_is_a_capture(self):
        assert shell.scan("n=`grep 'x' out.txt`") == []

    def test_unquiet_grep_redirected_to_a_file_is_extraction(self):
        assert shell.scan("grep 'warning' log.txt > warnings.txt") == []

    def test_quiet_grep_redirected_to_devnull_is_still_an_assertion(self):
        # -q already means "status only"; the redirect is belt and braces.
        assert polarity("grep -q 'x' out.txt > /dev/null") == "needs-match"

    def test_process_substitution_is_not_a_capture(self):
        # `<(...)` is an argument. The grep outside it is still the assertion.
        script = "if grep -q 'unread' <(whichrc -q); then\n  exit 1\nfi"
        assert polarity(script) == "needs-no-match"


class TestBareAssertionCanFailABranch:
    """A branch can fail the step without an `exit`.

    Under `bash -e` a bare `grep -q X f` in an `else` is itself an assertion.
    Looking only for `exit`/`false`/`return` called a real `if`/`else` in
    `whichrc` decorative when its else branch asserts.
    """

    def test_else_branch_holding_only_a_bare_grep(self):
        script = (
            "if grep -q '^ ssh *nothing$' rows.txt; then\n"
            "  echo 'this bash does not source it'\n"
            "else\n"
            "  grep -q '^ ssh *\\.bashrc$' rows.txt\n"
            "fi"
        )
        found = shell.scan(script)
        assert [a.polarity for a in found] == ["needs-match", "needs-match"]


class TestFlags:
    @pytest.mark.parametrize(
        "command,fixed,anchored",
        [
            ("grep -qF 'x' f", True, False),
            ("grep -qxF 'x' f", True, True),
            ("grep -qFx 'x' f", True, True),
            ("grep -q -F -x 'x' f", True, True),
            ("grep -q --fixed-strings --line-regexp 'x' f", True, True),
            ("fgrep -q 'x' f", True, False),
            ("grep -q 'x' f", False, False),
            ("grep -qE 'a|b' f", False, False),
        ],
    )
    def test_fixed_and_anchored(self, command, fixed, anchored):
        found = one(command)
        assert (found.fixed, found.anchored) == (fixed, anchored)

    @pytest.mark.parametrize(
        "command,pattern",
        [
            ("grep -qF 'needle' f", "needle"),
            ("grep -q -e 'needle' f", "needle"),
            ("grep -qF --regexp=needle f", "needle"),
            ("grep -qF -- '-leading-dash' f", "-leading-dash"),
            ("grep -qF 'two words here' f", "two words here"),
        ],
    )
    def test_pattern(self, command, pattern):
        assert one(command).pattern == pattern

    def test_pattern_keeps_a_trailing_space(self):
        # `'unbuilt: '` is a real assertion in this org and the space is the point.
        assert one("grep -qF 'unbuilt: ' out.txt").pattern == "unbuilt: "


class TestLogicalLines:
    def test_backslash_continuation_is_one_assertion(self):
        script = "grep -qF 'x' out.txt \\\n  || { echo no; exit 1; }"
        assert polarity(script) == "needs-match"

    def test_trailing_pipe_continuation(self):
        script = "cat out.txt |\n  grep -qF 'x'"
        assert polarity(script) == "needs-match"

    def test_comment_is_stripped(self):
        assert shell.scan("# grep -qF 'x' out.txt") == []

    def test_hash_inside_a_pattern_is_not_a_comment(self):
        assert one("grep -qF 'issue #3 open' out.txt").pattern == "issue #3 open"

    def test_line_number_is_within_the_script(self):
        script = "set -e\n\ncat out.txt\ngrep -qF 'x' out.txt"
        assert one(script).line == 4


class TestBraceGroups:
    """logbook#73: a `|| { ... }` spanning lines read as `decorative`.

    The four shapes below all fail the step when the pattern is absent, and
    were checked under `bash -e` before this class was written. Any of them
    coming out as anything but `needs-match` is a false positive on a working
    assertion -- the mirror of what the tool exists to find.
    """

    def test_multi_line_brace_group_is_an_assertion(self):
        script = 'echo hi | grep -qxF \'x\' || {\n    echo "no"\n    exit 1\n  }'
        assert polarity(script) == "needs-match"

    def test_single_line_brace_group_still_is(self):
        assert polarity("echo hi | grep -qxF 'x' || { echo no; exit 1; }") == "needs-match"

    def test_multi_line_and_brace_group_needs_no_match(self):
        script = "grep -qF 'oops' out.txt && {\n    echo bad\n    exit 1\n  }"
        assert polarity(script) == "needs-no-match"

    def test_newlines_in_the_group_become_separators(self):
        # The bug was not the joining, it was joining with a space: `_FAILING`
        # wants an `exit` that starts a command, and `"no" exit 1` does not.
        script = 'grep -qF \'x\' f || {\n  echo "no"\n  exit 1\n}'
        assert one(script).text == 'grep -qF \'x\' f || { echo "no"; exit 1; }'

    def test_a_group_holding_only_a_message_is_still_decorative(self):
        # Fixing the join must not make every brace group look like a failure.
        script = 'grep -qF \'x\' f || {\n  echo "just saying"\n}'
        assert polarity(script) == "decorative"

    def test_an_unclosed_brace_does_not_swallow_the_script(self):
        # A `{` this miscounts -- in a heredoc, say -- must not take the rest
        # of the file with it. Unbalanced scripts get line-at-a-time reading.
        script = "cat <<'EOF'\n{\nEOF\ngrep -qF 'x' out.txt"
        assert polarity(script) == "needs-match"

    def test_braces_in_quotes_and_expansions_open_nothing(self):
        assert shell.group_delta("awk '{print $1}' f") == 0
        assert shell.group_delta('echo "${VAR}"') == 0
        assert shell.group_delta("x=$(f); echo {") == 1

    def test_if_then_at_the_top_level_is_still_its_own_line(self):
        # `_branch_polarity` walks logical lines to find the `else`, so an
        # `if` outside a brace group must not be joined to its body.
        script = "if grep -qF 'oops' out.txt; then\n  exit 1\nfi"
        assert [text for _, text in shell.logical_lines(script)] == [
            "if grep -qF 'oops' out.txt; then",
            "exit 1",
            "fi",
        ]


class TestOneLineIf:
    """An `if` must read the same whether it is written on one line or four.

    logbook#73's blindness again, one newline earlier: `_FAILING` wanted a
    `;`, `&&`, `|`, `{` or `(` in front of the `exit`, and in
    `if grep -q x f; then exit 1; fi` what is in front of it is `then`. The
    only statement in the branch was invisible, so a working negated guard --
    the shape this tool exists to judge -- read as `decorative`.
    """

    SHAPES = [
        ("then exits", "if grep -q 'y' f; then exit 1; fi", "if grep -q 'y' f; then\n  exit 1\nfi"),
        (
            "bang, then exits",
            "if ! grep -q 'y' f; then exit 1; fi",
            "if ! grep -q 'y' f; then\n  exit 1\nfi",
        ),
        (
            "then exits, else does not",
            "if grep -q 'y' f; then exit 1; else :; fi",
            "if grep -q 'y' f; then\n  exit 1\nelse\n  :\nfi",
        ),
        (
            "else exits, then does not",
            "if grep -q 'y' f; then :; else exit 1; fi",
            "if grep -q 'y' f; then\n  :\nelse\n  exit 1\nfi",
        ),
        (
            "both branches exit",
            "if grep -q 'y' f; then exit 1; else exit 2; fi",
            "if grep -q 'y' f; then\n  exit 1\nelse\n  exit 2\nfi",
        ),
        (
            "neither branch can fail",
            "if grep -q 'y' f; then echo noted; fi",
            "if grep -q 'y' f; then\n  echo noted\nfi",
        ),
    ]

    @pytest.mark.parametrize("name,inline,spread", SHAPES, ids=[s[0] for s in SHAPES])
    def test_one_line_reads_as_the_spread_form(self, name, inline, spread):
        folded = one(inline)
        opened = one(spread)
        assert (folded.polarity, folded.undecided) == (opened.polarity, opened.undecided)

    def test_the_shape_itself(self):
        # Spelled out rather than left to the comparison above, so that a
        # change making *both* forms wrong cannot pass this file.
        assert polarity("if grep -q 'y' f; then exit 1; fi") == "needs-no-match"

    def test_an_exit_is_not_credited_to_the_wrong_branch(self):
        # The dangerous half: a one-line `if` holds both branches, and
        # crediting the `exit` to the other one inverts the polarity, which
        # is worse than the `decorative` it replaced.
        assert polarity("if grep -q 'y' f; then :; else exit 1; fi") == "needs-match"

    def test_then_false_and_then_return_also_fail(self):
        assert polarity("if grep -q 'y' f; then false; fi") == "needs-no-match"
        assert polarity("if grep -q 'y' f; then return 1; fi") == "needs-no-match"


class TestFunctionBodies:
    """logbook#66: `scan` declines a function body rather than half-reading it.

    `unread.scan` announces what is declined -- see tests/test_unread.py. The
    contract here is only that nothing in a body is silently treated as
    top-level code, because whether it reaches the step depends on where the
    function is called and this module does not work that out.
    """

    def test_a_matcher_in_a_function_body_is_not_an_assertion(self):
        script = "guard() {\n  if grep -q 'y' f; then exit 1; fi\n}\nguard"
        assert shell.scan(script) == []

    def test_nor_on_a_body_line_below_the_definition(self):
        # Before this, the first body line was joined to `name() {` and the
        # rest were read as though they were top-level code.
        script = "guard() {\n  echo x\n  grep -q 'y' f\n}\nguard"
        assert shell.scan(script) == []

    def test_nor_one_the_joined_line_would_have_surfaced(self):
        # The case the skip is actually load bearing for. The body is one
        # logical line whose head word is the function's name, so most
        # matchers in there are invisible anyway -- but `pipeline_parts`
        # splits on `|`, and without the skip this one was read as a
        # top-level assertion and reported against the definition's line.
        script = 'check() {\n  printf \'%s\' "$out" | grep -q Y || exit 1\n}\ncheck'
        assert shell.scan(script) == []

    def test_a_matcher_after_the_body_is_still_read(self):
        script = "guard() {\n  grep -q 'y' f\n}\nguard\ngrep -qF 'after' out.txt"
        assert [a.pattern for a in shell.scan(script)] == ["after"]

    def test_spans_cover_both_definition_forms(self):
        assert shell.function_spans("f() {\n  :\n}") == [(1, 3, "f")]
        assert shell.function_spans("function f {\n  :\n}") == [(1, 3, "f")]
        assert shell.function_spans("f() { :; }") == [(1, 1, "f")]
        assert shell.function_spans("echo not-a-function") == []
