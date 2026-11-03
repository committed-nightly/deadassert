"""Command line. Exit 0 nothing to report, 1 findings, 2 no answer."""

from __future__ import annotations

import argparse
import sys

from . import report
from .analyse import CorpusError, analyse


def _os_error(error: OSError) -> str:
    """An OSError as a sentence, rather than as its repr.

    `str(OSError)` is `[Errno 2] No such file or directory: '/tmp/x'`, which
    is Python's, not ours. The rest of the family prints `<prog>: <path>:
    <reason>` and so does this.
    """
    reason = (error.strerror or "cannot be read").lower()
    return f"{error.filename or '?'}: {reason}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deadassert",
        description=(
            "Say which of your CI assertions pass whether or not the thing they name is true."
        ),
        epilog=(
            "Exit code is 0 when there is nothing to report, 1 when there is, and 2 "
            "when deadassert could not get an answer at all."
        ),
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="repository to read .github/workflows from (default: .)",
    )
    parser.add_argument(
        "-f",
        "--file",
        action="append",
        default=[],
        metavar="PATH",
        help="read this workflow file instead of searching; repeatable",
    )
    parser.add_argument(
        "--against",
        metavar="PATH",
        help=(
            "a file of real output the assertions run over. Decides the `prefix` "
            "kind by comparison instead of by the shape of the pattern."
        ),
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="one line per finding")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        result = analyse(args.root, paths=args.file or None, against=args.against)
    except CorpusError as error:
        print(f"deadassert: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"deadassert: {_os_error(error)}", file=sys.stderr)
        return 2

    for line in report.error_lines(result):
        print(line, file=sys.stderr)

    if not result.files:
        # Nothing was read, so there is no measurement for a verdict to be
        # about. Nothing on stdout either, which is what stops a failed run
        # reading as a clean one.
        print("deadassert: no workflow file could be read", file=sys.stderr)
        return 2

    if args.json:
        print(report.as_json(result))
    else:
        print(report.text(result, quiet=args.quiet))

    return 1 if result.findings else 0
