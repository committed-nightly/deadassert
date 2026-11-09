"""What counts as an assertion this tool does not read, and what does not.

Every negative test in here is a false positive the first version produced or
would have produced. A count that is wrong in the generous direction is worse
than no count at all: the whole point of `logbook#69` is that the number be
trustworthy enough that a non-zero one sends you to look.
"""

from __future__ import annotations

from deadassert import unread


def kinds(script: str) -> list[str]:
    return [item.kind for item in unread.scan(script)]


def one(script: str) -> unread.Unread:
    found = unread.scan(script)
    assert len(found) == 1, f"expected one construct, got {found}"
    return found[0]


class TestPythonAsserts:
    def test_the_logbook_69_instance(self):
        # Verbatim from stockbox's dogfood job, which is what this exists for:
        # an assertion whose truth depends on the runner image, in the same
        # repository where deadassert reported that defect in a grep, and
        # invisible to it.
        script = """
stockbox roots -c loose.toml --json > loose.json
python3 -c "
import json
tight = json.load(open('trees.json'))['shapes'][0]['candidates']
loose = json.load(open('loose.json'))['shapes'][0]['candidates']
assert loose > tight * 5, (loose, tight)
"
"""
        found = one(script)
        assert found.kind == "assert"
        assert found.text == "assert loose > tight * 5, (loose, tight)"
        assert found.line == 7

    def test_a_heredoc_body(self):
        script = """
set -euo pipefail
python - <<'PY'
import json
d = json.load(open("out.json"))
assert d["assertions"] >= 2, f"only {d['assertions']} seen"
assert d["findings"] == [], d["findings"]
PY
"""
        assert kinds(script) == ["assert", "assert"]

    def test_an_indented_assert_inside_a_function(self):
        assert kinds("python3 - <<'PY'\ndef check(d):\n    assert d, d\ncheck({})\nPY") == ["assert"]

    def test_a_one_liner(self):
        assert kinds("python3 -c 'assert 1 == 1'") == ["assert"]

    def test_a_node_assert(self):
        assert kinds("node -e 'assert.ok(ran)'") == ["assert"]

    def test_a_bracketed_assert(self):
        assert kinds("python3 - <<'PY'\nassert(x)\nPY") == ["assert"]


class TestNotAnAssert:
    def test_a_commented_out_assert(self):
        assert kinds("python3 - <<'PY'\n# assert total > 0\nprint(1)\nPY") == []

    def test_a_commented_out_assert_in_node(self):
        assert kinds("node -e 'x'\n// assert.ok(x)") == []

    def test_a_method_whose_name_merely_starts_with_assert(self):
        # `assert_called_once()` is a call, not a statement, and a tool that
        # counted it would put a number on every mock-using repo in the org.
        assert kinds("python3 - <<'PY'\nassert_called_once()\nPY") == []

    def test_the_name_of_this_package_is_not_an_assert(self):
        # `\\bassert\\b` and not a substring: `import deadassert` sits on a
        # line with an interpreter on it and must not match.
        assert kinds("python3 -c 'import deadassert; print(deadassert.__file__)'") == []

    def test_the_word_assertions_in_prose(self):
        assert kinds("python3 -m pytest && echo 'all assertions ran'") == []

    def test_a_shell_line_mentioning_assert(self):
        assert kinds("echo 'assert something' > note.txt") == []


class TestDiff:
    def test_a_plain_diff(self):
        found = one("mytool . > got.txt\ndiff -u expected.txt got.txt")
        assert found.kind == "diff"
        assert found.line == 2

    def test_cmp_counts_too(self):
        assert kinds("cmp -s a.bin b.bin") == ["diff"]

    def test_a_diff_in_an_if_condition(self):
        assert kinds("if diff -q a b > /dev/null; then echo same; fi") == ["diff"]

    def test_a_redirected_diff_still_counts(self):
        # Under `bash -e` the status reaches the step whatever happens to
        # stdout, so this is an assertion and not extraction.
        assert kinds("diff -u a.txt b.txt > changes.diff") == ["diff"]

    def test_a_swallowed_diff_does_not_count(self):
        # The failure is thrown away, so there is no assertion here to have
        # missed reading.
        assert kinds("diff -u a.txt b.txt || true") == []

    def test_a_captured_diff_does_not_count(self):
        # Feeding a variable, exactly as `shell.scan` treats a captured grep.
        assert kinds('changed=$(diff -u a.txt b.txt); echo "$changed"') == []

    def test_a_filename_containing_diff_does_not_count(self):
        assert kinds("cat expected.diff") == []


class TestJq:
    def test_jq_e_counts(self):
        found = one("curl -sS $url | jq -e '.ok' > /dev/null")
        assert found.kind == "jq -e"

    def test_a_combined_short_flag(self):
        assert kinds("jq -er '.permalink' < resp.json") == ["jq -e"]

    def test_the_long_flag(self):
        assert kinds("jq --exit-status '.ok' resp.json") == ["jq -e"]

    def test_plain_jq_is_extraction(self):
        # Without -e the status says only that the JSON parsed, which is not
        # a verdict about the value and not what this is counting.
        assert kinds("jq -r '.items[].name' resp.json") == []

    def test_an_arg_named_e_is_not_the_flag(self):
        assert kinds("jq --arg e 1 '.x' resp.json") == []


class TestDeliberateOmissions:
    def test_test_is_not_counted(self):
        # The caveat on logbook#69: `test "$s" -eq N` is the commonest
        # assertion in the org and almost always sound, so counting it would
        # put a number on nearly every file and the number would stop being
        # read. Documented in the README rather than silently dropped.
        assert kinds('mytool .; s=$?\ntest "$s" -eq 1') == []

    def test_bracket_is_not_counted(self):
        assert kinds('[ -f out.txt ] || exit 1') == []

    def test_a_grep_is_not_counted_here(self):
        # `shell.scan` reads these, so counting them again would double-count
        # the only assertions the tool does understand.
        assert kinds("grep -qxF 'All clear.' out.txt") == []


class TestOrdering:
    def test_constructs_come_back_in_line_order(self):
        script = """
diff -u a.txt b.txt
python3 - <<'PY'
assert 1
PY
jq -e '.ok' r.json
"""
        assert [item.line for item in unread.scan(script)] == [2, 4, 6]
        assert kinds(script) == ["diff", "assert", "jq -e"]
