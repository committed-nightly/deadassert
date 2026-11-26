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

    def test_else_branch_holding_only_a_bare_grep_is_not_decorative(self):
        """The `whichrc` step this rule was written for.

        It must not read `decorative` -- the else branch really can fail the
        step. It must not read `needs-match` either: that is logbook#78, and
        `TestEitherOr` below is where the rest of this shape is pinned.
        """
        script = (
            "if grep -q '^ ssh *nothing$' rows.txt; then\n"
            "  echo 'this bash does not source it'\n"
            "else\n"
            "  grep -q '^ ssh *\\.bashrc$' rows.txt\n"
            "fi"
        )
        condition = shell.scan(script)[0]
        assert condition.polarity != "decorative"
        assert condition.polarity == "undecided"

    def test_else_branch_with_an_unconditional_exit_still_needs_a_match(self):
        """The control for logbook#78, and the whole discrimination.

        Nothing gets a say in whether this `else` fails, so the condition is
        the entire verdict and `needs-match` is exactly right. Verified
        against `bash -e`: with the pattern absent this script exits 1.
        """
        script = (
            "if grep -q '^ ssh *nothing$' rows.txt; then\n"
            "  echo 'this bash does not source it'\n"
            "else\n"
            "  echo 'no row' >&2\n"
            "  exit 1\n"
            "fi"
        )
        assert polarity(script) == "needs-match"


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
        script = "echo hi | grep -qxF 'x' || {\n    echo \"no\"\n    exit 1\n  }"
        assert polarity(script) == "needs-match"

    def test_single_line_brace_group_still_is(self):
        assert polarity("echo hi | grep -qxF 'x' || { echo no; exit 1; }") == "needs-match"

    def test_multi_line_and_brace_group_needs_no_match(self):
        script = "grep -qF 'oops' out.txt && {\n    echo bad\n    exit 1\n  }"
        assert polarity(script) == "needs-no-match"

    def test_newlines_in_the_group_become_separators(self):
        # The bug was not the joining, it was joining with a space: `_FAILING`
        # wants an `exit` that starts a command, and `"no" exit 1` does not.
        script = "grep -qF 'x' f || {\n  echo \"no\"\n  exit 1\n}"
        assert one(script).text == "grep -qF 'x' f || { echo \"no\"; exit 1; }"

    def test_a_group_holding_only_a_message_is_still_decorative(self):
        # Fixing the join must not make every brace group look like a failure.
        script = "grep -qF 'x' f || {\n  echo \"just saying\"\n}"
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


