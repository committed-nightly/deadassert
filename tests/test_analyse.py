"""Witnessing, the sentence rule, and what `--against` decides."""

from __future__ import annotations

import pytest

from deadassert import analyse


def repo(tmp_path, *steps: str) -> str:
    """A repository whose single job runs each of `steps` as its own script."""
    body = ["on: push", "jobs:", "  dogfood:", "    runs-on: ubuntu-latest", "    steps:"]
    for step in steps:
        body.append("      - run: |")
        for line in step.strip("\n").split("\n"):
            body.append(f"          {line}")
    directory = tmp_path / ".github" / "workflows"
    directory.mkdir(parents=True)
    (directory / "ci.yml").write_text("\n".join(body) + "\n")
    return str(tmp_path)


def kinds(result) -> list[str]:
    return [finding.kind for finding in result.findings]


def patterns(result, kind: str) -> list[str]:
    return [f.assertion.pattern for f in result.findings if f.kind == kind]


class TestUnwitnessed:
    def test_a_negated_assertion_with_no_witness_is_a_finding(self, tmp_path):
        root = repo(tmp_path, "if grep -qF 'merge-upstream' out.txt; then\n  exit 1\nfi")
        result = analyse.analyse(root)
        assert kinds(result) == ["unwitnessed"]

    def test_a_positive_assertion_on_the_same_pattern_witnesses_it(self, tmp_path):
        root = repo(
            tmp_path,
            "grep -qF 'merge-upstream' expected.txt",
            "if grep -qF 'merge-upstream' out.txt; then\n  exit 1\nfi",
        )
        assert analyse.analyse(root).findings == []

    def test_a_longer_positive_pattern_containing_it_witnesses_it(self, tmp_path):
        """Asserting a longer string proves the shorter one appears."""
        root = repo(
            tmp_path,
            "grep -qF 'reported merge-upstream twice' expected.txt",
            "if grep -qF 'merge-upstream' out.txt; then\n  exit 1\nfi",
        )
        assert analyse.analyse(root).findings == []

    def test_a_shorter_positive_pattern_does_not_witness_it(self, tmp_path):
        """Containment counts one way only.

        `grep -qF 'checked'` passing says nothing about `not checked:` ever
        being printed, so it cannot stand as the proof that it can be.
        """
        root = repo(
            tmp_path,
            "grep -qF 'checked' expected.txt",
            "if grep -qF 'not checked:' out.txt; then\n  exit 1\nfi",
        )
        assert kinds(analyse.analyse(root)) == ["unwitnessed"]

    def test_a_witness_in_another_job_still_counts(self, tmp_path):
        directory = tmp_path / ".github" / "workflows"
        directory.mkdir(parents=True)
        (directory / "ci.yml").write_text(
            "on: push\n"
            "jobs:\n"
            "  a:\n"
            "    steps:\n"
            "      - run: grep -qF 'kind-x' expected.txt\n"
            "  b:\n"
            "    steps:\n"
            "      - run: |\n"
            "          if grep -qF 'kind-x' out.txt; then\n"
            "            exit 1\n"
            "          fi\n"
        )
        assert analyse.analyse(str(tmp_path)).findings == []

    def test_a_negated_assertion_is_counted_in_the_denominator(self, tmp_path):
        root = repo(tmp_path, "if grep -qF 'x' out.txt; then\n  exit 1\nfi")
        assert analyse.analyse(root).negated == 1


class TestSentenceRule:
    """Every clause of this exists because a wider version fired on a
    deliberate substring match in this organisation's real workflows."""

    @pytest.mark.parametrize(
        "pattern",
        [
            "Every instruction has an effect on this build.",
            "4 instructions with no effect on this build.",
            "Found 3 declarations git never acts on, across 3 submodules.",
            "10 lines here have no effect on what logrotate does. Read 5 rules in 6 files.",
        ],
    )
    def test_whole_summary_lines_are_findings(self, tmp_path, pattern):
        root = repo(tmp_path, f"grep -qF '{pattern}' out.txt")
        assert kinds(analyse.analyse(root)) == ["prefix"]

    @pytest.mark.parametrize(
        "pattern,why",
        [
            (".dockerignore line 1 (.git)", "ends with a bracket, so it is a column"),
            ("submodules. Read 3 gitlinks and 3 entries.", "starts mid-sentence"),
            ("Nothing to check.", "three words, and the tail of a longer sentence"),
            ("merge-upstream", "a finding kind, not a line"),
            ("unbuilt: ", "a prefix, matched as one on purpose"),
            ("^not checked:", "already anchored by hand"),
        ],
    )
    def test_deliberate_substring_matches_are_not_findings(self, tmp_path, pattern, why):
        root = repo(tmp_path, f"grep -qF {pattern!r} out.txt")
        assert analyse.analyse(root).findings == [], why

    def test_an_anchored_assertion_is_never_a_prefix_finding(self, tmp_path):
        root = repo(tmp_path, "grep -qxF 'Every instruction has an effect on this build.' o")
        assert analyse.analyse(root).findings == []

    def test_a_regex_assertion_is_not_a_prefix_finding(self, tmp_path):
        # Without -F the pattern is a regex and `-x` is not the fix for it.
        root = repo(tmp_path, "grep -q 'Every instruction has an effect on this build.' o")
        assert analyse.analyse(root).findings == []


