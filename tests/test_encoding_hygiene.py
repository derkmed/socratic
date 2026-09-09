"""The test suite's source scans decode UTF-8, not the machine's locale codec.

Several tests read the project's own source to enforce structural rules — the
import scan, the mode-branch scan, the profile-internals scan. Those files are
UTF-8 and carry non-ASCII punctuation (em dashes throughout the domain), so a
read that falls back to the platform's locale codec decodes mojibake on
Windows and raises `UnicodeDecodeError` wherever the codec has undefined slots
in the range. Nothing about a structural scan should depend on the machine it
runs on, and the prototype targets Mac, Windows and Linux alike.

Two layers here. The behavioural tests point a scan at a fixture whose bytes
are genuinely undecodable under a common non-UTF-8 codec. The guard test is
static: it walks every module under `tests/` and fails on any text-mode read
that omits `encoding`, so the convention survives the next scan someone adds.

`subprocess.run(..., text=True)` is one of those reads (#43). It decodes a
child's stdout and stderr through the same locale codec, one process removed —
and the suite's one such call reads its child's stderr only on failure, when
it holds a traceback quoting em-dashed domain source.
"""

import ast
import codecs
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

TESTS_ROOT = pathlib.Path(__file__).resolve().parent

# The suite has no package of its own, so sibling modules are only importable
# once their directory is on the path.
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import test_import_hygiene  # noqa: E402

# U+00C1 is `C3 81` in UTF-8, and 0x81 is one of cp1252's undefined slots, so
# these bytes raise rather than quietly mojibake when the locale codec wins.
UNDECODABLE_IN_CP1252 = "Á"


def _write_utf8(path: pathlib.Path, text: str) -> pathlib.Path:
    """Write `text` as UTF-8 bytes, bypassing any default-encoding choice."""
    path.write_bytes(text.encode("utf-8"))
    return path


def test_the_import_scan_reads_utf_8_source(tmp_path, monkeypatch):
    source_root = tmp_path / "socratic"
    source_root.mkdir()
    _write_utf8(
        source_root / "annotated.py",
        f'"""A docstring with non-ASCII punctuation: {UNDECODABLE_IN_CP1252}."""\n'
        "import json\n",
    )
    monkeypatch.setattr(test_import_hygiene, "SOURCE_ROOT", source_root)

    roots = test_import_hygiene._imported_roots()

    assert roots == {pathlib.Path("annotated.py"): {"json"}}


def test_the_pyproject_scan_reads_utf_8(tmp_path, monkeypatch):
    pyproject = _write_utf8(
        tmp_path / "pyproject.toml",
        "[project]\n"
        f'description = "Socratic — a tutor {UNDECODABLE_IN_CP1252}"\n'
        "dependencies = []\n",
    )
    monkeypatch.setattr(test_import_hygiene, "PYPROJECT", pyproject)

    config = test_import_hygiene._load_pyproject()

    assert config["project"]["dependencies"] == []
    assert UNDECODABLE_IN_CP1252 in config["project"]["description"]


def test_a_text_mode_subprocess_decodes_with_the_locale_codec():
    """`subprocess.run(..., text=True)` is the same choice as `open` without one.

    The proof has to be locale-independent, and the parent's own codec is
    whatever the machine says — so the check runs one level down: a middle
    interpreter started with the C locale and UTF-8 mode off, which reports
    its own preferred codec and then decodes a child's em dash twice, once
    with `encoding=` and once without. `-X warn_default_encoding` is no help
    here: 3.10's `subprocess` does not emit `EncodingWarning` for its implicit
    text wrapper the way `open` does, so the mangling itself is the evidence.
    """
    middle = (
        "import locale, subprocess, sys\n"
        "child = \"import sys; "
        "sys.stderr.buffer.write('\\\\u2014'.encode('utf-8'))\"\n"
        "print(locale.getpreferredencoding(False))\n"
        "for extra in ({}, {'encoding': 'utf-8'}):\n"
        "    try:\n"
        "        result = subprocess.run(\n"
        "            [sys.executable, '-c', child],\n"
        "            capture_output=True, text=True, **extra)\n"
        "        print(ascii(result.stderr))\n"
        "    except UnicodeDecodeError:\n"
        "        print('raised UnicodeDecodeError')\n"
    )
    environ = dict(os.environ)
    environ.update(
        LC_ALL="C", LC_CTYPE="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0"
    )
    probe = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", middle],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environ,
    )
    assert probe.returncode == 0, probe.stderr
    codec, without_encoding, with_encoding = probe.stdout.split("\n")[:3]
    if codecs.lookup(codec).name == "utf-8":
        pytest.skip(f"the machine's locale codec is already UTF-8 ({codec})")

    assert with_encoding == ascii("—"), (
        f"an explicit encoding should carry the em dash through: {with_encoding}"
    )
    assert without_encoding != with_encoding, (
        f"the em dash survived {codec} unscathed, so this machine cannot "
        "demonstrate the fault the guard below exists to prevent"
    )


