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
"""

import ast
import pathlib
import sys

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
            mode = node.args[0] if node.args else None
            binary = isinstance(mode, ast.Constant) and "b" in str(mode.value)
            if not binary:
                offenders.append(node.lineno)
    return offenders


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