class TestAgainst:
    """Given real output, the question stops being about intent."""

    def output(self, tmp_path, *lines: str) -> str:
        path = tmp_path / "output.txt"
        path.write_text("\n".join(lines) + "\n")
        return str(path)

    def test_a_pattern_that_is_a_whole_line_is_a_finding(self, tmp_path):
        root = repo(tmp_path, "grep -qF 'All clear.' out.txt")
        against = self.output(tmp_path, "All clear.")
        result = analyse.analyse(root, against=against)
        assert kinds(result) == ["prefix"]
        assert "whole line of output.txt" in result.findings[0].detail

    def test_a_short_pattern_that_is_a_whole_line_is_still_a_finding(self, tmp_path):
        """The output has settled what the heuristic could only guess.

        `'All clear.'` is three words and would not pass the sentence rule,
        but it provably *is* a whole line here, so matching it without -x is
        latently wrong whatever it looks like.
        """
        root = repo(tmp_path, "grep -qF 'ok' out.txt")
        against = self.output(tmp_path, "ok")
        assert kinds(analyse.analyse(root, against=against)) == ["prefix"]

    def test_a_pattern_that_is_the_opening_of_a_longer_line_is_a_finding(self, tmp_path):
        """The append already happened and the assertion did not notice."""
        root = repo(tmp_path, "grep -qF 'Every rule here rotates something.' out.txt")
        against = self.output(tmp_path, "Every rule here rotates something. Read 1 rule.")
        result = analyse.analyse(root, against=against)
        assert kinds(result) == ["prefix"]
        assert "already passing over a line it does not name in full" in (result.findings[0].detail)
        assert "Read 1 rule." in result.findings[0].detail

    def test_a_pattern_inside_a_line_is_not_a_finding(self, tmp_path):
        """What `--against` is for: taking findings away."""
        root = repo(tmp_path, "grep -qF 'Read 3 rules in 2 files.' out.txt")
        against = self.output(tmp_path, "Found 1 dead rule. Read 3 rules in 2 files.")
        assert analyse.analyse(root, against=against).findings == []

    def test_against_overrides_the_heuristic_both_ways(self, tmp_path):
        """The same assertion, a finding without the output and not with it."""
        root = repo(tmp_path, "grep -qF 'Found 2 dead entries, across 3 files.' out.txt")
        assert kinds(analyse.analyse(root)) == ["prefix"]
        against = self.output(tmp_path, "note: Found 2 dead entries, across 3 files.")
        assert analyse.analyse(root, against=against).findings == []

    def test_a_positive_pattern_matching_nothing_is_listed_not_found(self, tmp_path):
        root = repo(tmp_path, "grep -qF 'never printed' out.txt")
        against = self.output(tmp_path, "something else")
        result = analyse.analyse(root, against=against)
        assert result.findings == []
        assert result.absent == [("ci.yml:7", "never printed")]

    def test_an_anchored_pattern_is_matched_as_a_whole_line(self, tmp_path):
        root = repo(tmp_path, "grep -qxF 'All clear.' out.txt")
        against = self.output(tmp_path, "note: All clear.")
        # -x means the substring hit does not count, so this one is absent.
        assert analyse.analyse(root, against=against).absent == [("ci.yml:7", "All clear.")]


class TestExpansions:
    """A pattern the shell fills in is not a pattern this tool can judge."""

    @pytest.mark.parametrize(
        "pattern", ['"$clean"', '"${msg}"', '"$(cat expected.txt)"', '"`date`"']
    )
    def test_expansions_are_undecided_not_judged(self, tmp_path, pattern):
        root = repo(tmp_path, f"if grep -qF {pattern} out.txt; then\n  exit 1\nfi")
        result = analyse.analyse(root)
        assert result.findings == []
        assert len(result.undecided) == 1
        assert "shell expansion" in result.undecided[0][1]

    def test_an_escaped_dollar_is_a_literal_and_is_judged(self, tmp_path):
        root = repo(tmp_path, "grep -qF 'costs \\$5 to run.' out.txt")
        assert analyse.analyse(root).undecided == []

    def test_an_expansion_is_not_counted_in_the_denominator(self, tmp_path):
        root = repo(tmp_path, 'grep -qF "$msg" out.txt')
        assert analyse.analyse(root).assertions == []


class TestCannotFail:
    @pytest.mark.parametrize(
        "step,kind",
        [
            ("grep -qF 'x' out.txt || true", "swallowed"),
            ("grep -qF 'x' out.txt | tee -a log", "masked"),
            ("if grep -qF 'x' out.txt; then\n  echo yes\nelse\n  echo no\nfi", "decorative"),
        ],
    )
    def test_each_shape(self, tmp_path, step, kind):
        assert kinds(analyse.analyse(repo(tmp_path, step))) == [kind]


class TestNothingToCheck:
    """logbook#62: an all-clear over nothing is not an all-clear."""

    def test_a_workflow_with_no_assertions_says_so(self, tmp_path):
        root = repo(tmp_path, "echo hello\nmake build")
        result = analyse.analyse(root)
        assert result.nothing_to_check
        assert result.findings == []

    def test_a_workflow_with_a_sound_assertion_is_not_nothing_to_check(self, tmp_path):
        root = repo(tmp_path, "grep -qxF 'All clear.' out.txt")
        result = analyse.analyse(root)
        assert not result.nothing_to_check
        assert result.findings == []


class TestRefusals:
    def test_a_tree_with_no_workflows_raises(self, tmp_path):
        with pytest.raises(analyse.CorpusError, match="no workflow files here"):
            analyse.analyse(str(tmp_path))

    def test_an_unreadable_workflow_is_recorded_not_raised(self, tmp_path):
        directory = tmp_path / ".github" / "workflows"
        directory.mkdir(parents=True)
        (directory / "ci.yml").write_text("name: [unclosed\n")
        result = analyse.analyse(str(tmp_path))
        assert result.files == []
        assert len(result.unreadable) == 1

    def test_a_named_file_that_is_not_there(self, tmp_path):
        with pytest.raises(analyse.CorpusError, match="not a file"):
            analyse.analyse(".", paths=[str(tmp_path / "nope.yml")])