class TestCommandParts:
    r"""The splitter behind logbook#81, and the one `;` it must not split on.

    `command_parts` is only interesting because of what it refuses to do.
    Every case below is a `;` that looks like a command separator and is
    not, and each one has a wrong answer attached to it: a `;` inside
    `|| { ... }` yields a bare `exit 1`, which would read as a failure no
    matcher gets a say in and put logbook#78's shape back on a live
    polarity.
    """

    def test_a_list_splits_on_its_separators(self):
        assert shell.command_parts("grep -q 'B' f; exit 1") == ["grep -q 'B' f", "exit 1"]

    def test_a_brace_group_is_one_command(self):
        # The case with teeth: `exit 1` here is the matcher's verdict.
        text = "grep -q 'B' f || { echo no; exit 1; }"
        assert shell.command_parts(text) == [text]

    def test_a_nested_compound_is_one_command(self):
        text = "if grep -q 'B' f; then exit 1; fi"
        assert shell.command_parts(text) == [text]
        assert shell.command_parts("while grep -q x f; do sleep 1; done") == [
            "while grep -q x f; do sleep 1; done"
        ]
        assert shell.command_parts("case $x in a) echo a;; esac; exit 1") == [
            "case $x in a) echo a;; esac",
            "exit 1",
        ]

    def test_a_keyword_in_argument_position_opens_and_closes_nothing(self):
        # The direction with teeth is the *opening* one: a stray `fi` only
        # pushes the depth negative, which a `<= 0` test forgives, but a
        # stray `if` hides the separator after it and the part goes back to
        # being read as one blob.
        assert shell.command_parts("echo if; exit 1") == ["echo if", "exit 1"]
        assert shell.command_parts("echo fi; exit 1") == ["echo fi", "exit 1"]

    def test_a_separator_in_quotes_or_an_expansion_is_not_one(self):
        assert shell.command_parts("grep -q 'a;b' f; exit 1") == ["grep -q 'a;b' f", "exit 1"]
        assert shell.command_parts("echo $(date; true); exit 1") == ["echo $(date; true)", "exit 1"]
        assert shell.command_parts("echo ${x:-a;b}; exit 1") == ["echo ${x:-a;b}", "exit 1"]
        assert shell.command_parts("echo `date; true`; exit 1") == ["echo `date; true`", "exit 1"]
        assert shell.command_parts("( grep -q B f; exit 1 )") == ["( grep -q B f; exit 1 )"]

    def test_a_leading_keyword_comes_off(self):
        # Without this the branch body of a one-line `if` is a command called
        # `then`, which is no matcher and no `exit` -- the shape that read
        # `decorative`.
        assert shell.command_parts("; then grep -q 'C' f; fi") == ["grep -q 'C' f", "fi"]
        assert shell._bare_assertion(shell.command_parts("; then grep -q 'C' f")[0])

    def test_the_separators_the_shell_uses_that_this_does_not(self):
        # `&&`, `||` and `&` all separate commands. The first two make what
        # follows conditional on a verdict, which is the distinction being
        # read; a lone `&` is left with them because not splitting declines
        # rather than over-claims.
        assert shell.command_parts("grep -q B f && exit 1") == ["grep -q B f && exit 1"]
        assert shell.command_parts("grep -q B f & exit 1") == ["grep -q B f & exit 1"]


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
        script = "check() {\n  printf '%s' \"$out\" | grep -q Y || exit 1\n}\ncheck"
        assert shell.scan(script) == []

    def test_a_matcher_after_the_body_is_still_read(self):
        script = "guard() {\n  grep -q 'y' f\n}\nguard\ngrep -qF 'after' out.txt"
        assert [a.pattern for a in shell.scan(script)] == ["after"]

    def test_spans_cover_both_definition_forms(self):
        assert shell.function_spans("f() {\n  :\n}") == [(1, 3, "f")]
        assert shell.function_spans("function f {\n  :\n}") == [(1, 3, "f")]
        assert shell.function_spans("f() { :; }") == [(1, 1, "f")]
        assert shell.function_spans("echo not-a-function") == []


