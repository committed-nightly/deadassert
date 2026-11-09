"""Exit codes, modes, and the example the README quotes."""

from __future__ import annotations

import json
import os

import pytest

from deadassert.cli import main

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def repo(tmp_path, *steps: str) -> str:
    body = ["on: push", "jobs:", "  j:", "    runs-on: ubuntu-latest", "    steps:"]
    for step in steps:
        body.append("      - run: |")
        for line in step.strip("\n").split("\n"):
            body.append(f"          {line}")
    directory = tmp_path / ".github" / "workflows"
    directory.mkdir(parents=True)
    (directory / "ci.yml").write_text("\n".join(body) + "\n")
    return str(tmp_path)


class TestExitCodes:
    def test_findings_exit_1(self, capsys, tmp_path):
        root = repo(tmp_path, "if grep -qF 'kind-x' out.txt; then\n  exit 1\nfi")
        code, out, _ = run(capsys, root)
        assert code == 1
        assert "unwitnessed" in out

    def test_nothing_to_report_exits_0(self, capsys, tmp_path):
        root = repo(tmp_path, "grep -qxF 'All clear.' out.txt")
        code, out, _ = run(capsys, root)
        assert code == 0
        assert "Every assertion here fails when the thing it names stops being true." in out

    def test_no_workflows_exits_2_with_nothing_on_stdout(self, capsys, tmp_path):
        code, out, err = run(capsys, str(tmp_path))
        assert code == 2
        assert out == ""
        assert "no workflow files here" in err

    def test_an_unreadable_workflow_exits_2_with_nothing_on_stdout(self, capsys, tmp_path):
        directory = tmp_path / ".github" / "workflows"
        directory.mkdir(parents=True)
        (directory / "ci.yml").write_text("name: [unclosed\n")
        code, out, err = run(capsys, str(tmp_path))
        assert code == 2
        assert out == ""
        assert "not valid YAML" in err

    def test_a_missing_against_file_exits_2(self, capsys, tmp_path):
        root = repo(tmp_path, "grep -qF 'x' out.txt")
        code, out, err = run(capsys, root, "--against", str(tmp_path / "nope.txt"))
        assert code == 2
        assert out == ""
        assert "no such file or directory" in err

    def test_an_os_error_is_a_sentence_not_a_repr(self, capsys, tmp_path):
        root = repo(tmp_path, "grep -qF 'x' out.txt")
        _, _, err = run(capsys, root, "--against", str(tmp_path / "nope.txt"))
        assert "[Errno" not in err
        assert err.startswith("deadassert: ")


class TestNothingToCheck:
    """logbook#62: a clean verdict over no assertions is not a clean verdict."""

    def test_a_workflow_with_no_assertions_does_not_claim_all_clear(self, capsys, tmp_path):
        root = repo(tmp_path, "echo building\nmake all")
        code, out, _ = run(capsys, root)
        assert code == 0
        assert "No assertions here." in out
        assert "Nothing to check." in out
        assert "Every assertion here fails" not in out


class TestNotRead:
    """logbook#69: a clean run over the greps is not a clean run."""

    DIFFS = "mytool . > got.txt\ndiff -u expected.txt got.txt"

    def test_a_repo_that_asserts_only_with_diff_no_longer_claims_nothing_to_check(
        self, capsys, tmp_path
    ):
        # The broadest false statement the tool made. `diff` is what this
        # project's own README tells people to prefer over `grep`, so a repo
        # that took the advice got `Nothing to check.` over a file full of
        # assertions.
        root = repo(tmp_path, self.DIFFS)
        code, out, _ = run(capsys, root)
        assert "No assertions here." in out
        assert "Nothing to check." not in out
        assert "What it does not read: 1 assertion in 1 of those scripts (1 diff)" in out
        assert "diff -u expected.txt got.txt" in out
        # Still not a finding, so still not a failure.
        assert code == 0

    def test_the_clean_verdict_says_what_it_did_not_read(self, capsys, tmp_path):
        root = repo(tmp_path, "grep -qxF 'All clear.' out.txt", self.DIFFS)
        code, out, _ = run(capsys, root)
        assert code == 0
        assert "Every assertion here fails when the thing it names stops being true." in out
        assert "1 assertion in 2 scripts across 1 workflow file, 0 of them negated." in out
        assert "Not counted above: 1 assertion in 1 of those scripts (1 diff)" in out

    def test_the_logbook_69_shape_is_named_in_a_run_that_has_findings(self, capsys, tmp_path):
        root = repo(
            tmp_path,
            "if grep -qF 'kind-x' out.txt; then\n  exit 1\nfi",
            "python3 -c \"\nimport json\nassert json.load(open('o.json'))['n'] > 5\n\"",
        )
        code, out, _ = run(capsys, root)
        assert code == 1
        assert "unwitnessed" in out
        assert "not read:" in out
        assert "assert json.load(open('o.json'))['n'] > 5" in out

    def test_a_repo_with_nothing_unread_says_nothing_about_it(self, capsys, tmp_path):
        root = repo(tmp_path, "grep -qxF 'All clear.' out.txt")
        _, out, _ = run(capsys, root)
        assert "not read:" not in out
        assert "Not counted above" not in out

    def test_quiet_stays_one_line_per_finding(self, capsys, tmp_path):
        # `not read:` is a coverage note, not a finding, and -q is for
        # feeding a list of findings to something else.
        root = repo(tmp_path, "if grep -qF 'kind-x' o.txt; then\n  exit 1\nfi", self.DIFFS)
        _, out, _ = run(capsys, root, "-q")
        assert out.strip().splitlines() == [".github/workflows/ci.yml:7: unwitnessed: 'kind-x'"]

    def test_json_carries_the_constructs_and_the_script_count(self, capsys, tmp_path):
        root = repo(tmp_path, "grep -qxF 'All clear.' out.txt", self.DIFFS)
        code, out, _ = run(capsys, root, "--json")
        payload = json.loads(out)
        assert code == 0
        assert payload["findings"] == []
        assert payload["unread_scripts"] == 1
        assert payload["unread"] == [
            {
                "where": "ci.yml:10",
                "kind": "diff",
                "command": "diff -u expected.txt got.txt",
            }
        ]

    def test_the_line_number_points_at_the_construct(self, capsys, tmp_path):
        root = repo(tmp_path, "echo one\necho two\ndiff -u a.txt b.txt")
        _, out, _ = run(capsys, root, "--json")
        # `run: |` on line 6, so the script's own lines start at 7.
        assert json.loads(out)["unread"][0]["where"] == "ci.yml:9"