SUBPROCESS_RUNNERS = frozenset({"run", "check_output", "check_call", "call", "Popen"})
"""The `subprocess` entry points that will wrap a pipe in a text decoder.

`run`, `check_output` and `Popen` are the shapes the suite has reason to use;
`call` and `check_call` take the same keywords and are listed so the guard does
not have to be edited the first time someone reaches for one."""

TEXT_MODE_KEYWORDS = frozenset({"text", "universal_newlines"})


def _is_binary_mode(node: ast.Call) -> bool:
    """Whether an `open` call names a binary mode.

    The mode sits at a different position in the two spellings: `open(path,
    "rb")` passes the file first, `path.open("rb")` does not. Reading the wrong
    one is not merely conservative — a filename that happens to contain a "b"
    reads as binary and the call is waved through.
    """
    for keyword in node.keywords:
        if keyword.arg == "mode":
            return isinstance(keyword.value, ast.Constant) and "b" in str(
                keyword.value.value
            )
    index = 0 if isinstance(node.func, ast.Attribute) else 1
    if len(node.args) <= index:
        return False
    mode = node.args[index]
    return isinstance(mode, ast.Constant) and "b" in str(mode.value)


def _decodes_in_text_mode(node: ast.Call) -> bool:
    """Whether a subprocess call asks for text without saying in which codec.

    `text=True` (or its older spelling `universal_newlines=True`) wraps the
    child's stdout and stderr in a decoder built from
    `locale.getpreferredencoding(False)`. A value that is not a literal counts
    as text mode: the guard cannot evaluate it, and the safe reading of an
    unknown flag is the one that asks for an encoding.
    """
    for keyword in node.keywords:
        if keyword.arg not in TEXT_MODE_KEYWORDS:
            continue
        value = keyword.value
        if isinstance(value, ast.Constant) and not value.value:
            continue
        return True
    return False


def _text_reads_without_encoding(tree: ast.AST) -> list[int]:
    """Line numbers of text-mode reads and writes that omit `encoding`."""
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if any(keyword.arg == "encoding" for keyword in node.keywords):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in {"read_text", "write_text"}:
            offenders.append(node.lineno)
        elif name == "open":
            # A binary handle has no encoding to get wrong; a text one does.
            if not _is_binary_mode(node):
                offenders.append(node.lineno)
        elif name in SUBPROCESS_RUNNERS and _decodes_in_text_mode(node):
            # Same choice as a text-mode `open`, one process removed: the
            # child's stdout and stderr come back through the locale codec.
            offenders.append(node.lineno)
    return offenders


def _offending_lines(source: str) -> list[int]:
    """The guard's verdict on a snippet, for the shape tests below."""
    return _text_reads_without_encoding(ast.parse(textwrap.dedent(source)))


def test_the_guard_flags_text_mode_subprocess_calls_without_an_encoding():
    assert _offending_lines("subprocess.run(cmd, text=True)\n") == [1]
    assert _offending_lines("subprocess.check_output(cmd, text=True)\n") == [1]
    assert _offending_lines("subprocess.Popen(cmd, universal_newlines=True)\n") == [1]


def test_the_guard_passes_over_subprocess_calls_that_cannot_mis_decode():
    assert _offending_lines("subprocess.run(cmd, text=True, encoding='utf-8')\n") == []
    assert _offending_lines("subprocess.run(cmd, capture_output=True)\n") == []
    assert _offending_lines("subprocess.run(cmd, text=False)\n") == []


def test_the_guard_reads_the_mode_argument_of_open_in_either_form():
    # `open(path, "rb")` and `path.open("rb")` put the mode at different
    # positions, and a filename that merely contains a "b" is not a mode.
    assert _offending_lines("open(path, 'rb')\n") == []
    assert _offending_lines("path.open('rb')\n") == []
    assert _offending_lines("open(path, mode='rb')\n") == []
    assert _offending_lines("open('bench.txt')\n") == [1]


def test_no_test_module_reads_a_file_with_the_locale_codec():
    offenders = {}
    for path in sorted(TESTS_ROOT.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        lines = _text_reads_without_encoding(ast.parse(source))
        if lines:
            offenders[path.name] = lines

    assert offenders == {}, (
        "text reads without an explicit encoding decode through the machine's "
        f"locale codec: {offenders}"
    )
