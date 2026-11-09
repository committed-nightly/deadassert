# deadassert

Say which of your CI assertions pass whether or not the thing they name is
true.

If you have a workflow full of `grep -q 'all clear' out.txt` steps and you
want to know which of them would still be green after the output they check
stopped saying that, this tells you. It reads the `run:` scripts in
`.github/workflows`, works out which way round each assertion fails, and
reports the ones that cannot fail at all.

It also counts the assertions it did *not* read — a `diff`, a `jq -e`, a
Python `assert` — so that a clean run says what it covered rather than
implying it covered everything.

## Install

```sh
pip install -e .
```

Python 3.10+, PyYAML, nothing else.

## Use

```sh
deadassert                            # .github/workflows in the current repo
deadassert path/to/repo
deadassert -f .github/workflows/ci.yml
deadassert --against out.txt          # decide `prefix` against real output
deadassert -q                         # one line per finding
deadassert --json
```

Exit code is 0 when there is nothing to report, 1 when there is, and 2 when
`deadassert` could not get an answer — no workflow files, or none that
parse. On exit 2 nothing is printed to stdout, so a run that failed can
never be read as a clean one.

A workflow with no assertions in it exits 0, but says so in its own words
rather than reporting a clean one.

## The thing this is about

An assertion that rots does not rot symmetrically.

```sh
grep -qF 'no dead entries' out.txt          # fails when the string goes away
if grep -qF 'dead entry' out.txt; then      # passes when the string goes away
  exit 1
fi
```

Both are reasonable lines to write. The first one looks after itself: change
the wording in the tool and CI goes red that afternoon and somebody fixes
it. The second is a promise that a particular string stays out of the
output, and the day the tool stops being able to produce that string at all,
the promise becomes free. It keeps passing. Nothing tells you it stopped
checking anything, because from the outside a negated assertion that is
working and a negated assertion that is dead look exactly the same: green.

`deadassert` finds the second kind by asking what *else* is in your
workflows. If something, somewhere, positively asserts that the string can
appear, the negated assertion has a witness and is sound. If nothing does,
it is a check with no way of ever firing.

## An example

`examples/` is a repository whose dogfood job looks thorough. Four things
are wrong with it, in four different ways.

```console
$ deadassert examples/
ci.yml:51  unwitnessed  'countdead warning:'
    grep: passes whether or not the pattern is ever produced
    this passes when the pattern is absent, and nothing in these workflows asserts that it
    is ever present -- so it also passes if the pattern stops being produced at all
      if grep -qF 'countdead warning:' out.txt; then

ci.yml:42  prefix  'Every entry here is read.'
    grep: matches a whole sentence as a substring
    the pattern reads as a whole line of output, but -F without -x matches it as a
    substring, so the assertion survives anything appended to that line
      grep -qF 'Every entry here is read.' clean.txt

ci.yml:61  swallowed  '"dead": 2'
    grep: cannot fail the step
    its failure is discarded, so the step passes either way
      grep -qF '"dead": 2' out.json || true

ci.yml:67  decorative  'skipped'
    grep: cannot fail the step
    it chooses which branch runs, and neither branch can fail the step
      if grep -qF 'skipped' seen.txt; then

4 assertions here do not check what they read as checking (1 unwitnessed, 1 prefix, 1 swallowed, 1 decorative).
7 assertions in 7 scripts across 1 workflow file, 1 of them negated. Not counted above: 2 assertions in 1 of those scripts (1 assert, 1 diff), listed below.

not read:
  ci.yml:96  assert  assert d['dead'] == 2, d['dead']
  ci.yml:99  diff    diff -u examples/expected-porcelain.txt got.txt
```

The last two lines are not findings — they are the two assertions in that
file `deadassert` never read, and the reason the four above them are a
verdict about the greps rather than about the file. See `not read:` below.

## The kinds

| kind | what it means |
| --- | --- |
| `unwitnessed` | fails only when its pattern appears, and nothing establishes that it can |
| `prefix` | names a whole output line but matches it as a substring |
| `swallowed` | its failure is discarded, by `\|\| true` or similar |
| `masked` | its exit status is thrown away by its position in a pipeline |
| `decorative` | it picks a branch, and neither branch can fail the step |

`not read` is not in that table on purpose. The five kinds are verdicts on
an assertion; `not read` is the admission that there was an assertion there
and no verdict was reached. It is never a finding and never changes the exit
code.

## `--against`, and the one guess in here

`prefix` is the only kind that rests on what somebody *meant*. `grep -qF 'x'`
matching a substring is not a bug; it is a bug only if the pattern was meant
to be the whole line. Without help, `deadassert` guesses from the shape of
the pattern — it has to end in a full stop, start with a capital or a digit,
be four words or more, and carry no `^` or `$`. Every one of those clauses is
there because a looser version fired on a substring match somebody meant.

Give it real output and the guess goes away:

```console
$ countdead examples/ > output.txt       # whatever your tool prints
$ countdead clean >> output.txt          # every variant, concatenated
$ deadassert examples/ --against output.txt
ci.yml:42  prefix  'Every entry here is read.'
    grep: matches a whole sentence as a substring
    in output.txt that line reads 'Every entry here is read. Read 0 entries in 1 file.' --
    the pattern is its opening, so this assertion is already passing over a line it does not
    name in full, which is the edit it was written to catch
```

That is the finding worth having, and the shape-based guess cannot see it:
the line has **already** grown and the assertion did not notice. Pass
`--against` a file holding every variant of output your CI matches against —
it is compared line by line, so more is better.

`--against` also takes findings away. A pattern that only ever appears
*inside* a line, never at its start, is a substring match on purpose and
stops being reported.