class TestEitherOr:
    r"""A branch that asserts, so the condition above it is not the verdict.

    logbook#78 for the `else` branch, logbook#89 for the `then` branch, and
    logbook#81 for the spelling that put both on one line. The condition
    decides the step only when nothing in the branch it chooses gets a say,
    and a branch body is a command list: `grep -q B f` followed by `exit 1`
    fails whatever `B` does, while `grep -q B f || { exit 1; }` fails only
    when `B` misses.

    Every expectation here was taken from running the shape under `bash -e`
    for each combination of which patterns `f` holds, never from reading the
    code:

                                       none A B C AB AC BC ABC
        then, bare matcher                0 1 0 0  1  0  0   0
        then, matcher then exit           0 1 0 .  1  1  .   1
        else, bare matcher                1 0 1 0  0  0  0   0
        else, matcher then exit           1 0 1 1  0  0  1   0

    Row 1 is why `needs-no-match` on a `then`-branch either-or is a false
    label: `A` present with `C` present exits 0. Row 2 is the control one
    `;` away from it, where the `exit` is nobody's verdict and the condition
    really is `needs-no-match`.
    """

    script = "if grep -q 'A' out.txt; then\n  echo 'a note'\nelse\n  grep -q 'B' out.txt\nfi"

    def test_the_condition_is_undecided_with_a_reason(self):
        condition = shell.scan(self.script)[0]
        assert condition.polarity == "undecided"
        assert "either-or" in (condition.undecided or "")

    def test_the_else_branch_matcher_is_still_read(self):
        """Only the condition's label was wrong; the else branch is unchanged.

        A matcher in a branch body is live if that branch runs, which this
        module does not work out for any branch -- not a change made here.
        """
        found = shell.scan(self.script)
        assert [a.pattern for a in found] == ["A", "B"]
        assert found[1].polarity == "needs-match"

    def test_an_exit_reached_through_the_else_matcher_is_still_either_or(self):
        """`|| { exit 1; }` on the else matcher does not make it unconditional.

        The `exit` is that matcher's verdict. Reading it as an unconditional
        failure would put this shape back on `needs-match`.
        """
        script = (
            "if grep -q 'A' out.txt; then\n"
            "  echo 'a note'\n"
            "else\n"
            "  grep -q 'B' out.txt || { echo 'neither' >&2; exit 1; }\n"
            "fi"
        )
        assert shell.scan(script)[0].polarity == "undecided"

    def test_a_matcher_and_an_exit_on_one_line_is_the_verdict(self):
        """logbook#81: the two spellings of one `else` branch now agree.

        `else grep -q B f; exit 1` fails whatever `B` does -- exit 1 with
        `B` present as well as absent -- so the condition is the whole
        verdict and `needs-match` is right. The multi-line spelling always
        got this, because `note` is called once per logical line and the
        `exit` arrived on its own; written on one line both commands reached
        it as a single part, no part was free of a matcher, and the condition
        was declined.
        """
        script = "if grep -q 'A' f; then echo note; else grep -q 'B' f; exit 1; fi"
        assert shell.scan(script)[0].polarity == "needs-match"

    def test_the_multi_line_spelling_of_that_branch_agrees(self):
        """The control for the test above: the spelling must not matter."""
        script = "if grep -q 'A' f; then\n  echo note\nelse\n  grep -q 'B' f\n  exit 1\nfi"
        assert shell.scan(script)[0].polarity == "needs-match"

    def test_a_then_branch_that_asserts_declines_the_condition(self):
        """logbook#89, the mirror of logbook#78 and the dangerous direction.

        `alone` was tracked for both branches and read only for the `else`,
        so this reported `needs-no-match` -- a *live* polarity, counted,
        able to stand as a witness, and feeding "every assertion here fails
        when the thing it names stops being true". Under `bash -e` the step
        exits 1 only when `A` matches and `B` misses: `A` present with `B`
        present exits 0, so `B` gets a say and the condition is not the
        verdict.
        """
        script = "if grep -q 'A' out.txt; then\n  grep -q 'B' out.txt\nfi"
        condition = shell.scan(script)[0]
        assert condition.polarity == "undecided"
        assert "either-or" in (condition.undecided or "")
        assert "`then` branch" in (condition.undecided or "")

    def test_a_then_branch_matcher_is_still_read(self):
        """The mirror of `test_the_else_branch_matcher_is_still_read`.

        Only the condition's label was wrong. The matcher in the branch body
        is as load bearing as it ever was.
        """
        found = shell.scan("if grep -q 'A' out.txt; then\n  grep -q 'B' out.txt\nfi")
        assert [a.pattern for a in found] == ["A", "B"]
        assert found[1].polarity == "needs-match"

    def test_a_then_branch_that_also_exits_is_the_verdict(self):
        """The control one `;` away, and the reason this needs a splitter.

        Row 2 of the class docstring: the `exit` is nobody's verdict, so the
        step fails exactly when `A` matches. Declining this as well would be
        the easy over-correction -- it is the shape of every `if grep -q bad
        out.txt; then echo ...; exit 1; fi` guard in the organisation.
        """
        script = "if grep -q 'A' f; then\n  grep -q 'B' f\n  exit 1\nfi"
        assert shell.scan(script)[0].polarity == "needs-no-match"

    def test_a_one_line_then_branch_that_asserts_is_not_decorative(self):
        """The same shape on one line read `decorative` -- a finding.

        Worse than logbook#89's wrong label, and the same root: the body
        arrived as `; then grep -q 'C' f; fi`, where `command_head` sees a
        command called `then` and no matcher at all. So the branch looked
        empty, neither branch could fail, and the tool reported a live
        assertion as dead config.
        """
        found = shell.scan("if grep -q 'A' f; then grep -q 'C' f; fi")[0]
        assert found.polarity == "undecided"
        assert found.polarity != "decorative"

    def test_a_one_line_then_branch_that_exits_is_still_the_verdict(self):
        """And its control: the splitter must not decline this one."""
        found = shell.scan("if grep -q 'A' f; then grep -q 'B' f; exit 1; fi")[0]
        assert found.polarity == "needs-no-match"

    def test_a_matcher_in_a_nested_condition_is_still_over_decided(self):
        """Pinned, not fixed: the gap this change leaves, filed as logbook#90.

        `if grep -q 'B' f; then exit 1; fi` is one command, so the splitter
        correctly keeps it whole -- and `_bare_assertion` asks
        `command_head` about it, which answers "a command called `if`". The
        matcher in the nested condition is invisible, the `exit` looks
        unconditional, and the outer condition reads `needs-no-match`. Under
        `bash -e` the step exits 1 only when both patterns match, so that is
        logbook#89's claim one level deeper. Both spellings agree on it now,
        which they did not before.
        """
        nested = "if grep -q 'B' f; then exit 1; fi"
        for script in (
            f"if grep -q 'A' f; then\n  {nested}\nfi",
            f"if grep -q 'A' f; then {nested}; fi",
        ):
            assert shell.scan(script)[0].polarity == "needs-no-match", script
        # The `else` mirror, wrong the same way: the step exits 1 only when
        # `A` misses and `B` matches, and `needs-match` is just as live.
        for script in (
            f"if grep -q 'A' f; then\n  echo x\nelse\n  {nested}\nfi",
            f"if grep -q 'A' f; then echo x; else {nested}; fi",
        ):
            assert shell.scan(script)[0].polarity == "needs-match", script


