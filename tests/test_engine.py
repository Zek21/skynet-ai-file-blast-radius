"""Tests for the blast radius engine -- find / blast radius / advisor packet.

Grounded in a real defect caught while building the tool on 2026-08-01: the
reference scanner matched only the bare import form, so `tools/blog_cdp_session.py`
reported a fan-in of **2** when a plain grep found 103 referencing files. The repo
imports the same module both ways. Under-reporting a blast radius is worse than
not measuring it, because it manufactures false confidence -- that regression is
locked below and must never come back.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from skynet_blast_radius import engine as atlas  # noqa: E402


@pytest.fixture()
def fake_repo(tmp_path, monkeypatch):
    """A miniature repo: one target tool, and callers that reach it 4 different ways."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "data").mkdir()

    (tmp_path / "tools" / "target_tool.py").write_text(
        '"""Target tool for radius tests."""\n'
        "import argparse\n"
        "def run():\n    return 1\n",
        encoding="utf-8")

    # 1. bare import
    (tmp_path / "tools" / "caller_bare.py").write_text(
        "from target_tool import run\n", encoding="utf-8")
    # 2. package-qualified import -- THE regression
    (tmp_path / "tools" / "caller_dotted.py").write_text(
        "from tools.target_tool import run\n", encoding="utf-8")
    # 3. subprocess/CLI reference by path
    (tmp_path / "tools" / "caller_cli.py").write_text(
        'CMD = "python tools/target_tool.py --json"\n', encoding="utf-8")
    # 4. registry naming the module with no extension
    (tmp_path / "data" / "reg.json").write_text(
        json.dumps({"deps": ["target_tool"]}), encoding="utf-8")

    (tmp_path / "tests" / "test_target_tool.py").write_text(
        "from tools.target_tool import run\ndef test_run():\n    assert run() == 1\n",
        encoding="utf-8")

    monkeypatch.setattr(atlas, "ROOT", tmp_path)
    monkeypatch.setattr(atlas, "INDEX_PATH", tmp_path / "data" / "idx.json")
    monkeypatch.setattr(atlas, "REGISTRY_PATH", tmp_path / "data" / "registry.json")
    monkeypatch.setattr(atlas, "SKILLS_DIR", tmp_path / "skills")
    return tmp_path


# --- blast radius: the regression that started this file ---------------------

def test_package_qualified_import_counts_as_an_importer(fake_repo):
    """`from tools.X import Y` is an import. Missing it under-reported fan-in 49x."""
    hits = atlas._referencing_files("tools/target_tool.py", "target_tool")
    assert "tools/caller_dotted.py" in hits["imports"]


def test_bare_import_also_counts(fake_repo):
    hits = atlas._referencing_files("tools/target_tool.py", "target_tool")
    assert "tools/caller_bare.py" in hits["imports"]


def test_cli_path_reference_counts_as_a_caller(fake_repo):
    """A subprocess string is real coupling even though no import exists."""
    hits = atlas._referencing_files("tools/target_tool.py", "target_tool")
    assert "tools/caller_cli.py" in hits["cli_callers"]


def test_registry_naming_module_without_extension_counts(fake_repo):
    hits = atlas._referencing_files("tools/target_tool.py", "target_tool")
    assert "data/reg.json" in hits["registries"]


def test_test_file_is_recorded_so_coverage_is_visible(fake_repo):
    hits = atlas._referencing_files("tools/target_tool.py", "target_tool")
    assert "tests/test_target_tool.py" in hits["tests"]


def test_blast_reports_tier_and_tested(fake_repo):
    res = atlas.blast("tools/target_tool.py")
    assert res["ok"] is True
    assert res["fan_in"] >= 3
    assert res["tested"] is True


def test_blast_on_missing_path_is_an_error_not_a_crash(fake_repo):
    res = atlas.blast("tools/does_not_exist.py")
    assert res["ok"] is False and "not found" in res["error"]


# --- the gate must fail CLOSED ----------------------------------------------

def test_gate_blocks_high_tier_with_no_tests():
    ok, msg = atlas.blast_gate(
        {"ok": True, "path": "t.py", "tier": "HIGH", "tested": False, "fan_in": 40})
    assert ok is False and "NO tests" in msg


def test_gate_allows_high_tier_that_is_tested():
    ok, _ = atlas.blast_gate(
        {"ok": True, "path": "t.py", "tier": "HIGH", "tested": True, "fan_in": 40})
    assert ok is True


def test_gate_blocks_an_unresolvable_target():
    ok, _ = atlas.blast_gate({"ok": False, "error": "not found: x"})
    assert ok is False


# --- static analysis via ast, not regex -------------------------------------

def test_subcommands_come_from_real_add_parser_calls(tmp_path):
    p = tmp_path / "cli.py"
    p.write_text(
        '"""One line summary."""\n'
        "import argparse\n"
        "ap = argparse.ArgumentParser()\n"
        "sub = ap.add_subparsers()\n"
        "sub.add_parser('status')\n"
        "ap.add_argument('--gate')\n"
        "def public():\n    pass\n"
        "def _private():\n    pass\n",
        encoding="utf-8")
    info = atlas._summarize_python(p)
    assert info["subcommands"] == ["status"]
    assert "--gate" in info["flags"]
    assert info["summary"] == "One line summary."
    assert "public" in info["symbols"] and "_private" not in info["symbols"]