class TestDenominator:
    """logbook#59 and logbook#57: a verdict needs what it was measured over."""

    def test_the_clean_verdict_carries_counts(self, capsys, tmp_path):
        root = repo(tmp_path, "grep -qxF 'All clear.' out.txt")
        _, out, _ = run(capsys, root)
        assert "1 assertion in 1 script across 1 workflow file, 0 of them negated." in out

    def test_the_findings_verdict_carries_counts(self, capsys, tmp_path):
        root = repo(tmp_path, "if grep -qF 'kind-x' out.txt; then\n  exit 1\nfi")
        _, out, _ = run(capsys, root)
        assert "1 assertion in 1 script across 1 workflow file, 1 of them negated." in out


class TestModes:
    def test_quiet_is_one_line_per_finding(self, capsys, tmp_path):
        root = repo(
            tmp_path,
            "if grep -qF 'kind-x' out.txt; then\n  exit 1\nfi",
            "grep -qF 'Every entry here is read.' c.txt",
        )
        code, out, _ = run(capsys, root, "-q")
        assert code == 1
        lines = out.strip().split("\n")
        assert len(lines) == 2
        assert all(line.startswith(".github/workflows/ci.yml:") for line in lines)

    def test_json_carries_the_counts_and_the_findings(self, capsys, tmp_path):
        root = repo(tmp_path, "if grep -qF 'kind-x' out.txt; then\n  exit 1\nfi")
        code, out, _ = run(capsys, root, "--json")
        assert code == 1
        payload = json.loads(out)
        assert payload["assertions"] == 1
        assert payload["negated"] == 1
        assert payload["scripts"] == 1
        finding = payload["findings"][0]
        assert finding["kind"] == "unwitnessed"
        assert finding["pattern"] == "kind-x"
        assert finding["polarity"] == "needs-no-match"
        assert finding["job"] == "j"

    def test_json_is_valid_when_there_is_nothing_to_report(self, capsys, tmp_path):
        root = repo(tmp_path, "grep -qxF 'All clear.' out.txt")
        code, out, _ = run(capsys, root, "--json")
        assert code == 0
        assert json.loads(out)["findings"] == []

    def test_a_named_file_is_read_instead_of_searching(self, capsys, tmp_path):
        root = repo(tmp_path, "if grep -qF 'kind-x' out.txt; then\n  exit 1\nfi")
        path = os.path.join(root, ".github", "workflows", "ci.yml")
        code, out, _ = run(capsys, ".", "-f", path)
        assert code == 1
        assert "unwitnessed" in out


class TestShippedExample:
    """The example in `examples/` is what the README quotes, so it is pinned.

    `logbook#59`: a dogfood step that runs the tool on a tree with none of
    its subject asserts nothing. This repository's own workflows do contain
    assertions -- that is unavoidable, since the subject is assertions -- but
    the example is the input that holds one of every kind on purpose.
    """

    def test_all_four_kinds_fire_with_their_lines(self, capsys):
        code, out, _ = run(capsys, EXAMPLES, "-q")
        assert code == 1
        assert out.strip().split("\n") == [
            ".github/workflows/ci.yml:51: unwitnessed: 'countdead warning:'",
            ".github/workflows/ci.yml:42: prefix: 'Every entry here is read.'",
            ".github/workflows/ci.yml:61: swallowed: '\"dead\": 2'",
            ".github/workflows/ci.yml:67: decorative: 'skipped'",
        ]

    def test_the_six_sound_assertions_are_not_findings(self, capsys):
        _, out, _ = run(capsys, EXAMPLES, "--json")
        payload = json.loads(out)
        assert payload["assertions"] == 7
        assert len(payload["findings"]) == 4

    def test_against_names_the_line_that_already_grew(self, capsys):
        code, out, _ = run(capsys, EXAMPLES, "--against", os.path.join(EXAMPLES, "output.txt"))
        assert code == 1
        # The detail is wrapped for the terminal, so compare on the words.
        flat = " ".join(out.split())
        assert "Every entry here is read. Read 0 entries in 1 file." in flat
        assert "already passing over a line it does not name in full" in flat

    def test_every_sound_positive_assertion_matches_the_shipped_output(self, capsys):
        """If this fails, `examples/output.txt` has drifted from the workflow."""
        _, out, _ = run(
            capsys, EXAMPLES, "--against", os.path.join(EXAMPLES, "output.txt"), "--json"
        )
        assert json.loads(out)["not_matched"] == []


class TestHelp:
    def test_help_exits_0(self, capsys):
        with pytest.raises(SystemExit) as exit_info:
            main(["--help"])
        assert exit_info.value.code == 0