class TestElifConditions:
    r"""A matcher in an `elif` condition, and what the chain does to the one above it.

    Before logbook#82 the first of these read `decorative` -- a finding kind,
    so the tool reported a load-bearing assertion as dead. `_branch_polarity`
    was called with the `elif`'s own index and `depth` is counted from the
    tokens on each line, where the `if` in `elif` is preceded by `el` rather
    than a separator. It was never counted, `if depth <= 0: break` ended the
    walk on the first line, and the function fell through to its last line: a
    verdict over no branches at all.

    Every expectation below was taken from running the shape under `bash -e`
    for each combination of which patterns are present in `f`, never from
    reading the code. The exit statuses, with the file holding the patterns
    named in each column:

                               none A B C AB BC AC ABC
        1 elif-then-exit          0 0 1 .  0  1  .   0
        2 elif-harmless           0 0 0 .  0  0  .   0
        3 elif-else-exit          1 0 0 .  0  0  .   0
        4 elif-bang-then-exit     1 0 0 .  0  0  .   0
        6 nested-if-in-elif       0 0 1 0  0  1  0   0
        8 elif-chain-three-way    0 0 1 1  0  1  0   0

    Polarity is what the condition's status does to the step *when it is
    reached*, which is the convention the rest of the module already uses: a
    matcher nested inside another `if` is read the same way, and an `elif` is
    gated by the conditions above it exactly as nesting gates that one.
    """

    def test_an_elif_condition_with_a_failing_then_needs_no_match(self):
        """logbook#82's repro. This read `decorative` before.

        Shape 1: the step exits 1 exactly when `B` is present and `A` is not,
        so `B`'s own `then` branch is the verdict and the polarity is the way
        round that says "this rots silently if the output stops being
        produced".
        """
        script = "if grep -q 'A' f; then\n  echo a\nelif grep -q 'B' f; then\n  exit 1\nfi"
        found = {a.pattern: a for a in shell.scan(script)}
        assert found["B"].polarity == "needs-no-match"
        assert found["B"].polarity != "decorative"

    def test_the_condition_above_it_is_gated_by_the_elif(self):
        """The other half of logbook#82, and the half with a live polarity.

        For `A`, the `exit 1` on the else-path sits behind `B`: shape 1 exits
        0 with `A` absent and `B` absent. `needs-match` -- "the step fails
        when the pattern is absent" -- is a claim the step does not make, and
        it is a *live* polarity, so it counted and could stand as a witness.
        This is logbook#78 with the second matcher one line further away.
        """
        script = "if grep -q 'A' f; then\n  echo a\nelif grep -q 'B' f; then\n  exit 1\nfi"
        found = {a.pattern: a for a in shell.scan(script)}
        assert found["A"].polarity == "undecided"
        assert "either-or" in (found["A"].undecided or "")

    def test_an_elif_whose_branches_do_nothing_is_still_decorative(self):
        """The control. `decorative` must stay reachable through an `elif`.

        Shape 2 exits 0 for every combination, so both conditions really are
        decorative. A fix that declined every `elif` would pass the two tests
        above and lose the finding this tool exists to report.
        """
        script = "if grep -q 'A' f; then\n  echo a\nelif grep -q 'B' f; then\n  echo b\nfi"
        assert [a.polarity for a in shell.scan(script)] == ["decorative", "decorative"]

    def test_an_elif_whose_else_exits_unconditionally_needs_a_match(self):
        """Shape 3, where nothing gets a say in the `else`.

        Reached with `A` absent, the step exits 1 unless `B` matches, so the
        `elif` condition is the whole verdict from there.
        """
        script = (
            "if grep -q 'A' f; then\n"
            "  echo a\n"
            "elif grep -q 'B' f; then\n"
            "  echo b\n"
            "else\n"
            "  exit 1\n"
            "fi"
        )
        found = {a.pattern: a for a in shell.scan(script)}
        assert found["B"].polarity == "needs-match"

    def test_a_negated_elif_condition_is_the_other_way_round(self):
        """Shape 4. The `!` is handled in `scan`, but on a polarity this
        function now actually produces -- before, it inverted `decorative`,
        which is to say it did nothing."""
        script = "if grep -q 'A' f; then\n  echo a\nelif ! grep -q 'B' f; then\n  exit 1\nfi"
        found = {a.pattern: a for a in shell.scan(script)}
        assert found["B"].polarity == "needs-match"

    def test_a_nested_if_in_the_elif_body_does_not_end_the_walk(self):
        """Shape 6. The seeded depth has to stay a real depth counter.

        An `if`/`fi` pair inside the body returns to the same depth it
        started at, so the walk must still be running when `exit 1` arrives
        two lines later.
        """
        script = (
            "if grep -q 'A' f; then\n"
            "  echo a\n"
            "elif grep -q 'B' f; then\n"
            "  if true; then\n"
            "    echo inner\n"
            "  fi\n"
            "  exit 1\n"
            "fi"
        )
        found = {a.pattern: a for a in shell.scan(script)}
        assert found["B"].polarity == "needs-no-match"

    def test_a_three_way_chain_declines_the_middle_condition(self):
        """Shape 8, and the reason the gating is not simply ignored.

        With `A` absent the step exits 1 when `B` matches, and also when `B`
        misses and `C` matches -- both branches of `B`'s walk can fail, which
        is the sentence this already had. `C`, last in the chain, has nothing
        below it and is the verdict where it is reached.
        """
        script = (
            "if grep -q 'A' f; then\n"
            "  echo a\n"
            "elif grep -q 'B' f; then\n"
            "  exit 1\n"
            "elif grep -q 'C' f; then\n"
            "  exit 1\n"
            "fi"
        )
        found = {a.pattern: a for a in shell.scan(script)}
        assert found["B"].polarity == "undecided"
        assert found["B"].undecided == "either branch of the `if` can fail the step"
        assert found["C"].polarity == "needs-no-match"