def test_commented_out_def_is_not_a_symbol(tmp_path):
    """The reason this uses ast: a regex would report `ghost` as a real symbol."""
    p = tmp_path / "c.py"
    p.write_text("# def ghost():\n#     pass\ndef real():\n    pass\n", encoding="utf-8")
    info = atlas._summarize_python(p)
    assert info["symbols"] == ["real"]


def test_syntax_error_is_reported_not_swallowed(tmp_path):
    p = tmp_path / "broken.py"
    p.write_text("def (:\n", encoding="utf-8")
    info = atlas._summarize_python(p)
    assert info["parse_error"] is not None


# --- find: every term must land ---------------------------------------------

def test_find_requires_all_terms(fake_repo):
    atlas.build_index(force=True)
    assert atlas.find("target tool") != []
    assert atlas.find("target nonexistentterm") == []


def test_find_ranks_exact_stem_first(fake_repo):
    atlas.build_index(force=True)
    res = atlas.find("target_tool")
    assert res[0]["path"] == "tools/target_tool.py"


# --- excluded trees ----------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "node_modules/x/y.py",
    "screenmemory-vsix/dist/a.py",
    "tools/_archive_2026_05_16/old.py",
    "tools/__pycache__/x.py",
])
def test_build_output_and_frozen_history_are_excluded(fake_repo, rel):
    assert atlas._excluded(fake_repo / rel) is True


def test_live_tools_are_not_excluded(fake_repo):
    assert atlas._excluded(fake_repo / "tools" / "target_tool.py") is False


# --- advisor packet ----------------------------------------------------------

def test_packet_puts_the_question_first_and_demands_a_verdict(fake_repo):
    out = fake_repo / "packet"
    res = atlas.build_packet(["tools/target_tool.py"], "Is this idempotent?", out)
    brief = (out / "BRIEF.md").read_text(encoding="utf-8")
    assert res["ok"] is True
    assert brief.index("Is this idempotent?") < brief.index("What is attached")
    assert "VERDICT: APPROVE|BLOCK|UNSURE" in brief


def test_packet_copies_real_bytes_and_lists_them_as_attachments(fake_repo):
    out = fake_repo / "packet"
    res = atlas.build_packet(["tools/target_tool.py"], "q", out)
    assert (out / "target_tool.py").exists()
    assert any(a.endswith("target_tool.py") for a in res["attachments"])
    assert res["attachments"][0].endswith("BRIEF.md")


def test_packet_skips_an_oversized_file_with_a_stated_reason(fake_repo, monkeypatch):
    """A silently dropped attachment is how an advisor ends up guessing."""
    monkeypatch.setattr(atlas, "MAX_PACK_FILE_BYTES", 10)
    res = atlas.build_packet(["tools/target_tool.py"], "q", fake_repo / "p2")
    assert res["skipped"] and "exceeds" in res["skipped"][0]["why"]


def test_packet_records_a_missing_target_instead_of_silently_dropping_it(fake_repo):
    res = atlas.build_packet(["tools/nope.py"], "q", fake_repo / "p3")
    assert res["skipped"][0]["why"] == "not found"


def test_brief_is_capped_and_says_so(fake_repo, monkeypatch):
    monkeypatch.setattr(atlas, "MAX_BRIEF_BYTES", 200)
    res = atlas.build_packet(["tools/target_tool.py"], "q" * 500, fake_repo / "p4")
    assert res["truncated"] is True
    assert res["brief_bytes"] <= 200 + 120  # cap plus the truncation notice
    assert "truncated at cap" in (fake_repo / "p4" / "BRIEF.md").read_text(encoding="utf-8")


def test_packet_emits_a_runnable_convene_command(fake_repo):
    res = atlas.build_packet(["tools/target_tool.py"], "q", fake_repo / "p5")
    assert "skynet_ai_convene_cdp.py --target both" in res["convene_command"]
    assert "--attachment" in res["convene_command"]


# --- markdown audit ----------------------------------------------------------

def test_md_audit_reads_authorities_from_the_central_registry(fake_repo):
    (fake_repo / "data" / "registry.json").write_text(
        json.dumps({"markdown": {"authorities": [{"path": "CLAUDE.md"}]}}), encoding="utf-8")
    (fake_repo / "PROPOSAL_ORPHAN.md").write_text("# orphan\n", encoding="utf-8")
    res = atlas.md_audit()
    assert res["authorities"] == ["CLAUDE.md"]
    assert res["proposal_count"] >= 1


def test_generated_index_is_never_counted_as_a_reference(fake_repo):
    """The index lists every path, so counting it made 1512 proposals look referenced.

    A tool must not cite its own output as evidence. Real run: 0 orphans before
    this exclusion, 622 after.
    """
    (fake_repo / "data" / "registry.json").write_text(json.dumps({}), encoding="utf-8")
    (fake_repo / "PROPOSAL_ONLY_IN_INDEX.md").write_text("# x\n", encoding="utf-8")
    atlas.build_index(force=True)          # writes INDEX_PATH naming that proposal
    assert atlas.INDEX_PATH.exists()
    assert "PROPOSAL_ONLY_IN_INDEX.md" in atlas.INDEX_PATH.read_text(encoding="utf-8")
    res = atlas.md_audit()
    assert "PROPOSAL_ONLY_IN_INDEX.md" in res["orphan_proposals_sample"]


def test_index_is_excluded_from_blast_radius_too(fake_repo):
    """Same contamination path: the index would give every file a phantom registry hit."""
    atlas.build_index(force=True)
    hits = atlas._referencing_files("tools/target_tool.py", "target_tool")
    assert not any("idx.json" in r for r in hits["registries"])