## What it will not tell you

- **It does not run anything.** It reads workflow files. A step that fails
  for some other reason is not its business.
- **It reads POSIX shells only.** A `run:` under `pwsh` or `shell: python`
  is skipped, and the skip is printed, so it never reads as a pass.
- **A pattern the shell fills in is never judged.** `grep -qF "$expected"`
  is a real assertion, but the characters in the file are not the characters
  grep gets, so it goes in `not decided:` and is left out of the counts.
- **It only knows about matchers.** `grep`, `egrep`, `fgrep`, `rg`. A
  verdict reached by `diff`, `test` or a language's own assert is not read —
  which is a reason to prefer those, not a gap to fill. See below. It is no
  longer *invisible*, though: those get counted, and the count is printed.
- **`unwitnessed` is a smell, not a proof.** It says nothing in *these
  workflows* establishes the string can appear. The proof may well live in a
  unit test, where it is arguably better placed. What the finding is really
  telling you is that the CI step, read on its own, does not stand up.

## `not read:` — the assertions it did not read

A clean run used to read as *the assertions in this file can fail*. It only
ever meant *the greps in this file can fail*, and the difference is not
academic. On the night `deadassert` was swept across this organisation
looking for dead assertions, it walked past one — in the repository it was
auditing, two steps below a finding it did report:

```sh
python3 -c "
loose = json.load(open('loose.json'))['shapes'][0]['candidates']
assert loose > tight * 5, (loose, tight)
"
```

An assertion whose truth depends on the runner image rather than on the
thing it names. Same defect, different punctuation, and the run said `2
findings` over `32 assertions in 20 scripts` without a word to say that one
of those 20 scripts asserted something it had not read.

So it counts them now:

```console
$ deadassert .
Every assertion here fails when the thing it names stops being true.
2 assertions in 15 scripts across 1 workflow file, 0 of them negated. Not counted above: 11 assertions in 5 of those scripts (9 assert, 2 diff), listed below.

not read:
  ci.yml:37   assert  assert total >= 110, f"only {total} tests collected"
  ci.yml:85   diff    diff -u examples/expected-quiet.txt got.txt
  ...
```

Three shapes, picked by counting what the workflow files in this
organisation actually use: a language's own `assert` (nearly always Python,
via `-c` or a heredoc), `diff` and `cmp`, and `jq -e`.

**This decides nothing.** It does not judge whether a `diff` can fail; it
reports that one is there and was not read. The same move the tool already
makes with `not decided:` and with the non-POSIX shells it skips — and for
the same reason, which is that a tool about assertions that quietly check
nothing has no business quietly covering nothing. It is coverage, not a
complaint: `diff` is what the section below tells you to prefer, and a
repository full of them gets a count, not a finding. Nothing here affects
the exit code, and `-q` still prints findings only.

The sharpest case was the verdict over no greps at all. `deadassert
uncollected` used to print:

```
No assertions here. Read 12 run: scripts in 1 workflow file and found no
command whose exit status decides a step. Nothing to check.
```

over a CI file holding nine Python asserts. Two other repositories in this
organisation got the same answer.

**`test` is deliberately not counted.** `test "$s" -eq 0` and `[ -f out ]`
are the most common assertions in these files and are usually the soundest
thing in them — the status *is* the assertion, with no pattern to rot. A
count that included them would be non-zero nearly everywhere on day one and
would stop being read, which is the one failure mode that would make this
worse than silence. The cost is real and this is where it is written down: a
`test` on a status captured from the wrong command is not covered here
either.

## `grep` is a poor way to assert a whole output

Having written this, the conclusion it kept pushing me towards is that most
of these findings are a symptom of reaching for the wrong tool. If you are
checking an entire output, `diff` against a committed file says exactly what
you mean, fails loudly on any change, and has no substring semantics to get
wrong:

```sh
mytool examples/ > got.txt
diff -u expected.txt got.txt
```

This repository's own CI does that wherever it can, which is why
`deadassert` run on itself finds only two assertions to examine. That number
is small on purpose, and it is printed rather than hidden.

## Measured on something real

Run across the 31 repositories of the organisation this was written in — 37
workflow files, 360 `run:` scripts:

| | count |
| --- | --- |
| assertions read | 170 |
| negated (`needs-no-match`) | 24 |
| `unwitnessed` | 6 |
| `prefix` | 3 |
| `swallowed` / `masked` / `decorative` | 0 |
| not read | 74, in 24 scripts |

Seven of the thirty-one repositories have at least one finding. The first
run of this tool found 33 — 12 `unwitnessed` and 21 `prefix`, across 12
repositories — and the 24 that have gone are a repo-at-a-time sweep, not a
`sed`; the remaining 9 are the tail of it.

The three can't-fail kinds scoring zero is the result I did not expect:
every `|| true` in that organisation is on a *tool invocation*, deliberately
allowing a non-zero findings exit before the output is asserted, which is
correct usage. An early version of this tool reported all of them and was
wrong to.

The last row is the one that changes how you should read the others. **74
assertions in these files are not read at all** — 67 Python asserts, 5
`diff`, 2 `jq -e` — against 170 that are, and they are concentrated: 15
repositories have at least one, and 3 have nothing *but* unread assertions,
so the verdict on them used to be `Nothing to check.` The 9 findings above
are a verdict about the greps in this organisation, which is a smaller thing
than it looks, and the point of printing the number is that you no longer
have to take my word for how much smaller.

`shellcheck` 0.9.0 reports none of these at default settings. With
`--enable=all` it finds the two pipeline cases as `SC2312` (info) and
suggests fixing them with `|| true`, which for an assertion is the opposite
of a fix.

## Licence

MIT.
