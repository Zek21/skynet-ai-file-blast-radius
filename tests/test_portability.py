"""Import resolution across the layouts real Python projects actually use.

These lock two defects found by pointing the analyser at a foreign repository
for the first time. Both produced the same symptom — a file with several
importers reporting **zero dependents and zero test coverage** — and both were
invisible on the repository the analyser grew up in, because that repository
uses a flat `tools/` layout with plain `import module` statements.

An impact analyser that under-reports is worse than no analyser: it manufactures
confidence that a change is safe.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from skynet_blast_radius import engine as atlas  # noqa: E402


@pytest.fixture()
def src_layout_repo(tmp_path):
    """The layout `pip`/`setuptools` generate: package under `src/`, tests importing it absolutely."""
    pkg = tmp_path / "src" / "mypkg"
    pkg.mkdir(parents=True)
    (tmp_path / "tests").mkdir()

    (pkg / "__init__.py").write_text(
        "from .transport import Socket\nfrom .client import Client\n", encoding="utf-8")
    (pkg / "transport.py").write_text(
        '"""Transport."""\nclass Socket:\n    pass\n', encoding="utf-8")
    (pkg / "client.py").write_text(
        "from .transport import Socket\n"
        "class Client:\n    def __init__(self):\n        self.s = Socket()\n",
        encoding="utf-8")
    (pkg / "helper.py").write_text(
        "from mypkg.transport import Socket\ndef f():\n    return Socket()\n",
        encoding="utf-8")
    sub = pkg / "sub"
    sub.mkdir()
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "deep.py").write_text(
        "from ..transport import Socket\ndef g():\n    return Socket()\n",
        encoding="utf-8")

    (tmp_path / "tests" / "test_transport.py").write_text(
        "from mypkg.transport import Socket\ndef test_s():\n    assert Socket()\n",
        encoding="utf-8")

    atlas.set_root(tmp_path)
    atlas._GRAPH_CACHE.clear()
    atlas.build_index(force=True)
    yield tmp_path
    atlas._GRAPH_CACHE.clear()


class TestRelativeImports:
    def test_from_dot_module_import_symbol_creates_an_edge(self, src_layout_repo):
        """`from .transport import Socket` names module 'transport'.

        The resolver previously returned the imported SYMBOLS for any relative
        import, so the tokens were ['Socket'] and the module never appeared.
        No edge could match, and intra-package dependencies — most of the graph
        in a normal package — were entirely invisible.
        """
        result = atlas.blast("src/mypkg/transport.py")
        dependents = set(result["radius"]["imports"])
        assert "src/mypkg/client.py" in dependents

    def test_from_dot_import_module_creates_an_edge(self, src_layout_repo):
        """`from . import x` — here the NAMES are the modules, not symbols."""
        result = atlas.blast("src/mypkg/transport.py")
        all_dependents = set(result["radius"]["imports"]) | set(result["radius"]["tests"])
        assert any("__init__" in d for d in all_dependents)

    def test_parent_relative_import_resolves(self, src_layout_repo):
        """`from ..transport import Socket`, two levels down."""
        result = atlas.blast("src/mypkg/transport.py")
        reached = set(result["radius"]["imports"])
        assert "src/mypkg/sub/deep.py" in reached

    def test_relative_prefix_walks_up_one_package_per_extra_dot(self):
        path = Path("/repo/src/pkg/sub/mod.py")
        assert atlas._relative_package_prefix(path, 1) == "sub"
        assert atlas._relative_package_prefix(path, 2) == "pkg"
        assert atlas._relative_package_prefix(path, 3) == "src"


class TestSrcLayoutAbsoluteImports:
    def test_package_absolute_import_resolves_under_a_source_root(self, src_layout_repo):
        """`from mypkg.transport import Socket` where the file is at
        `src/mypkg/transport.py`.

        Resolution previously required the dotted name to equal the path from the
        repo root, which is never true under a `src/` layout, so every
        package-absolute import failed silently.
        """
        result = atlas.blast("src/mypkg/transport.py")
        assert "src/mypkg/helper.py" in set(result["radius"]["imports"])

    def test_a_test_importing_the_package_counts_as_coverage(self, src_layout_repo):
        """The consequence that matters: unresolved imports made every file in a
        src-layout project look untested."""
        result = atlas.blast("src/mypkg/transport.py")
        assert result["coverage"]["target_covered"] is True
        assert "src/mypkg/transport.py" not in result["coverage"]["uncovered"]

    def test_module_map_offers_every_source_root_relative_form(self):
        mapping, _ = atlas._module_map({"src/mypkg/transport.py"})
        assert mapping.get("mypkg.transport") == "src/mypkg/transport.py"
        assert mapping.get("src.mypkg.transport") == "src/mypkg/transport.py"

    def test_a_dotted_name_claimed_by_two_files_is_dropped_not_guessed(self):
        """A wrong edge is worse than a missing one in a tool used to decide
        what is safe to change."""
        mapping, ambiguous = atlas._module_map({"src/a/mod.py", "lib/a/mod.py"})
        assert "a.mod" in ambiguous
        assert "a.mod" not in mapping

    def test_package_init_resolves_for_a_bare_package_import(self):
        mapping, _ = atlas._module_map({"src/mypkg/__init__.py"})
        assert mapping.get("src.mypkg") == "src/mypkg/__init__.py"


class TestRootDiscovery:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BLAST_RADIUS_ROOT", str(tmp_path))
        assert atlas._discover_root() == tmp_path.resolve()

    def test_git_working_tree_is_found_from_a_subdirectory(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        monkeypatch.delenv("BLAST_RADIUS_ROOT", raising=False)
        monkeypatch.chdir(nested)
        assert atlas._discover_root() == tmp_path.resolve()

    def test_set_root_rebinds_the_index_path(self, tmp_path):
        original = atlas.ROOT
        try:
            atlas.set_root(tmp_path)
            assert atlas.INDEX_PATH.parent == tmp_path.resolve()
        finally:
            atlas.set_root(original)


class TestNoSkynetCoupling:
    """The package must not require the repository it was extracted from."""

    def test_engine_source_names_no_skynet_specific_path(self):
        source = (ROOT / "src" / "skynet_blast_radius" / "engine.py").read_text(
            encoding="utf-8")
        for hardcoded in ("D:/Prospects", "D:\\\\Prospects", "skynet_system_registry.json",
                          "skynet_code_atlas_index.json"):
            assert hardcoded not in source, f"{hardcoded} still hardcoded"

    def test_missing_optional_registry_is_not_an_error(self, src_layout_repo):
        """An ordinary repo has no registry and no skills directory. Their
        absence is a supported configuration, not a degraded one."""
        assert not atlas.REGISTRY_PATH.exists()
        result = atlas.blast("src/mypkg/transport.py")
        assert result["ok"] is True


class TestAmbiguityIsReported:
    """A dropped edge that is never counted is invisible to the reader.

    `_module_map` discards a dotted name claimed by two files rather than
    guessing which one was meant. That is the right call, but the count was
    computed and then thrown away for several revisions, while the documentation
    claimed it was reported. An unreported exclusion is indistinguishable from an
    edge that never existed.
    """

    def test_dotted_collisions_are_counted_in_graph_stats(self, tmp_path):
        (tmp_path / "src" / "a").mkdir(parents=True)
        (tmp_path / "lib" / "a").mkdir(parents=True)
        (tmp_path / "tests").mkdir()
        for root in ("src", "lib"):
            (tmp_path / root / "a" / "__init__.py").write_text("", encoding="utf-8")
            (tmp_path / root / "a" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        (tmp_path / "src" / "user.py").write_text(
            "from a.mod import VALUE\n", encoding="utf-8")

        atlas.set_root(tmp_path)
        atlas._GRAPH_CACHE.clear()
        result = atlas.blast("src/a/mod.py")
        atlas._GRAPH_CACHE.clear()

        assert "ambiguous_dotted" in result["graph_stats"]
        assert result["graph_stats"]["ambiguous_dotted"] >= 1, \
            "a dotted name claimed by two files must be counted, not silently dropped"