class TestAVerdictOverNoBranches:
    r"""`decorative` must not be reachable from a walk that read nothing.

    This is logbook#77's lesson one function up, made an invariant: a walk
    that never got inside the block has not established that neither branch
    can fail, and `decorative` says exactly that. It is what would have made
    logbook#82 announce itself in `not decided:` instead of answering wrongly.

    It is unreachable through `scan` today, and deliberately kept anyway.
    `scan` only calls `_branch_polarity` on a line matching `^(?:el)?if\s+`:
    an `elif` seeds the depth, and an `if` is matched by the depth regex on
    its own first line. So the guard is tested by calling the function
    directly, which is the only honest way to show the sentence can print.
    """

    def test_a_walk_that_never_entered_the_block_declines(self):
        polarity, why = shell._branch_polarity([(1, "while grep -q 'x' f")], 0)
        assert polarity == "undecided"
        assert why == "the `if` structure could not be read, so no branch was examined"

    def test_a_one_line_if_still_enters_its_block(self):
        """The guard's own near-miss, caught by `TestOneLineIf`.

        A one-line `if ... fi` opens and closes the block on one line, so a
        depth read after the line says the walk was never inside it. Asked
        per token instead. This shape is live -- the org writes it -- so the
        first version of the guard would have turned a real `decorative` into
        a decline.
        """
        found = one("if grep -q 'y' f; then echo noted; fi")
        assert found.polarity == "decorative"
        assert found.undecided is None


