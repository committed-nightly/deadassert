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