def test_md_audit_flags_an_unreferenced_proposal_as_orphan(fake_repo):
    (fake_repo / "data" / "registry.json").write_text(json.dumps({}), encoding="utf-8")
    (fake_repo / "PROPOSAL_NOBODY_LINKS_ME.md").write_text("# x\n", encoding="utf-8")
    res = atlas.md_audit()
    assert "PROPOSAL_NOBODY_LINKS_ME.md" in res["orphan_proposals_sample"]


# --- index -------------------------------------------------------------------

def test_index_is_reused_when_nothing_changed(fake_repo):
    first = atlas.build_index(force=True)
    second = atlas.build_index()
    assert second["generated"] == first["generated"]


def test_index_rebuilds_after_a_new_file_appears(fake_repo):
    atlas.build_index(force=True)
    (fake_repo / "tools" / "newly_added.py").write_text("x = 1\n", encoding="utf-8")
    rebuilt = atlas.build_index()
    assert "tools/newly_added.py" in rebuilt["entries"]


# =============================================================================
# TRANSITIVE BLAST RADIUS
#
# The defect this half of the file locks: `blast` used to count only DIRECT
# importers. Measured live on this repo, tools/skynet_advisor_ladder.py has 5
# direct importers and 244 transitive dependents reached at depth 9, and the old
# gate passed it clean. Two direct importers and a hundred hidden dependents must
# never again read the same as two direct importers and nothing.
# =============================================================================


