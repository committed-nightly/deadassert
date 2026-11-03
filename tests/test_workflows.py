"""Reading workflow files, and the line numbers that make a finding findable."""

from __future__ import annotations

import pytest

from deadassert import workflows


def write(tmp_path, body: str, name: str = "ci.yml") -> str:
    directory = tmp_path / ".github" / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(body)
    return str(tmp_path)


class TestLineNumbers:
    """A finding is one command inside a script, so `ci.yml` is not a location.

    PyYAML marks a block scalar on its `|`, with content starting the line
    after, and a plain or quoted scalar on the content itself. Getting that
    distinction wrong puts every finding in the file off by one.
    """

    def test_block_scalar_first_line_is_after_the_bar(self, tmp_path):
        root = write(
            tmp_path,
            "name: ci\n"  # 1
            "on: push\n"  # 2
            "jobs:\n"  # 3
            "  j:\n"  # 4
            "    runs-on: ubuntu-latest\n"  # 5
            "    steps:\n"  # 6
            "      - run: |\n"  # 7
            "          echo one\n"  # 8
            "          echo two\n",  # 9
        )
        script = workflows.load(root, ".github/workflows/ci.yml").scripts[0]
        assert script.first_line == 8
        assert script.line_of(1) == 8
        assert script.line_of(2) == 9

    def test_folded_scalar_is_marked_the_same_way(self, tmp_path):
        root = write(
            tmp_path,
            "on: push\njobs:\n  j:\n    steps:\n      - run: >\n          echo one\n",
        )
        script = workflows.load(root, ".github/workflows/ci.yml").scripts[0]
        assert script.first_line == 6

    def test_plain_scalar_is_on_its_own_line(self, tmp_path):
        root = write(
            tmp_path,
            "on: push\n"  # 1
            "jobs:\n"  # 2
            "  j:\n"  # 3
            "    steps:\n"  # 4
            "      - run: grep -qF 'x' out.txt\n",  # 5
        )
        script = workflows.load(root, ".github/workflows/ci.yml").scripts[0]
        assert script.first_line == 5
        assert script.line_of(1) == 5

    def test_second_step_is_not_given_the_first_step_line(self, tmp_path):
        root = write(
            tmp_path,
            "on: push\n"
            "jobs:\n"
            "  j:\n"
            "    steps:\n"
            "      - run: |\n"
            "          echo first\n"
            "      - name: second\n"
            "        run: |\n"
            "          echo second\n",
        )
        first, second = workflows.load(root, ".github/workflows/ci.yml").scripts
        assert first.first_line == 6
        assert second.first_line == 9


class TestShells:
    def test_bash_is_the_default(self, tmp_path):
        root = write(tmp_path, "on: push\njobs:\n  j:\n    steps:\n      - run: 'true'\n")
        assert workflows.load(root, ".github/workflows/ci.yml").scripts[0].shell == "bash"

    def test_windows_runner_defaults_to_pwsh_and_is_skipped(self, tmp_path):
        root = write(
            tmp_path,
            "on: push\njobs:\n  j:\n    runs-on: windows-latest\n    steps:\n      - run: 'true'\n",
        )
        workflow = workflows.load(root, ".github/workflows/ci.yml")
        assert workflow.scripts == []
        assert workflow.foreign == [("steps[0]", "pwsh")]

    def test_python_shell_is_skipped_not_read_as_shell(self, tmp_path):
        root = write(
            tmp_path,
            "on: push\njobs:\n  j:\n    steps:\n"
            "      - shell: python\n        run: |\n          print('grep -qF x')\n",
        )
        workflow = workflows.load(root, ".github/workflows/ci.yml")
        assert workflow.scripts == []
        assert workflow.foreign == [("steps[0]", "python")]

    def test_a_shell_given_as_a_command_line_is_still_bash(self, tmp_path):
        root = write(
            tmp_path,
            "on: push\njobs:\n  j:\n    steps:\n"
            "      - shell: bash -eo pipefail {0}\n        run: 'true'\n",
        )
        assert workflows.load(root, ".github/workflows/ci.yml").scripts[0].posix

    def test_job_defaults_shell_applies(self, tmp_path):
        root = write(
            tmp_path,
            "on: push\njobs:\n  j:\n    defaults:\n      run:\n        shell: pwsh\n"
            "    steps:\n      - run: 'true'\n",
        )
        assert workflows.load(root, ".github/workflows/ci.yml").scripts == []


class TestRefusals:
    def test_on_is_a_boolean_in_yaml_11_and_jobs_still_load(self, tmp_path):
        """The thing to know in every tool in this family.

        `on:` comes back from PyYAML as `True`, not `"on"`. Nothing here reads
        the trigger block, but a test pins the assumption so a future change
        that starts reading it does not quietly find nothing.
        """
        import yaml

        doc = yaml.safe_load("on: push\njobs: {}\n")
        assert True in doc and "on" not in doc

    def test_empty_file(self, tmp_path):
        root = write(tmp_path, "")
        with pytest.raises(workflows.WorkflowError, match="empty file"):
            workflows.load(root, ".github/workflows/ci.yml")

    def test_not_yaml(self, tmp_path):
        root = write(tmp_path, "name: [unclosed\n")
        with pytest.raises(workflows.WorkflowError, match="not valid YAML"):
            workflows.load(root, ".github/workflows/ci.yml")

    def test_top_level_is_a_list(self, tmp_path):
        root = write(tmp_path, "- one\n- two\n")
        with pytest.raises(workflows.WorkflowError, match="not a mapping"):
            workflows.load(root, ".github/workflows/ci.yml")

    def test_missing_file(self, tmp_path):
        with pytest.raises(workflows.WorkflowError):
            workflows.load(str(tmp_path), ".github/workflows/nope.yml")

    def test_find_workflows_sorted_and_filtered(self, tmp_path):
        root = write(tmp_path, "on: push\njobs: {}\n", name="b.yml")
        write(tmp_path, "on: push\njobs: {}\n", name="a.yaml")
        write(tmp_path, "notes\n", name="README.md")
        assert workflows.find_workflows(root) == [
            ".github/workflows/a.yaml",
            ".github/workflows/b.yml",
        ]

    def test_find_workflows_on_a_tree_with_no_workflow_dir(self, tmp_path):
        assert workflows.find_workflows(str(tmp_path)) == []