class TestUndecidedReasonsAreReachable:
    """Every sentence `scan` builds for a shape it cannot decide must print.

    Three of them could not. Two were attached to an assertion that was then
    labelled `needs-match` -- live, counted, and usable as a witness -- and
    `analyse` only reads `undecided` when the polarity says so. The third
    (logbook#77) was thrown away with the loop variable.
    """

    def test_a_loop_condition_is_reported(self):
        found = one("while grep -q 'y' f; do sleep 1; done")
        assert found.polarity == "undecided"
        assert found.undecided == "the matcher is a loop condition, not a verdict"
        assert found.pattern == "y"

    @pytest.mark.parametrize(
        "script",
        [
            "while grep -q 'y' f; do exit 1; done",
            "until grep -q 'y' f; do sleep 1; done",
            "while ! grep -q 'y' f; do sleep 1; done",
            "while grep -q 'y' f\ndo\n  sleep 1\ndone",
        ],
    )
    def test_every_loop_form_reaches_it(self, script):
        assert polarity(script) == "undecided"

    def test_a_loop_over_read_is_not_a_matcher_at_all(self):
        """`while read -r f; do grep -q x "$f"; done`.

        The org's only `while` is this shape. The condition holds no matcher,
        and the matcher in the body is live under `set -e` -- so the loop
        keyword must not drag the body into `not decided:`.
        """
        found = shell.scan('while read -r f; do\n  grep -q "x" "$f"\ndone')
        assert [a.polarity for a in found] == ["needs-match"]

    def test_a_producing_grep_in_a_loop_condition_is_still_not_an_assertion(self):
        assert shell.scan("while grep -c 'y' f; do sleep 1; done") == []

    def test_both_branches_able_to_fail_is_reported(self):
        found = one("if grep -q 'A' f; then\n  exit 1\nelse\n  echo x\n  exit 1\nfi")
        assert found.polarity == "undecided"
        assert found.undecided == "either branch of the `if` can fail the step"

    def test_an_if_with_no_fi_is_reported(self):
        found = one("if grep -q 'A' f; then\n  exit 1\n")
        assert found.polarity == "undecided"
        assert found.undecided == "the `if` has no matching `fi` in this script"