@pytest.fixture()
def chain_repo(tmp_path, monkeypatch):
    """leaf <- mid <- top <- apex, with one test covering only part of the chain."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "data").mkdir()

    (tmp_path / "tools" / "leaf.py").write_text(
        '"""Leaf module."""\ndef go():\n    return 1\n', encoding="utf-8")
    (tmp_path / "tools" / "mid.py").write_text(
        "from tools.leaf import go\n", encoding="utf-8")
    (tmp_path / "tools" / "top.py").write_text(
        "from tools.mid import go\n", encoding="utf-8")
    (tmp_path / "tools" / "apex.py").write_text(
        "from tools.top import go\n", encoding="utf-8")
    (tmp_path / "tests" / "test_top.py").write_text(
        "from tools.top import go\ndef test_go():\n    assert go()\n", encoding="utf-8")

    monkeypatch.setattr(atlas, "ROOT", tmp_path)
    monkeypatch.setattr(atlas, "INDEX_PATH", tmp_path / "data" / "idx.json")
    monkeypatch.setattr(atlas, "REGISTRY_PATH", tmp_path / "data" / "registry.json")
    monkeypatch.setattr(atlas, "SKILLS_DIR", tmp_path / "skills")
    atlas._GRAPH_CACHE.clear()
    return tmp_path


def _graph(_repo):
    return atlas.build_dep_graph(atlas.build_index(force=True))


def test_transitive_dependents_reach_past_direct_importers(chain_repo):
    """leaf has ONE direct importer and THREE transitive ones. That gap is the point."""
    res = atlas.blast("tools/leaf.py")
    assert res["transitive"]["direct_count"] == 1
    assert res["transitive"]["count"] == 4          # mid, top, apex, test_top
    assert res["transitive"]["code_count"] == 3
    assert res["transitive"]["test_count"] == 1


def test_each_dependent_carries_the_depth_it_was_reached_at(chain_repo):
    res = atlas.blast("tools/leaf.py")
    depths = {d["path"]: d["depth"] for d in res["transitive"]["dependents"]}
    assert depths["tools/mid.py"] == 1
    assert depths["tools/top.py"] == 2
    assert depths["tools/apex.py"] == 3
    assert res["transitive"]["max_depth"] == 3


def test_each_dependent_carries_the_shortest_path_that_reaches_it(chain_repo):
    """A number nobody can audit is a number nobody should act on."""
    res = atlas.blast("tools/leaf.py")
    apex = next(d for d in res["transitive"]["dependents"] if d["path"] == "tools/apex.py")
    assert apex["shortest_path"] == [
        "tools/apex.py", "tools/top.py", "tools/mid.py", "tools/leaf.py"]


def test_a_shortcut_edge_wins_over_the_long_way_round(chain_repo):
    """Depth must be the SHORTEST distance, not whichever path BFS happened to take."""
    (chain_repo / "tools" / "shortcut.py").write_text(
        "from tools.top import go\nfrom tools.leaf import go as g2\n", encoding="utf-8")
    res = atlas.blast("tools/leaf.py")
    shortcut = next(d for d in res["transitive"]["dependents"]
                    if d["path"] == "tools/shortcut.py")
    assert shortcut["depth"] == 1
    assert shortcut["shortest_path"] == ["tools/shortcut.py", "tools/leaf.py"]


def test_an_import_cycle_terminates_and_counts_each_file_once(chain_repo):
    """a -> b -> c -> a. A naive walk here never returns; a visited-set BFS does."""
    (chain_repo / "tools" / "a.py").write_text("from tools.b import x\n", encoding="utf-8")
    (chain_repo / "tools" / "b.py").write_text("from tools.c import x\n", encoding="utf-8")
    (chain_repo / "tools" / "c.py").write_text("from tools.a import x\n", encoding="utf-8")
    (chain_repo / "tools" / "d.py").write_text("from tools.a import x\n", encoding="utf-8")

    res = atlas.blast("tools/c.py")
    paths = [d["path"] for d in res["transitive"]["dependents"]]
    assert len(paths) == len(set(paths)), "a cycle must not double-count a dependent"
    assert set(paths) == {"tools/b.py", "tools/a.py", "tools/d.py"}
    assert "tools/c.py" not in paths, "the target must never be its own dependent"


def test_a_self_import_cycle_of_two_files_terminates(chain_repo):
    (chain_repo / "tools" / "ping.py").write_text("from tools.pong import y\n", encoding="utf-8")
    (chain_repo / "tools" / "pong.py").write_text("from tools.ping import y\n", encoding="utf-8")
    res = atlas.blast("tools/ping.py")
    assert [d["path"] for d in res["transitive"]["dependents"]] == ["tools/pong.py"]


def test_traversal_does_not_expand_through_a_test_file(chain_repo):
    """A test is a leaf consumer. Routing production impact through one is fiction."""
    (chain_repo / "tools" / "reads_a_test.py").write_text(
        "from tests.test_top import test_go\n", encoding="utf-8")
    res = atlas.blast("tools/leaf.py")
    paths = [d["path"] for d in res["transitive"]["dependents"]]
    assert "tests/test_top.py" in paths, "the test itself is still a dependent"
    assert "tools/reads_a_test.py" not in paths


def test_max_depth_bounds_the_cascade_without_lying_about_it(chain_repo):
    res = atlas.blast("tools/leaf.py", max_depth=1)
    assert res["transitive"]["max_depth"] == 1
    assert {d["path"] for d in res["transitive"]["dependents"]} == {"tools/mid.py"}


# --- edge extraction: the false edges that were measured on the real repo -----

def test_a_path_named_only_in_a_docstring_is_not_a_dependency(chain_repo):
    """Real case: tools/skynet_post_structure.py documents publish_post.py.

    Counting that made the documenter a DEPENDENT of the thing it documents,
    which is the true edge pointing backwards.
    """
    (chain_repo / "tools" / "documents_leaf.py").write_text(
        '"""This module explains how tools/leaf.py works."""\nX = 1\n', encoding="utf-8")
    res = atlas.blast("tools/leaf.py")
    assert "tools/documents_leaf.py" not in [d["path"] for d in res["transitive"]["dependents"]]


def test_a_path_inside_an_embedded_script_payload_is_not_a_dependency(chain_repo):
    """Real case: tools/skynet_transfer_bundle.py embeds a PowerShell installer.

    A comment INSIDE that payload mentioned publish_post.py and manufactured an
    edge to it. A path used as an argument is short and single-line; a payload
    is neither.
    """
    payload = "# installer" + (" x" * 300) + chr(10) + "run tools/leaf.py"
    (chain_repo / "tools" / "bundles_a_script.py").write_text(
        "SCRIPT = " + repr(payload) + chr(10), encoding="utf-8")
    res = atlas.blast("tools/leaf.py")
    assert "tools/bundles_a_script.py" not in [d["path"]
                                               for d in res["transitive"]["dependents"]]


def test_a_short_path_literal_in_live_code_IS_a_dependency(chain_repo):
    """The counterpart: str(ROOT / "tools" / "leaf.py") is real coupling."""
    (chain_repo / "tools" / "shells_out.py").write_text(
        'CMD = ["python", "tools/leaf.py", "--json"]\n', encoding="utf-8")
    (chain_repo / "tools" / "joins_a_path.py").write_text(
        'from pathlib import Path\nP = Path("tools") / "leaf.py"\n', encoding="utf-8")
    res = atlas.blast("tools/leaf.py")
    reached = {d["path"]: d["edge"] for d in res["transitive"]["dependents"]}
    assert reached.get("tools/shells_out.py") == "path"
    assert reached.get("tools/joins_a_path.py") == "path"


def test_from_package_import_module_counts_as_an_import_edge(chain_repo):
    """`from tools import leaf` is a real import that a bare-module regex misses."""
    (chain_repo / "tools" / "pkg_style.py").write_text(
        "from tools import leaf as publisher\n", encoding="utf-8")
    res = atlas.blast("tools/leaf.py")
    reached = {d["path"]: d["edge"] for d in res["transitive"]["dependents"]}
    assert reached.get("tools/pkg_style.py") == "import"


def test_a_stdlib_import_never_resolves_to_a_same_named_repo_file(chain_repo):
    """import os.path must not create an edge to a repo file called path.py."""
    (chain_repo / "tools" / "path.py").write_text("X = 1\n", encoding="utf-8")
    (chain_repo / "tools" / "uses_stdlib.py").write_text(
        "import os.path\nimport json.decoder\n", encoding="utf-8")
    res = atlas.blast("tools/path.py")
    assert "tools/uses_stdlib.py" not in [d["path"] for d in res["transitive"]["dependents"]]


# --- coverage of the radius, reported honestly --------------------------------

def test_coverage_names_the_uncovered_files_it_does_not_just_count_them(chain_repo):
    """tests/test_top.py reaches top -> mid -> leaf. apex is reached by nothing."""
    res = atlas.blast("tools/leaf.py")
    cov = res["coverage"]
    assert cov["uncovered"] == ["tools/apex.py"]
    assert set(cov["covered"]) == {"tools/leaf.py", "tools/mid.py", "tools/top.py"}
    assert cov["population"] == 4          # target + 3 non-test dependents
    assert cov["uncovered_count"] == 1


def test_coverage_counts_the_target_itself_in_the_population(chain_repo):
    """A leaf with no test is still risky to change; hiding that is dishonest."""
    (chain_repo / "tests" / "test_top.py").unlink()
    res = atlas.blast("tools/leaf.py")
    assert res["coverage"]["target_covered"] is False
    assert "tools/leaf.py" in res["coverage"]["uncovered"]


def test_coverage_is_declared_as_reachability_not_assertion_coverage(chain_repo):
    """The honest caveat travels WITH the number, not in a README nobody reads."""
    res = atlas.blast("tools/leaf.py")
    assert "reachability" in res["coverage"]["method"]
    assert "upper bound" in res["coverage"]["method"].lower()


def test_test_files_are_dependents_but_are_excluded_from_the_reach_term(chain_repo):
    res = atlas.blast("tools/leaf.py")
    assert res["transitive"]["test_count"] == 1
    assert res["risk"]["components"]["reach"]["raw"] == res["transitive"]["code_count"]


# --- risk score: reproducible from the payload alone --------------------------

def test_risk_score_recomputes_from_the_published_components(chain_repo):
    """A stranger with only the JSON must get the same number. No hidden inputs."""
    risk = atlas.blast("tools/leaf.py")["risk"]
    recomputed = 100.0 * sum(risk["weights"][k] * risk["components"][k]["normalized"]
                             for k in risk["weights"])
    assert abs(recomputed - risk["score"]) < 0.5


def test_risk_weights_sum_to_one_so_the_score_is_bounded_by_100():
    assert abs(sum(atlas.RISK_WEIGHTS.values()) - 1.0) < 1e-9


def test_every_risk_component_states_its_formula_and_its_reason(chain_repo):
    risk = atlas.blast("tools/leaf.py")["risk"]
    for name, comp in risk["components"].items():
        assert comp["formula"], f"{name} has no stated formula"
        assert comp["why"], f"{name} has no stated reason"
        assert 0.0 <= comp["normalized"] <= 1.0


def test_saturation_constants_are_measured_not_hardcoded(chain_repo):
    """The model calibrates on the repo it is pointed at, not on ours."""
    cal = atlas.blast("tools/leaf.py")["risk"]["calibration"]
    assert cal["files_measured"] > 0
    assert cal["percentile"] == atlas.CALIBRATION_PERCENTILE
    assert cal["reach_saturation"] >= 1 and cal["depth_saturation"] >= 1
    assert "percentile" in cal["method"]


def test_a_wide_radius_scores_strictly_higher_than_a_leaf(chain_repo):
    """The whole promise: the two cases must not read the same."""
    wide = atlas.blast("tools/leaf.py")
    narrow = atlas.blast("tools/apex.py")
    assert narrow["transitive"]["count"] == 0
    assert wide["risk_score"] > narrow["risk_score"]


def test_deeper_cascade_scores_higher_than_a_shallow_one_of_equal_width(tmp_path,
                                                                       monkeypatch):
    """Isolates the depth term: same dependent count, same repo, different depth.

    Both targets must live in ONE repo. The saturation constants are measured
    per-repo, so comparing a score from repo A against a score from repo B
    compares two different scales -- which is exactly the mistake this test made
    on its first pass, and it passed for the wrong reason.
    """
    (tmp_path / "tools").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "data").mkdir()
    t = tmp_path / "tools"
    t.joinpath("deep_target.py").write_text("X = 1" + chr(10), encoding="utf-8")
    t.joinpath("d1.py").write_text("from tools.deep_target import X" + chr(10), encoding="utf-8")
    t.joinpath("d2.py").write_text("from tools.d1 import X" + chr(10), encoding="utf-8")
    t.joinpath("d3.py").write_text("from tools.d2 import X" + chr(10), encoding="utf-8")
    t.joinpath("flat_target.py").write_text("Y = 1" + chr(10), encoding="utf-8")
    for n in ("f1", "f2", "f3"):
        t.joinpath(n + ".py").write_text(
            "from tools.flat_target import Y" + chr(10), encoding="utf-8")

    monkeypatch.setattr(atlas, "ROOT", tmp_path)
    monkeypatch.setattr(atlas, "INDEX_PATH", tmp_path / "data" / "idx.json")
    monkeypatch.setattr(atlas, "SKILLS_DIR", tmp_path / "skills")
    atlas._GRAPH_CACHE.clear()

    deep = atlas.blast("tools/deep_target.py")
    flat = atlas.blast("tools/flat_target.py")
    assert deep["transitive"]["count"] == flat["transitive"]["count"] == 3
    assert deep["transitive"]["max_depth"] == 3
    assert flat["transitive"]["max_depth"] == 1
    assert deep["risk"]["components"]["reach"]["normalized"] ==         flat["risk"]["components"]["reach"]["normalized"]
    assert deep["risk"]["components"]["depth"]["normalized"] >         flat["risk"]["components"]["depth"]["normalized"]
    assert deep["risk_score"] > flat["risk_score"]


def test_an_entrypoint_reference_raises_the_score_over_an_identical_non_entrypoint(
        chain_repo):
    """Same radius, but one file is named by a governing document."""
    plain = atlas.blast("tools/leaf.py")["risk_score"]
    (chain_repo / "CLAUDE.md").write_text(
        "# Steering" + chr(10) + "Run tools/leaf.py before publishing." + chr(10),
        encoding="utf-8")
    atlas._GRAPH_CACHE.clear()
    flagged = atlas.blast("tools/leaf.py")
    assert flagged["entrypoint"]["value"] == atlas.ENTRYPOINT_AUTHORITY
    assert flagged["entrypoint"]["authority_refs"] == ["CLAUDE.md"]
    assert flagged["risk_score"] > plain


# --- the gate now sees past direct importers ---------------------------------

def test_gate_blocks_a_wide_uncovered_radius_that_direct_fan_in_called_safe():
    """The measured miss: 5 direct importers, 244 transitive dependents, gate PASSED."""
    ok, msg = atlas.blast_gate({
        "ok": True, "path": "t.py", "tier": "MEDIUM", "tested": True, "fan_in": 5,
        "risk_score": 73.1, "risk_band": "CRITICAL",
        "transitive": {"count": 244},
        "coverage": {"uncovered_count": 10, "uncovered": ["a.py", "b.py"]},
    })
    assert ok is False
    assert "244 dependents" in msg and "a.py" in msg


def test_gate_passes_a_wide_radius_that_is_fully_covered():
    ok, _ = atlas.blast_gate({
        "ok": True, "path": "t.py", "tier": "MEDIUM", "tested": True, "fan_in": 5,
        "risk_score": 73.1, "risk_band": "CRITICAL",
        "transitive": {"count": 244},
        "coverage": {"uncovered_count": 0, "uncovered": []},
    })
    assert ok is True


# --- the cached path and the live path must answer identically ----------------

def test_cached_and_live_reference_scans_agree(fake_repo):
    """A cache that answers a different question than the scan it replaces is a lie.

    blast() reads the reference map out of the index; without an index the files
    are scanned live. Both routes build their records with the same functions,
    and this test is what keeps them from drifting.
    """
    index = atlas.build_index(force=True)
    cached = atlas._referencing_files("tools/target_tool.py", "target_tool", index=index)
    live = atlas._referencing_files("tools/target_tool.py", "target_tool")
    assert cached == live


def test_cached_and_live_reference_scans_agree(chain_repo):
    """The warm index must find exactly what a cold filesystem scan finds.

    Two code paths answer 'who references this': one walks the tree, one reads
    the cached index. If they ever disagree, the tool reports a different blast
    radius depending on whether a cache happened to be warm -- and the faster
    path is the one people will trust.

    Originally this ran only against the analyser's own repository, on a
    hardcoded filename. It is the same assertion against a generated tree.
    """
    index = atlas.build_index(force=True)
    target = "tools/leaf.py"
    cached = atlas._referencing_files(target, "leaf", index=index)
    live = atlas._referencing_files(target, "leaf")
    assert cached == live


def test_a_cold_run_with_no_index_produces_the_same_answer_as_a_warm_one(chain_repo):
    """The warm index is a speed trick. It must never change the measurement."""
    warm = atlas.blast("tools/leaf.py")
    atlas.INDEX_PATH.unlink(missing_ok=True)
    atlas._GRAPH_CACHE.clear()
    cold = atlas.blast("tools/leaf.py")
    for key in ("transitive", "coverage", "risk_score", "risk_band", "radius"):
        assert cold[key] == warm[key], f"{key} differs between a cold and a warm run"


def test_an_unrelated_data_json_change_does_not_reparse_every_python_file(chain_repo):
    """Measured defect: any data/*.json write invalidated the whole index.

    This repo has daemons writing state constantly, so the "warm" path re-read
    and re-parsed 3600+ files on most runs and measured SLOWER than a cold build.
    """
    atlas.build_index(force=True)
    calls = []
    real = atlas._summarize_python

    def counting(p, src=None):
        calls.append(atlas._rel(p))
        return real(p, src=src)

    (chain_repo / "data" / "unrelated_state.json").write_text('{"x": 1}', encoding="utf-8")
    monkeypatch_target = atlas._summarize_python
    atlas._summarize_python = counting
    try:
        atlas.build_index()
    finally:
        atlas._summarize_python = monkeypatch_target
    assert calls == [], f"re-parsed python files for a json-only change: {calls}"


def test_a_changed_python_file_is_reparsed_and_only_that_one(chain_repo):
    atlas.build_index(force=True)
    calls = []
    real = atlas._summarize_python

    def counting(p, src=None):
        calls.append(atlas._rel(p))
        return real(p, src=src)

    (chain_repo / "tools" / "mid.py").write_text(
        "from tools.leaf import go" + chr(10) + "Z = 2" + chr(10), encoding="utf-8")
    atlas._summarize_python = counting
    try:
        atlas.build_index()
    finally:
        atlas._summarize_python = real
    assert calls == ["tools/mid.py"]


# --- CLI surface: a documented command that errors out is a broken tool --------

def test_json_flag_works_AFTER_the_subcommand(chain_repo, capsys):
    """The module docstring documented `blast <path> --json`; argparse exited 2.

    --json was only defined on the top-level parser, so the documented usage was
    an unrecognized-arguments error. Anything reading this tool from CI hit it.
    """
    rc = atlas.main(["blast", "tools/leaf.py", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["path"] == "tools/leaf.py"


def test_json_flag_still_works_BEFORE_the_subcommand(chain_repo, capsys):
    rc = atlas.main(["--json", "blast", "tools/leaf.py"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_gate_flag_exits_2_on_a_violation(chain_repo, capsys):
    (chain_repo / "tests" / "test_top.py").unlink()
    (chain_repo / "CLAUDE.md").write_text("Run tools/leaf.py nightly." + chr(10),
                                          encoding="utf-8")
    atlas._GRAPH_CACHE.clear()
    rc = atlas.main(["blast", "tools/leaf.py", "--gate", "--json"])
    capsys.readouterr()
    assert rc == 2


def test_why_prints_the_chain_for_one_named_dependent(chain_repo, capsys):
    rc = atlas.main(["blast", "tools/leaf.py", "--why", "tools/apex.py", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["why"]["shortest_path"][0] == "tools/apex.py"
    assert payload["why"]["shortest_path"][-1] == "tools/leaf.py"


def test_why_on_a_file_outside_the_radius_says_so_instead_of_inventing_a_path(
        chain_repo, capsys):
    (chain_repo / "tools" / "stranger.py").write_text("X = 1" + chr(10), encoding="utf-8")
    rc = atlas.main(["blast", "tools/leaf.py", "--why", "tools/stranger.py", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["why"]["reached"] is False


def test_blast_json_is_stable_and_machine_readable(chain_repo, capsys):
    """A CI job consumes this. Keys sorted, schema versioned, values serialisable."""
    atlas.main(["blast", "tools/leaf.py", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["schema_version"] == atlas.BLAST_SCHEMA
    assert json.dumps(payload, sort_keys=True)          # no unserialisable values
    keys = list(payload.keys())
    assert keys == sorted(keys), "top-level keys must be emitted in a stable order"


def test_max_listed_caps_the_list_without_corrupting_the_counts(chain_repo):
    res = atlas.blast("tools/leaf.py", max_listed=1)
    assert len(res["transitive"]["dependents"]) == 1
    assert res["transitive"]["dependents_truncated"] is True
    assert res["transitive"]["count"] == 4       # the COUNT stays honest


# --- judged on a repo-scale tree, not a four-file toy --------------------------

@pytest.fixture(scope="module")
def real_graph(tmp_path_factory):
    """A synthetic tree at repository scale: a hub, a deep chain, and leaves.

    These four tests originally ran against the analyser's own home repository,
    which made them unrunnable anywhere else -- the properties they check (a hub
    separating from a leaf, a transitive radius dwarfing direct fan-in, risk
    bands that actually partition the tree) need hundreds of files and a real
    cascade, and a standalone package has neither.

    Generating the tree keeps the property under test and drops the coupling.
    The shape is deliberately uneven, because a uniform tree would let a
    degenerate scoring function pass 'the bands are not degenerate'.
    """
    root = tmp_path_factory.mktemp("scale_repo")
    (root / "tools").mkdir()
    (root / "tests").mkdir()

    # One hub that a large share of the tree imports.
    (root / "tools" / "hub.py").write_text(
        '"""Hub."""\ndef go():\n    return 1\n', encoding="utf-8")

    # A deep chain, so max_depth is genuinely large.
    (root / "tools" / "chain00.py").write_text(
        "from hub import go\n", encoding="utf-8")
    for i in range(1, 12):
        (root / "tools" / f"chain{i:02d}.py").write_text(
            f"from chain{i - 1:02d} import go\n", encoding="utf-8")

    # A wide fan of direct importers of the hub.
    for i in range(40):
        (root / "tools" / f"widget{i:02d}.py").write_text(
            "from hub import go\n", encoding="utf-8")

    # Mid-level modules importing the widgets, to build second-order reach.
    for i in range(30):
        (root / "tools" / f"mid{i:02d}.py").write_text(
            f"from widget{i % 40:02d} import go\n", encoding="utf-8")

    # Pure leaves: nothing imports them, and they import nothing in-tree.
    for i in range(60):
        (root / "tools" / f"leaf{i:02d}.py").write_text(
            '"""Leaf."""\nimport json\ndef run():\n    return json\n', encoding="utf-8")

    # Tests covering part of the tree, so coverage is neither 0% nor 100%.
    for i in range(15):
        (root / "tests" / f"test_widget{i:02d}.py").write_text(
            f"from widget{i:02d} import go\ndef test_go():\n    assert go()\n",
            encoding="utf-8")
    # Most leaves are tested. Without this the tree has no genuinely low-risk
    # files at all -- an untested leaf still carries the full uncovered term --
    # and 'the bands are not degenerate' would fail for a reason that says
    # nothing about the scale, only about how the fixture was generated.
    for i in range(48):
        (root / "tests" / f"test_leaf{i:02d}.py").write_text(
            f"from leaf{i:02d} import run\ndef test_run():\n    assert run()\n",
            encoding="utf-8")

    atlas.set_root(root)
    atlas._GRAPH_CACHE.clear()
    graph = atlas.build_dep_graph(atlas.build_index(force=True))
    yield graph
    atlas._GRAPH_CACHE.clear()


def test_the_repo_hub_and_a_repo_leaf_do_not_read_the_same(real_graph):
    """The promise, measured on real files rather than asserted about a fixture."""
    reach = {}
    for node in real_graph["forward"]:
        tr = atlas.transitive_dependents(real_graph, node)
        reach[node] = tr["count"]
    hub = max(reach, key=lambda k: reach[k])
    leaves = sorted(k for k, v in reach.items() if v == 0)
    assert reach[hub] > 50, f"no hub found in this repo (max was {reach[hub]})"
    assert leaves, "no leaf found in this repo"
    hub_res = atlas.blast(hub)
    leaf_res = atlas.blast(leaves[0])
    assert hub_res["transitive"]["count"] > 10 * max(1, leaf_res["transitive"]["count"])
    assert hub_res["risk_score"] > leaf_res["risk_score"]


def test_a_real_hub_has_far_more_transitive_dependents_than_direct_importers(real_graph):
    """The specific weakness: direct fan-in under-reports a hub by an order of magnitude."""
    gaps = []
    for node in real_graph["forward"]:
        tr = atlas.transitive_dependents(real_graph, node)
        if tr["direct_count"] >= 1 and tr["count"] >= 10 * tr["direct_count"]:
            gaps.append((node, tr["direct_count"], tr["count"], tr["max_depth"]))
    assert gaps, "expected at least one file whose transitive radius dwarfs its fan-in"
    assert any(g[3] >= 3 for g in gaps), "expected at least one cascade deeper than 2 hops"


def test_risk_bands_are_not_degenerate(real_graph):
    """Cut points are only defensible if they actually partition the repo.

    A scale that calls everything CRITICAL, or nothing, carries no information.
    """
    bands = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    empty_hits = {"docs": [], "registries": [], "skills": []}
    for node in real_graph["forward"]:
        tr = atlas.transitive_dependents(real_graph, node)
        cov = atlas.radius_coverage(real_graph, node, tr)
        ep = atlas._entrypoint_signal(dict(empty_hits))
        risk = atlas.compute_risk(tr, cov, ep, real_graph["calibration"])
        bands[risk["band"]] += 1
    total = sum(bands.values())
    assert total > 100
    assert bands["LOW"] / total > 0.2, "the scale collapsed upward"
    assert (bands["CRITICAL"] + bands["HIGH"]) / total < 0.5, "the scale collapsed downward"


def test_calibration_is_measured_from_this_repo(real_graph):
    cal = real_graph["calibration"]
    assert cal["files_measured"] == len(real_graph["nodes"])
    assert cal["max_reach_observed"] >= cal["reach_saturation"]
    assert cal["max_depth_observed"] >= cal["depth_saturation"]


# --- resolution rules --------------------------------------------------------

@pytest.mark.parametrize("values,pct,expected", [
    ([], 0.95, 0),
    ([5], 0.95, 5),
    ([0, 0, 0, 10], 0.95, 10),
    ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5, 6),
])
def test_percentile_is_nearest_rank_and_deterministic(values, pct, expected):
    assert atlas._percentile(values, pct) == expected


def test_an_ambiguous_bare_import_resolves_to_one_file_and_says_which(chain_repo):
    """Two files share a stem. Adding an edge to BOTH would invent a blast radius."""
    (chain_repo / "tools" / "dup.py").write_text("A = 1" + chr(10), encoding="utf-8")
    (chain_repo / "tests" / "dup.py").write_text("A = 2" + chr(10), encoding="utf-8")
    (chain_repo / "tools" / "imports_dup.py").write_text("import dup" + chr(10),
                                                         encoding="utf-8")
    atlas._GRAPH_CACHE.clear()
    graph = atlas.build_dep_graph(atlas.build_index(force=True))
    assert "dup" in graph["ambiguous_stems"]
    assert graph["forward"]["tools/imports_dup.py"] == {"tools/dup.py": "import"}
    res = atlas.blast("tools/dup.py")
    assert res["graph_stats"]["ambiguous_stems"] >= 1


def test_entrypoint_is_graded_not_binary():
    authority = atlas._entrypoint_signal(
        {"docs": ["CLAUDE.md"], "registries": [], "skills": []})
    machine = atlas._entrypoint_signal(
        {"docs": [], "registries": ["data/workers.json"], "skills": ["skynet-blog"]})
    none = atlas._entrypoint_signal({"docs": ["docs/NOTES.md"], "registries": [],
                                     "skills": []})
    assert authority["value"] == atlas.ENTRYPOINT_AUTHORITY
    assert machine["value"] == atlas.ENTRYPOINT_MACHINE_ROUTE
    assert none["value"] == 0.0
    assert authority["authority_refs"] == ["CLAUDE.md"]


def test_blast_on_a_non_python_file_reports_an_empty_graph_not_a_crash(chain_repo):
    (chain_repo / "NOTES.md").write_text("# notes" + chr(10), encoding="utf-8")
    res = atlas.blast("NOTES.md")
    assert res["ok"] is True
    assert res["transitive"]["count"] == 0
    assert "not a python file" in res["transitive"]["note"]


def test_an_index_from_another_checkout_is_discarded(chain_repo):
    """A cache keyed to a different root is not evidence about this one."""
    atlas.build_index(force=True)
    stale = json.loads(atlas.INDEX_PATH.read_text(encoding="utf-8"))
    stale["root"] = "D:/some/other/repo"
    atlas.INDEX_PATH.write_text(json.dumps(stale), encoding="utf-8")
    assert atlas._load_cached_index() is None
    rebuilt = atlas.build_index()
    assert rebuilt["root"] == str(chain_repo)


def test_an_older_index_schema_is_discarded_whole(chain_repo):
    atlas.build_index(force=True)
    stale = json.loads(atlas.INDEX_PATH.read_text(encoding="utf-8"))
    stale["schema_version"] = atlas.INDEX_SCHEMA - 1
    atlas.INDEX_PATH.write_text(json.dumps(stale), encoding="utf-8")
    assert atlas._load_cached_index() is None


def test_adding_a_new_module_refreshes_mentions_for_files_that_did_not_change(chain_repo):
    """Incremental caching must not freeze the stem universe.

    tools/mid.py does not change when tools/leaf2.py appears, but it now mentions
    a module that exists. A per-file mtime cache alone would never notice.
    """
    atlas.build_index(force=True)
    (chain_repo / "tools" / "mentions_future.py").write_text(
        "CMD = 'python tools/leaf2.py --json'" + chr(10), encoding="utf-8")
    atlas.build_index()
    (chain_repo / "tools" / "leaf2.py").write_text("Y = 1" + chr(10), encoding="utf-8")
    atlas._GRAPH_CACHE.clear()
    res = atlas.blast("tools/leaf2.py")
    assert "tools/mentions_future.py" in [d["path"] for d in res["transitive"]["dependents"]]
