#!/usr/bin/env python3
"""Skynet AI File Blast Radius -- what breaks if I change this file, and is it tested?

WHY THIS EXISTS
---------------
Before you edit a file you need two numbers that almost no repo can produce:

  1. How much of the codebase can this break?  Not "who imports it" -- the FULL
     transitive dependent set. Measured on this repo, tools/skynet_advisor_ladder.py
     has 5 direct importers and 244 transitive dependents reached at depth 9.
     Counting direct importers reports 5 and calls it a leaf. That is the defect
     this tool exists to remove.
  2. Is that radius covered by tests?  A wide radius with tests behind it is
     recoverable. A wide radius with none is where the regression lands.

This tool answers, deterministically, with zero model budget:

    find   -- where does the named thing actually live?
    map    -- what IS this file: entrypoints, symbols, tests, registrations?
    blast  -- transitive dependents + depth + shortest path + coverage + risk score
    pack   -- the minimal high-signal packet to UPLOAD to the CDP advisors
    md     -- which markdown is authority, which is orphan weight

HOW THE DEPENDENCY GRAPH IS BUILT (read this before trusting a number)
---------------------------------------------------------------------
Nodes are the python files under tools/, tests/ and Skynet/. Two typed edge
kinds, both extracted from the AST -- never from raw text, because raw text
counts comments and documentation as dependencies:

  * import -- an Import / ImportFrom node whose module resolves to a repo file.
    All four real forms count: "import x", "from x import y",
    "from pkg.x import y", and "from pkg import x". The last one is a real
    import that a bare-module regex misses.
  * path -- a string CONSTANT (never a comment, never a docstring) that names a
    repo file, e.g. str(ROOT / "tools" / "publish_post.py") or
    "python tools/x.py --json". That is real coupling: rename the target and the
    caller breaks.

Three exclusions keep the graph honest. Each one was earned from a false edge
measured on this repo, not guessed at:

  * Docstrings are excluded. tools/skynet_post_structure.py documents
    publish_post.py in its module docstring; that is documentation, not a
    dependency, and counting it inverted the true edge direction.
  * String constants longer than PATH_LITERAL_MAX_CHARS, or containing a
    newline, are excluded. tools/skynet_transfer_bundle.py embeds a whole
    PowerShell installer as one python string; a comment INSIDE that payload
    mentioned publish_post.py and manufactured a dependency that does not exist.
  * Traversal STOPS at test files. A test is a leaf consumer -- nothing in
    production depends on a test -- so expanding through one produced chains
    like test_pid_guard -> test_daemon_singleton -> skynet_watchdog, which is
    not a cascade anybody can be hurt by. Tests are still recorded as
    dependents, and they are the coverage signal.

Cycles are safe: the traversal is a BFS with a visited set, so a cycle
terminates and nothing is counted twice. Depth is the SHORTEST edge distance
from the target, and every dependent carries the shortest path that reaches it.

Usage:
    python tools/skynet_code_atlas.py find "advisor ladder"
    python tools/skynet_code_atlas.py map tools/skynet_advisor_ladder.py
    python tools/skynet_code_atlas.py blast tools/blog_cdp_session.py --json
    python tools/skynet_code_atlas.py blast tools/publish_post.py --gate
    python tools/skynet_code_atlas.py pack tools/skynet_site_nav.py --question q
    python tools/skynet_code_atlas.py md --json

Exit codes: 0 ok, 2 gate violation (fail closed), 3 usage/not-found.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
from collections import deque
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def _discover_root() -> Path:
    """Locate the tree to analyse.

    Order: BLAST_RADIUS_ROOT, then the nearest enclosing git working tree, then
    the current directory. The original implementation hardcoded its own
    repository as `Path(__file__).parents[1]`, which is correct for a tool that
    lives inside the tree it measures and useless for one installed from PyPI.
    """
    override = os.environ.get("BLAST_RADIUS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return here


def set_root(path) -> Path:
    """Re-point the analyser at a different tree, rebinding derived paths."""
    global ROOT, INDEX_PATH
    ROOT = Path(path).expanduser().resolve()
    INDEX_PATH = ROOT / ".blast_radius_index.json"
    return ROOT


ROOT = _discover_root()
INDEX_PATH = ROOT / ".blast_radius_index.json"

# Optional integrations. Absent in an ordinary repository, and their absence is
# a supported configuration rather than a degraded one: every consumer of these
# checks existence first.
REGISTRY_PATH = ROOT / os.environ.get("BLAST_RADIUS_REGISTRY", "data/registry.json")
SKILLS_DIR = Path(os.environ.get("BLAST_RADIUS_SKILLS_DIR",
                                 Path.home() / ".claude" / "skills"))

# Bumped whenever the cached index gains a field the reader depends on. A cache
# written by an older schema is DISCARDED, never half-trusted -- a stale field
# read as fresh is how a measurement tool starts lying.
INDEX_SCHEMA = 2
BLAST_SCHEMA = 2

# Directories that are build output, vendor code, or frozen history. Indexing
# them buries the signal we actually need.
EXCLUDED_PARTS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", ".pytest_cache", ".mypy_cache", ".tox", ".eggs",
    "site-packages", "vendor", "third_party", "worktrees",
}
EXCLUDED_PREFIXES = ("_archive", ".")

# Directories scanned for source. Kept broad so the analyser is useful on a
# repository laid out in any of the common ways, not only this one.
CODE_DIRS = tuple(
    d for d in os.environ.get(
        "BLAST_RADIUS_CODE_DIRS",
        "src,lib,app,tools,tests,test,scripts,pkg,internal,.",
    ).split(",") if d
)
DOC_DIRS = ("docs", "doc")
TEST_PREFIX = os.environ.get("BLAST_RADIUS_TEST_PREFIX", "tests/")

# A file referenced by these is load-bearing: a break there is a live outage,
# not a unit-test failure.
CRITICAL_REFERRERS = tuple(
    d for d in os.environ.get(
        "BLAST_RADIUS_AUTHORITY_DOCS",
        "README.md,ARCHITECTURE.md,CONTRIBUTING.md,SPEC.md,AGENTS.md,CLAUDE.md",
    ).split(",") if d
)

MAX_BRIEF_BYTES = 12000
MAX_PACK_FILE_BYTES = 60000

# A path used as an argument is short and single-line ("tools/x.py",
# "python tools/x.py --json"). Anything longer is a payload -- an embedded
# script, a template, a prompt -- and the paths inside it are content, not calls.
PATH_LITERAL_MAX_CHARS = 200

_PATH_LITERAL_RE = re.compile(r"[\w./" + chr(92) + r"-]*\w+[.]py")
# [A-Za-z0-9_]+ rather than [A-Za-z_]\w* so that "1foo" tokenises as ONE token.
# That makes (stem in tokens) exactly equivalent to a word-boundary regex search
# for the stem, which is what lets the cached path and the live-scan path stay
# provably identical -- see test_cached_and_live_reference_scans_agree.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


# ---------------------------------------------------------------- risk model
#
#   score = 100 * (Wr*reach + Wu*uncovered + Wd*depth + We*entrypoint)
#
# Every input is printed in the JSON, so a stranger can recompute the score by
# hand from the payload alone. The two saturation constants are NOT invented:
# they are measured from the repo being analysed at graph-build time (see
# _calibrate), so the model self-calibrates instead of importing our numbers.
RISK_WEIGHTS = {
    # How many files can break. The primary term: it is the size of the accident.
    "reach": 0.35,
    # Whether a break is caught before it ships. Nearly as heavy as size, because
    # a covered radius is recoverable and an uncovered one is not.
    "uncovered": 0.30,
    # How far the cascade travels. This is the term that direct-importer counting
    # misses entirely. Below reach because depth without breadth is a chain, not
    # a blast.
    "depth": 0.20,
    # Consequence multiplier: breaking a registered entrypoint is a live outage,
    # not a unit-test failure. Smallest weight because it says nothing about size.
    "entrypoint": 0.15,
}

# Cut points, stated so CI can reproduce them. Verified against the measured
# score distribution of this repo by test_risk_bands_are_not_degenerate:
# CRITICAL stays a small minority and LOW covers the large leaf population.
RISK_BANDS = (("CRITICAL", 70.0), ("HIGH", 45.0), ("MEDIUM", 20.0), ("LOW", 0.0))

# Saturation is read off the 95th percentile of the repo being analysed. Reason
# for 95 and not 100: the maximum is a single pathological hub, and normalising
# by an outlier squashes every real file into the bottom of the scale.
CALIBRATION_PERCENTILE = 0.95

# An entrypoint reference is graded, not binary: a governing document naming the
# file means a documented human procedure runs it, while a registry or skill
# naming it means a machine route runs it. Half credit for the second, because
# it carries most -- not all -- of the consequence.
ENTRYPOINT_AUTHORITY = 1.0
ENTRYPOINT_MACHINE_ROUTE = 0.5

RISK_FORMULA = ("score = 100 * (0.35*reach + 0.30*uncovered + 0.20*depth"
                " + 0.15*entrypoint), each term in [0,1]")


# ---------------------------------------------------------------- utilities

def _rel(p: Path) -> str:
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def _excluded(p: Path) -> bool:
    try:
        parts = p.relative_to(ROOT).parts if p.is_absolute() else p.parts
    except ValueError:
        parts = p.parts
    for part in parts:
        if part in EXCLUDED_PARTS:
            return True
        if part.startswith(EXCLUDED_PREFIXES):
            return True
    return False


def _read(p: Path, limit: int | None = None) -> str:
    try:
        data = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return data[:limit] if limit else data


def _norm(p) -> str:
    """Case-folded absolute path as a plain string.

    Deliberately abspath, not resolve(): resolve() hits the filesystem once per
    file, and the enumeration runs over thousands of files on every command.
    """
    return os.path.normcase(os.path.abspath(str(p)))


def _iter_files(patterns: tuple[str, ...], root: Path | None = None):
    # Resolve ROOT at call time, not as a default argument: a default binds at
    # import and silently freezes the root for every later caller.
    root = root or ROOT
    skip = _norm(INDEX_PATH)
    for pattern in patterns:
        for p in root.glob(pattern):
            if p.is_file() and not _excluded(p) and _norm(p) != skip:
                yield p


def _is_generated_index(p: Path) -> bool:
    """The atlas index lists every path in the repo.

    Left in the reference scan it makes EVERY file look referenced -- it reported
    0 orphans out of 1512 proposals, because the only thing citing them was the
    index this tool had just written. A tool must never count its own output as
    evidence.
    """
    try:
        return _norm(p) == _norm(INDEX_PATH)
    except Exception:
        return False


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text))


def _percentile(values, pct: float) -> int:
    """Nearest-rank percentile: ordered[round_half_up(pct * (n-1))].

    Round HALF UP explicitly rather than through round(), which uses banker
    rounding: round(4.5) is 4 in python. A reader recomputing the saturation
    constant by hand would land on a different element, and a number a stranger
    cannot reproduce is not a defensible number.
    """
    ordered = sorted(values)
    if not ordered:
        return 0
    idx = int(pct * (len(ordered) - 1) + 0.5)
    return ordered[min(len(ordered) - 1, max(0, idx))]


# ---------------------------------------------------------------- static facts

def _module_name(p: Path) -> str:
    return p.stem


def _docstring_node_ids(tree: ast.AST) -> set:
    """ids of every Constant node that is a docstring (module/class/function).

    Needed because a path named in a docstring is DOCUMENTATION, not a call.
    Counting docstrings produced a live false edge on this repo:
    tools/skynet_post_structure.py documents publish_post.py and was reported as
    one of its dependents, which is the true edge pointing backwards.
    """
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


def _relative_package_prefix(path: Path, level: int) -> str:
    """Dotted package name that `level` leading dots resolve to, for `path`.

    `from ..core import x` inside `src/pkg/sub/mod.py` resolves against
    `src/pkg`, so this returns "pkg" (one dot = the file's own package, each
    additional dot walks up one more).
    """
    try:
        parts = list(path.resolve().parent.parts)
    except OSError:
        return ""
    if level > 1:
        parts = parts[:-(level - 1)] if level - 1 < len(parts) else []
    return parts[-1] if parts else ""


def _import_module_tokens(node: ast.AST, path: Path | None = None) -> list:
    """Every module string an import node could be naming.

    "from pkg import x" yields both "pkg" and "pkg.x" -- the second is the one
    that matters, and it is exactly the form a bare-module regex cannot see.

    RELATIVE IMPORTS. `from .transport import WebSocket` has `node.module ==
    "transport"` and `node.names == ["WebSocket"]`. An earlier version returned
    only the names for any relative import, so the tokens were the SYMBOLS and
    the module was never emitted -- meaning no edge could ever match and every
    intra-package dependency was invisible. On a repository laid out as a normal
    Python package that is most of the graph: measured on one, a module imported
    by four siblings reported zero dependents.

    So the module component is always emitted, and where the file path is known
    the dotted form anchored at the containing package is emitted too.
    """
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []

    if node.module and node.level == 0:
        return [node.module] + [node.module + "." + a.name for a in node.names]

    tokens: list = []
    if node.module:
        # `from .transport import X` -- "transport" is the module being depended on.
        tokens.append(node.module)
        tokens.extend(node.module + "." + a.name for a in node.names)
    else:
        # `from . import transport` -- here the NAMES are the modules.
        tokens.extend(a.name for a in node.names)

    if path is not None:
        prefix = _relative_package_prefix(path, node.level)
        if prefix:
            if node.module:
                tokens.append(prefix + "." + node.module)
            else:
                tokens.extend(prefix + "." + a.name for a in node.names)
    return tokens


def _summarize_python(p: Path, src: str | None = None) -> dict:
    """Static facts about a python file: docstring, symbols, argparse surface, deps.

    Uses ast rather than regex so a commented-out def never becomes a symbol, a
    subcommand string in a docstring never becomes a real subcommand, and a path
    inside a comment never becomes a dependency edge.
    """
    src = _read(p) if src is None else src
    info = {
        "summary": "",
        "symbols": [],
        "subcommands": [],
        "flags": [],
        "imports": [],
        "dep_modules": [],
        "dep_paths": [],
        "parse_error": None,
    }
    if not src:
        return info
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        info["parse_error"] = f"line {exc.lineno}: {exc.msg}"
        return info

    doc = ast.get_docstring(tree) or ""
    info["summary"] = doc.strip().splitlines()[0].strip() if doc.strip() else ""

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                info["symbols"].append(node.name)
        elif isinstance(node, ast.ClassDef):
            info["symbols"].append(node.name)

    doc_ids = _docstring_node_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for mod in _import_module_tokens(node, p):
                info["dep_modules"].append(mod)
                info["imports"].append(mod.split(".")[0])
        elif isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name == "add_parser" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    info["subcommands"].append(first.value)
            elif name == "add_argument" and node.args:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                            and arg.value.startswith("--"):
                        info["flags"].append(arg.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc_ids:
            value = node.value
            if len(value) <= PATH_LITERAL_MAX_CHARS and chr(10) not in value:
                info["dep_paths"].extend(_PATH_LITERAL_RE.findall(value))

    for key in ("symbols", "subcommands", "flags", "imports", "dep_modules", "dep_paths"):
        info[key] = sorted(set(info[key]))
    return info


# ---------------------------------------------------------------- inventory

def _inventory():
    """Every file the atlas looks at, as (path, rel, kind, in_entries).

    kind is one of py / md / json / cmd / skill. in_entries is False for files
    that live outside the repo tree (skills) or are pure operator surface
    (slash commands): they are valid REFERRERS but must never pollute find(),
    which would start returning paths that are not in this repository.
    """
    seen = set()
    out = []

    def add(p: Path, kind: str, in_entries: bool):
        rel = _rel(p)
        if rel in seen:
            return
        seen.add(rel)
        out.append((p, rel, kind, in_entries))

    for d in CODE_DIRS:
        for p in _iter_files((d + "/**/*.py",)):
            add(p, "py", True)
    for d in DOC_DIRS:
        for p in _iter_files((d + "/**/*.md",)):
            add(p, "md", True)
    for p in _iter_files(("*.md",)):
        add(p, "md", True)
    for p in _iter_files(("data/*.json",)):
        add(p, "json", True)
    for p in _iter_files((".claude/commands/*.md",)):
        add(p, "cmd", False)
    if SKILLS_DIR.exists():
        for p in sorted(SKILLS_DIR.glob("**/*.md")):
            if p.is_file():
                add(p, "skill", False)
    return out


def _skill_label(p: Path) -> str:
    """A skill is identified by its directory when the file is the SKILL.md."""
    return p.name if p.name != "SKILL.md" else p.parent.name


def _import_stems(entry: dict, stem_set: set) -> list:
    """Module stems this file imports, restricted to stems that exist in-repo.

    Last-component matching mirrors what a human means by "it imports X": both
    "import pkg.X" and "from pkg.X import y" are imports of X. "from pkg import X"
    is too -- that form is why the reference scanner previously under-reported
    fan-in, because a bare-module regex cannot see it.
    """
    out = set()
    for mod in entry.get("dep_modules") or []:
        last = mod.split(".")[-1]
        if last in stem_set:
            out.add(last)
    return sorted(out)


def _ref_record(rel: str, kind: str, text: str, stem_set: set,
                entry: dict | None, label: str = "") -> dict:
    """The reference-scan facts for one file: what it mentions, what it imports.

    Both the cached path and the live-scan path call THIS function, so the two
    cannot drift apart -- a cache that answers a different question from the
    scan it replaces is a silent measurement bug.
    """
    rec = {
        "path": rel,
        "kind": kind,
        "m": sorted(_tokens(text) & stem_set),
        "i": _import_stems(entry, stem_set) if (kind == "py" and entry) else [],
    }
    if label:
        rec["label"] = label
    return rec


# ---------------------------------------------------------------- index

def _load_cached_index() -> dict | None:
    try:
        cached = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(cached, dict) or cached.get("schema_version") != INDEX_SCHEMA:
        return None       # an older schema is discarded whole, never half-read
    if cached.get("root") != str(ROOT):
        return None       # a cache from another checkout is not evidence here
    return cached


def build_index(force: bool = False) -> dict:
    """Build (or incrementally refresh) the atlas index.

    Incremental on purpose. The first version re-parsed all 3600+ files whenever
    ANY data/*.json changed, and in a repo with live daemons writing state that
    is most runs -- the "warm" path measured SLOWER than a cold build. Here a
    file is re-read only when its own mtime or size moved.

    Correctness on a cold run is not traded away for that speed: with no cache
    every file is parsed, and the reference map is recomputed in full whenever
    the set of module stems changes, because a newly added module can be
    mentioned by files that did not themselves change.
    """
    inv = _inventory()
    stems = sorted({Path(rel).stem for _, rel, kind, _ in inv if kind == "py"})
    stem_set = set(stems)
    stems_hash = hashlib.sha1(chr(10).join(stems).encode("utf-8")).hexdigest()

    cached = None if force else _load_cached_index()
    cached_entries = (cached or {}).get("entries") or {}
    cached_refs = ((cached or {}).get("refs") or {}).get("files") or {}
    stems_ok = bool(cached) and ((cached or {}).get("refs") or {}).get("stems_hash") == stems_hash

    entries: dict = {}
    refs: dict = {}
    changed = not cached or not stems_ok
    newest = 0.0

    for p, rel, kind, in_entries in inv:
        try:
            st = p.stat()
        except OSError:
            continue
        mtime = round(st.st_mtime, 6)
        size = st.st_size
        newest = max(newest, st.st_mtime)

        old_entry = cached_entries.get(rel)
        old_ref = cached_refs.get(rel)
        entry_ok = (not in_entries) or (
            old_entry is not None and old_entry.get("mtime") == mtime
            and old_entry.get("bytes") == size)
        ref_ok = stems_ok and old_ref is not None \
            and old_ref.get("mtime") == mtime and old_ref.get("bytes") == size
        if entry_ok and ref_ok:
            if in_entries:
                entries[rel] = old_entry
            refs[rel] = old_ref
            continue

        changed = True
        text = _read(p)
        entry = {"path": rel, "bytes": size, "mtime": mtime,
                 "kind": p.suffix.lstrip(".")}
        py_facts = None
        if kind == "py":
            py_facts = _summarize_python(p, src=text)
            entry.update(py_facts)
        elif kind in ("md", "cmd", "skill"):
            head = text[:4000].strip().splitlines()
            title = next((l.lstrip("# ").strip() for l in head if l.startswith("#")), "")
            entry["summary"] = title or (head[0].strip() if head else "")
        if in_entries:
            entries[rel] = entry
        rec = _ref_record(rel, kind, text, stem_set, py_facts,
                          label=_skill_label(p) if kind == "skill" else "")
        rec["mtime"] = mtime
        rec["bytes"] = size
        refs[rel] = rec

    if cached and not changed:
        if set(cached_entries) == set(entries) and set(cached_refs) == set(refs):
            return cached       # nothing moved: keep the original generated stamp

    index = {
        "schema_version": INDEX_SCHEMA,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": str(ROOT),
        "file_count": len(entries),
        "newest_mtime": newest,
        "entries": entries,
        "refs": {"stems_hash": stems_hash, "stems": stems, "files": refs},
    }
    try:
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        INDEX_PATH.write_text(json.dumps(index), encoding="utf-8")
    except Exception:
        pass  # an unwritable cache degrades speed, never correctness
    return index


# ---------------------------------------------------------------- find

def find(query: str, limit: int = 12, index: dict | None = None) -> list:
    """Rank indexed files against a free-text query.

    Scoring is deliberately boring: exact stem beats path substring beats
    summary/symbol hit. Every term must land somewhere or the file is dropped,
    which is what keeps a 2500-file index from returning 200 maybes.
    """
    index = index or build_index()
    terms = [t for t in re.split(r"[^A-Za-z0-9_.]+", query.lower()) if t]
    if not terms:
        return []

    results = []
    for rel, entry in index["entries"].items():
        stem = Path(rel).stem.lower()
        path_l = rel.lower()
        summary = (entry.get("summary") or "").lower()
        symbols = " ".join(entry.get("symbols") or []).lower()
        haystack = f"{path_l} {summary} {symbols}"

        if not all(t in haystack for t in terms):
            continue

        score = 0
        for t in terms:
            if stem == t:
                score += 100
            elif t in stem:
                score += 40
            if t in path_l:
                score += 10
            if t in summary:
                score += 8
            if t in symbols:
                score += 5
        if rel.startswith("tools/"):
            score += 6      # the executable surface is usually what was meant
        elif rel.startswith(TEST_PREFIX):
            score += 2
        results.append({
            "path": rel,
            "score": score,
            "summary": entry.get("summary", ""),
            "kind": entry.get("kind", ""),
        })

    results.sort(key=lambda r: (-r["score"], r["path"]))
    return results[:limit]


# ---------------------------------------------------------------- references

def _classify_reference(rec: dict, stem: str, hits: dict) -> None:
    """Sort one referring file into the radius buckets. Single source of truth.

    The cached path and the live-scan path both funnel through here, so the two
    can never answer differently.
    """
    rel = rec["path"]
    kind = rec["kind"]
    imports_hit = stem in rec.get("i", ())
    mention_hit = stem in rec.get("m", ())
    if kind == "py":
        if imports_hit:
            hits["imports"].append(rel)
        elif mention_hit:
            hits["cli_callers"].append(rel)
        if rel.startswith(TEST_PREFIX) and (imports_hit or mention_hit):
            hits["tests"].append(rel)
        return
    if not mention_hit:
        return
    if kind == "json":
        hits["registries"].append(rel)
    elif kind == "skill":
        hits["skills"].append(rec.get("label") or rel)
    else:
        hits["docs"].append(rel)


def _referencing_files(target_rel: str, stem: str, index: dict | None = None) -> dict:
    """Every file that reaches the target: python import, CLI string, or doc mention.

    The import test MUST tolerate a dotted package prefix. This repo imports the
    same module both ways -- "from blog_cdp_session import ensure_guard" and
    "from tools.blog_cdp_session import CDPSession" -- and matching only the bare
    form under-reported one file fan-in as 2 when it was really 100+.

    With an index the answer comes from the cached reference map; without one the
    files are scanned live. Both routes build the same records with the same
    functions, and test_cached_and_live_reference_scans_agree locks that.
    """
    hits = {"imports": [], "cli_callers": [], "tests": [],
            "docs": [], "registries": [], "skills": []}
    stem_set = {stem}

    cached = ((index or {}).get("refs") or {}).get("files")
    if cached:
        for rel, rec in cached.items():
            if rel == target_rel:
                continue
            _classify_reference(rec, stem, hits)
    else:
        for p, rel, kind, _ in _inventory():
            if rel == target_rel:
                continue
            text = _read(p)
            if not text or stem not in _tokens(text):
                # Provably equivalent to scanning it: an import of the stem, or a
                # path naming it, both require the stem to appear as a token. So a
                # file without that token can contribute nothing to any bucket,
                # and skipping it here avoids parsing 1200 files to answer about 1.
                continue
            facts = _summarize_python(p, src=text) if kind == "py" else None
            rec = _ref_record(rel, kind, text, stem_set, facts,
                              label=_skill_label(p) if kind == "skill" else "")
            _classify_reference(rec, stem, hits)

    for k in hits:
        hits[k] = sorted(set(hits[k]))
    return hits


# ---------------------------------------------------------------- dep graph

_GRAPH_CACHE: dict = {}

# A dotted import only resolves through a package prefix that is really a
# directory in this repo. Without that guard "import os.path" would resolve to a
# repo file named path.py and invent an edge to the standard library.
_PACKAGE_ROOTS = set(CODE_DIRS)


def _stem_map(rels) -> dict:
    """stem -> the single file that name resolves to, plus the ambiguous ones.

    A bare "import convene_gate" when two files carry that stem is genuinely
    ambiguous. Adding an edge to BOTH manufactures a blast radius that does not
    exist, so one candidate is chosen deterministically (tools before Skynet
    before tests, then shortest path, then alphabetical) and the ambiguity is
    reported in the payload rather than hidden.
    """
    buckets: dict = {}
    for rel in rels:
        buckets.setdefault(Path(rel).stem, []).append(rel)

    def rank(rel: str):
        top = rel.split("/")[0]
        order = {"tools": 0, "Skynet": 1, "tests": 2}.get(top, 3)
        return (order, rel.count("/"), rel)

    chosen = {}
    ambiguous = {}
    for stem, candidates in buckets.items():
        ordered = sorted(candidates, key=rank)
        chosen[stem] = ordered[0]
        if len(ordered) > 1:
            ambiguous[stem] = ordered
    return chosen, ambiguous


def _module_map(rels) -> tuple:
    """Every dotted name each file could legitimately be imported as.

    `src/pkg/mod.py` is `pkg.mod` when `src/` is on sys.path -- the standard
    "src layout" that packaging tools generate -- and `src.pkg.mod` when the repo
    root is. Both are produced by dropping leading path components, so resolution
    does not depend on the analyser knowing which layout a project uses.

    Without this, a dotted import only resolved when the module's dotted name
    matched its path from the repo root exactly. On a src-layout project that is
    never true, so every package-absolute import failed to resolve and the files
    appeared to have no dependents and no test coverage at all.

    A dotted name produced by two different files is dropped rather than guessed:
    a wrong edge is worse than a missing one in a tool used to decide what is
    safe to change.
    """
    mapping: dict = {}
    ambiguous: set = set()
    for rel in rels:
        if not rel.endswith(".py"):
            continue
        parts = rel[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        for start in range(len(parts)):
            dotted = ".".join(parts[start:])
            if "." not in dotted:
                continue  # bare stems belong to the stem map, with its own rules
            if dotted in mapping and mapping[dotted] != rel:
                ambiguous.add(dotted)
            else:
                mapping.setdefault(dotted, rel)
    for dotted in ambiguous:
        mapping.pop(dotted, None)
    return mapping, ambiguous


def _resolve_module(mod: str, stem_map: dict, rel_set: set,
                    module_map: dict | None = None) -> str | None:
    """Map an imported module string onto a repo file, or None.

    Precision over recall: a dotted module resolves by its real path, by any
    source-root-relative form of that path, or as a package `__init__`. A bare
    name resolves by stem. Anything else -- os.path, json.decoder -- resolves to
    nothing, which is correct: the standard library is not part of a blast radius.
    """
    if not mod:
        return None
    if "." not in mod:
        return stem_map.get(mod)
    as_path = mod.replace(".", "/") + ".py"
    if as_path in rel_set:
        return as_path
    pkg_init = mod.replace(".", "/") + "/__init__.py"
    if pkg_init in rel_set:
        return pkg_init
    if module_map:
        hit = module_map.get(mod)
        if hit:
            return hit
    head = mod.split(".")[0]
    if head in _PACKAGE_ROOTS:
        if pkg_init in rel_set:
            return pkg_init
    return None


def _resolve_path_literal(literal: str, stem_map: dict, rel_set: set) -> str | None:
    """Map a path string constant onto a repo file, or None.

    "tools/publish_post.py" resolves directly. A BARE "publish_post.py" resolves
    only when exactly one repo file carries that stem, because the common real
    form is str(ROOT / "tools" / "publish_post.py") -- the directory arrives as a
    separate literal and never reaches this function.
    """
    value = literal.replace(chr(92), "/").lstrip("./")
    if value in rel_set:
        return value
    if "/" in value:
        return None
    stem = value[:-3] if value.endswith(".py") else value
    return stem_map.get(stem)


def build_dep_graph(index: dict) -> dict:
    """Typed dependency graph over the python files in the index.

    Built once per index and memoised, because a single BFS over it costs well
    under a millisecond while re-reading the repo costs seconds.
    """
    key = (index.get("root"), index.get("generated"), index.get("file_count"))
    hit = _GRAPH_CACHE.get(key)
    if hit is not None:
        return hit

    py = {rel: e for rel, e in (index.get("entries") or {}).items()
          if e.get("kind") == "py"}
    rel_set = set(py)
    stem_map, ambiguous = _stem_map(rel_set)
    module_map, ambiguous_dotted = _module_map(rel_set)

    forward: dict = {}
    for rel, entry in py.items():
        edges: dict = {}
        for mod in entry.get("dep_modules") or []:
            target = _resolve_module(mod, stem_map, rel_set, module_map)
            if target and target != rel:
                edges[target] = "import"
        for literal in entry.get("dep_paths") or []:
            target = _resolve_path_literal(literal, stem_map, rel_set)
            if target and target != rel and target not in edges:
                edges[target] = "path"      # an import edge outranks a path edge
        forward[rel] = edges

    reverse: dict = {}
    for src, edges in forward.items():
        for dst, kind in edges.items():
            reverse.setdefault(dst, {})[src] = kind

    graph = {
        "nodes": sorted(rel_set),
        "forward": forward,
        "reverse": reverse,
        "ambiguous_stems": ambiguous,
        "edge_count": sum(len(e) for e in forward.values()),
    }
    graph["test_reachable"] = _test_reachable(graph)
    graph["calibration"] = _calibrate(graph)
    _GRAPH_CACHE.clear()          # one repo at a time; never serve another root
    _GRAPH_CACHE[key] = graph
    return graph


def _test_reachable(graph: dict) -> set:
    """Every file some test file can reach through import/path edges.

    This is REACHABILITY, not assertion coverage: it says a test loads the file,
    not that a test asserts anything about it. It is therefore an upper bound,
    and the payload says so. An upper bound is still decisive in the direction
    that matters -- a file NOT in this set has no test standing behind it at all.
    """
    seen = {rel for rel in graph["forward"] if rel.startswith(TEST_PREFIX)}
    queue = deque(seen)
    while queue:
        cur = queue.popleft()
        for nxt in graph["forward"].get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _bfs_dependents(graph: dict, target: str, max_depth: int | None = None):
    """BFS over reverse edges. Returns (depth, parent, edge_kind) maps.

    Visited-set BFS, so an import cycle terminates and no file is counted twice,
    and the recorded depth is the SHORTEST distance to the target.

    Traversal does not expand THROUGH a test file: a test is a leaf consumer, so
    a chain that routes production impact through a test is not a real cascade.
    """
    depth = {target: 0}
    parent: dict = {}
    edge: dict = {}
    queue = deque([target])
    reverse = graph["reverse"]
    while queue:
        cur = queue.popleft()
        if cur != target and cur.startswith(TEST_PREFIX):
            continue
        nxt_depth = depth[cur] + 1
        if max_depth is not None and nxt_depth > max_depth:
            continue
        for dep in sorted(reverse.get(cur, {})):
            if dep in depth:
                continue
            depth[dep] = nxt_depth
            parent[dep] = cur
            edge[dep] = reverse[cur][dep]
            queue.append(dep)
    return depth, parent, edge


def _calibrate(graph: dict) -> dict:
    """Measure this repo, so the risk model has no imported magic numbers.

    Runs the dependent BFS for every node -- sub-second on 1200 files -- and
    takes the 95th percentile of both distributions. The result is what
    "large blast radius here" and "deep cascade here" actually mean in THIS
    repository, and it travels with the payload so a reader can check it.
    """
    reach_values = []
    depth_values = []
    for node in graph["forward"]:
        depth, _, _ = _bfs_dependents(graph, node)
        code = [r for r in depth if r != node and not r.startswith(TEST_PREFIX)]
        reach_values.append(len(code))
        depth_values.append(max(depth.values()) if len(depth) > 1 else 0)
    return {
        "files_measured": len(reach_values),
        "percentile": CALIBRATION_PERCENTILE,
        "reach_saturation": max(1, _percentile(reach_values, CALIBRATION_PERCENTILE)),
        "depth_saturation": max(1, _percentile(depth_values, CALIBRATION_PERCENTILE)),
        "max_reach_observed": max(reach_values) if reach_values else 0,
        "max_depth_observed": max(depth_values) if depth_values else 0,
        "method": ("95th percentile of the transitive non-test dependent count and of the "
                   "cascade depth, measured over every python file in this repo"),
    }


def transitive_dependents(graph: dict, target: str, max_depth: int | None = None) -> dict:
    """Full transitive dependent set with depth and the shortest path to each.

    This is the whole point of the tool. Direct importers answer "who names me";
    this answers "who breaks", which on this repo is a different number by an
    order of magnitude for hub files.
    """
    if target not in graph["forward"]:
        return {"count": 0, "code_count": 0, "test_count": 0, "max_depth": 0,
                "by_depth": {}, "direct_count": 0, "dependents": [],
                "note": "target is not a python file in the indexed graph"}

    depth, parent, edge = _bfs_dependents(graph, target, max_depth=max_depth)
    records = []
    for rel, d in depth.items():
        if rel == target:
            continue
        chain = [rel]
        cur = rel
        while cur in parent:
            cur = parent[cur]
            chain.append(cur)
        records.append({
            "path": rel,
            "depth": d,
            "edge": edge.get(rel, "import"),
            "is_test": rel.startswith(TEST_PREFIX),
            "shortest_path": chain,
        })
    records.sort(key=lambda r: (r["depth"], r["path"]))

    by_depth: dict = {}
    for rec in records:
        by_depth[str(rec["depth"])] = by_depth.get(str(rec["depth"]), 0) + 1
    return {
        "count": len(records),
        "code_count": sum(1 for r in records if not r["is_test"]),
        "test_count": sum(1 for r in records if r["is_test"]),
        "max_depth": max((r["depth"] for r in records), default=0),
        "by_depth": by_depth,
        "direct_count": len(graph["reverse"].get(target, {})),
        "dependents": records,
    }


def radius_coverage(graph: dict, target: str, transitive: dict) -> dict:
    """Which files in the radius have a test standing behind them, and which do not.

    The uncovered list is the useful half. A wide radius that is fully reachable
    from tests is recoverable; the files nobody tests are exactly where a change
    lands silently, so they are NAMED, not counted.
    """
    reachable = graph.get("test_reachable") or set()
    population = [target] + [r["path"] for r in transitive["dependents"]
                             if not r["is_test"]]
    covered = [rel for rel in population if rel in reachable]
    uncovered = [rel for rel in population if rel not in reachable]
    return {
        "method": ("test-reachability over the import/path graph: a file is covered when "
                   "some tests/ file can reach it. Upper bound on real assertion coverage"),
        "population": len(population),
        "covered_count": len(covered),
        "uncovered_count": len(uncovered),
        "covered_ratio": round(len(covered) / len(population), 4) if population else 0.0,
        "target_covered": target in reachable,
        "uncovered": sorted(uncovered),
        "covered": sorted(covered),
    }


def _entrypoint_signal(hits: dict) -> dict:
    """Graded entrypoint evidence, with the evidence attached.

    A governing document naming the file means a documented human procedure runs
    it. A registry or skill naming it means a machine route runs it. The second
    is worth half, because it carries most of the consequence but is not part of
    the operating contract a person follows.
    """
    critical = sorted({
        r for r in hits["docs"] + hits["registries"]
        if any(r.endswith(c) or r == c for c in CRITICAL_REFERRERS)
    })
    machine = sorted(set(hits["registries"]) - set(critical)) + sorted(hits["skills"])
    if critical:
        value, why = ENTRYPOINT_AUTHORITY, "named by a governing document"
    elif machine:
        value, why = ENTRYPOINT_MACHINE_ROUTE, "named by a registry or skill route"
    else:
        value, why = 0.0, "no registered entrypoint reference"
    return {"value": value, "why": why, "authority_refs": critical,
            "machine_refs": machine[:20]}


def compute_risk(transitive: dict, coverage: dict, entrypoint: dict,
                 calibration: dict) -> dict:
    """Reproducible risk score. Every input is in the payload.

    reach is log-scaled because the review posture changes far more between 2 and
    20 dependents than between 200 and 220; depth is linear because it is already
    a small integer. Both saturate at the measured 95th percentile of THIS repo.
    """
    reach_sat = max(1, int(calibration.get("reach_saturation") or 1))
    depth_sat = max(1, int(calibration.get("depth_saturation") or 1))

    reach_raw = transitive["code_count"]
    depth_raw = transitive["max_depth"]
    uncovered_raw = coverage["uncovered_count"]
    population = max(1, coverage["population"])

    reach_norm = min(1.0, math.log10(1 + reach_raw) / math.log10(1 + reach_sat))
    depth_norm = min(1.0, depth_raw / depth_sat)
    uncovered_norm = uncovered_raw / population
    entry_norm = float(entrypoint["value"])

    components = {
        "reach": {"raw": reach_raw, "saturation": reach_sat,
                  "normalized": round(reach_norm, 4),
                  "formula": "min(1, log10(1+raw)/log10(1+saturation))",
                  "why": "transitive non-test dependents: how many files can break"},
        "uncovered": {"raw": uncovered_raw, "population": population,
                      "normalized": round(uncovered_norm, 4),
                      "formula": "raw/population",
                      "why": "share of the radius (target included) no test reaches"},
        "depth": {"raw": depth_raw, "saturation": depth_sat,
                  "normalized": round(depth_norm, 4),
                  "formula": "min(1, raw/saturation)",
                  "why": "deepest cascade hop: the part a reviewer cannot see"},
        "entrypoint": {"raw": entry_norm, "normalized": round(entry_norm, 4),
                       "formula": "1.0 governing document, 0.5 registry/skill route, else 0",
                       "why": entrypoint["why"]},
    }
    weighted = {k: round(RISK_WEIGHTS[k] * components[k]["normalized"], 4)
                for k in RISK_WEIGHTS}
    for k in weighted:
        components[k]["weighted"] = weighted[k]
    score = round(100.0 * sum(weighted.values()), 1)

    band = "LOW"
    for name, floor in RISK_BANDS:
        if score >= floor:
            band = name
            break
    return {
        "score": score,
        "band": band,
        "formula": RISK_FORMULA,
        "weights": dict(RISK_WEIGHTS),
        "components": components,
        "bands": {name: floor for name, floor in RISK_BANDS},
        "calibration": calibration,
    }


# ---------------------------------------------------------------- blast radius

def blast(target: str, index: dict | None = None, max_depth: int | None = None,
          max_listed: int | None = None) -> dict:
    """Blast radius for one file: direct radius, transitive set, coverage, risk.

    Both numbers are reported on purpose. direct.fan_in is who NAMES the file;
    transitive.count is who BREAKS. On this repo those differ by an order of
    magnitude for hub files, and reporting only the first is the false
    confidence this tool was built to remove.
    """
    p = (ROOT / target).resolve()
    if not p.exists():
        return {"ok": False, "error": f"not found: {target}"}
    rel = _rel(p)
    index = index or build_index()
    entry = index["entries"].get(rel, {})
    hits = _referencing_files(rel, _module_name(p), index=index)

    fan_in = len(hits["imports"]) + len(hits["cli_callers"])
    entrypoint = _entrypoint_signal(hits)
    critical = entrypoint["authority_refs"]
    tested = bool(hits["tests"])

    graph = build_dep_graph(index)
    transitive = transitive_dependents(graph, rel, max_depth=max_depth)
    coverage = radius_coverage(graph, rel, transitive)
    risk = compute_risk(transitive, coverage, entrypoint, graph["calibration"])

    if fan_in >= 10 or critical:
        tier = "HIGH"
    elif fan_in >= 3 or hits["skills"]:
        tier = "MEDIUM"
    else:
        tier = "LOW"

    reasons = []
    if critical:
        reasons.append(f"referenced by governing authority: {', '.join(critical)}")
    if fan_in:
        reasons.append(f"{fan_in} in-repo callers")
    if transitive["count"] > transitive["direct_count"]:
        reasons.append(f"{transitive['count']} transitive dependents "
                       f"(vs {transitive['direct_count']} direct) to depth "
                       f"{transitive['max_depth']}")
    if hits["skills"]:
        reasons.append(f"{len(hits['skills'])} skill(s) route through it")
    if coverage["uncovered_count"]:
        reasons.append(f"{coverage['uncovered_count']} of {coverage['population']} files in "
                       f"the radius have no test reaching them")
    if not tested:
        reasons.append("NO test references it")

    listed = transitive["dependents"]
    truncated = False
    if max_listed is not None and len(listed) > max_listed:
        listed = listed[:max_listed]
        truncated = True

    return {
        "ok": True,
        "schema_version": BLAST_SCHEMA,
        "path": rel,
        "summary": entry.get("summary", ""),
        "subcommands": entry.get("subcommands", []),
        "tier": tier,
        "fan_in": fan_in,
        "tested": tested,
        "risk_score": risk["score"],
        "risk_band": risk["band"],
        "reasons": reasons,
        "radius": hits,
        "transitive": {**transitive, "dependents": listed,
                       "dependents_truncated": truncated},
        "coverage": coverage,
        "entrypoint": entrypoint,
        "risk": risk,
        "graph_stats": {"nodes": len(graph["nodes"]), "edges": graph["edge_count"],
                        "ambiguous_stems": len(graph["ambiguous_stems"])},
    }


def blast_gate(result: dict) -> tuple:
    """Fail CLOSED when a wide radius has no test standing behind it.

    Two rules, both earned. The first is the original: a HIGH-tier file with zero
    tests is the shape of every regression that reached the live site. The second
    exists because the first could not see past direct importers -- a file with
    two importers and a hundred uncovered transitive dependents passed it clean.
    """
    if not result.get("ok"):
        return False, result.get("error", "unknown")
    if result.get("tier") == "HIGH" and not result.get("tested"):
        return False, (f"{result['path']}: tier=HIGH ({result.get('fan_in', 0)} callers) with "
                       f"NO tests. Write a test that locks current behaviour before changing it.")
    band = result.get("risk_band")
    coverage = result.get("coverage") or {}
    if band in ("CRITICAL", "HIGH") and coverage.get("uncovered_count"):
        transitive = result.get("transitive") or {}
        return False, (f"{result['path']}: risk={result.get('risk_score')} band={band} with "
                       f"{coverage['uncovered_count']} untested file(s) in a radius of "
                       f"{transitive.get('count', 0)} dependents. First uncovered: "
                       f"{', '.join(coverage.get('uncovered', [])[:5])}")
    return True, (f"{result['path']}: tier={result.get('tier')} "
                  f"risk={result.get('risk_score')} band={band} "
                  f"tested={result.get('tested')}")


# ---------------------------------------------------------------- advisor packet

def build_packet(targets: list, question: str, out_dir: Path,
                 mission: str = "", index: dict | None = None) -> dict:
    """Emit a bounded, high-signal advisor packet: BRIEF.md + the source files.

    Bounded on purpose. An advisor that receives 40 files reads none of them;
    the brief states the question FIRST, then the measured facts, then the code.
    """
    index = index or build_index()
    out_dir.mkdir(parents=True, exist_ok=True)
    attachments = []
    sections = []
    skipped = []

    for t in targets:
        p = (ROOT / t).resolve()
        if not p.exists():
            skipped.append({"path": t, "why": "not found"})
            continue
        rel = _rel(p)
        size = p.stat().st_size
        if size > MAX_PACK_FILE_BYTES:
            skipped.append({"path": rel, "why": f"{size}B exceeds {MAX_PACK_FILE_BYTES}B cap"})
            continue
        dest = out_dir / p.name
        shutil.copy2(p, dest)
        attachments.append(str(dest))
        b = blast(rel, index=index)
        entry = index["entries"].get(rel, {})
        line = [f"### {rel}  ({size} B)"]
        if entry.get("summary"):
            line.append(f"Purpose: {entry['summary']}")
        if entry.get("subcommands"):
            line.append(f"Subcommands: {', '.join(entry['subcommands'])}")
        if b.get("ok"):
            tr = b["transitive"]
            cov = b["coverage"]
            line.append(f"Blast radius: risk={b['risk_score']} ({b['risk_band']}), "
                        f"{tr['direct_count']} direct -> {tr['count']} transitive dependents "
                        f"to depth {tr['max_depth']}, {cov['uncovered_count']} of "
                        f"{cov['population']} untested"
                        + (f" -- {'; '.join(b['reasons'])}" if b["reasons"] else ""))
            if cov["uncovered"]:
                line.append(f"Untested in radius: {', '.join(cov['uncovered'][:8])}")
            if b["radius"]["tests"]:
                line.append(f"Tests: {', '.join(b['radius']['tests'][:6])}")
        sections.append(chr(10).join(line))

    brief = [
        "# Skynet advisor packet",
        "",
        "## The question (answer THIS)",
        question.strip() or "(no question supplied)",
        "",
    ]
    if mission.strip():
        brief += ["## Mission context", mission.strip(), ""]
    brief += [
        "## What is attached",
        f"{len(attachments)} file(s), copied verbatim from the repo at "
        f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}.",
        "",
        *sections,
        "",
        "## Ground rules for your answer",
        "- Cite the exact file and line you are reasoning about; a claim with no line is noise.",
        "- If the attachment did not reach you, SAY SO instead of guessing at the code.",
        "- State uncertainty explicitly. Unknown is a valid, useful answer.",
        "- End with a single trailing line: VERDICT: APPROVE|BLOCK|UNSURE.",
    ]
    text = chr(10).join(brief)
    truncated = False
    if len(text.encode("utf-8")) > MAX_BRIEF_BYTES:
        text = text.encode("utf-8")[:MAX_BRIEF_BYTES].decode("utf-8", "ignore")
        text += chr(10) * 2 + "[brief truncated at cap -- shrink the target list, do not raise the cap]"
        truncated = True

    brief_path = out_dir / "BRIEF.md"
    brief_path.write_text(text, encoding="utf-8")
    attachments.insert(0, str(brief_path))

    attach_args = " ".join(f'--attachment "{a}"' for a in attachments)
    cmd = ("python tools/skynet_ai_convene_cdp.py --target both "
           f'--prompt-file "{brief_path}" {attach_args} --json')

    return {
        "ok": True,
        "out_dir": str(out_dir),
        "brief": str(brief_path),
        "brief_bytes": len(text.encode("utf-8")),
        "truncated": truncated,
        "attachments": attachments,
        "skipped": skipped,
        "convene_command": cmd,
    }


# ---------------------------------------------------------------- markdown audit

def md_audit() -> dict:
    """Separate governing markdown from orphan weight.

    Root markdown is the loudest sprawl in the repo. Authority files come from
    the central registry; everything unreferenced is weight we pay for on every
    context load and every advisor upload.
    """
    authorities = []
    try:
        reg = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        authorities = [a["path"] for a in reg.get("markdown", {}).get("authorities", [])]
    except Exception:
        pass

    root_md = sorted(_rel(p) for p in _iter_files(("*.md",)))
    docs_md = sorted(_rel(p) for p in _iter_files(("docs/**/*.md",)))

    # Which docs are actually pointed at by code, registry, or steering files?
    referenced = set()
    scan = []
    for d in CODE_DIRS:
        scan.extend(_iter_files((d + "/**/*.py",)))
    scan.extend(_iter_files(("data/*.json",)))
    scan.extend(ROOT / n for n in ("CLAUDE.md", "AGENTS.md") if (ROOT / n).exists())
    if SKILLS_DIR.exists():
        scan.extend(p for p in SKILLS_DIR.glob("**/*.md") if p.is_file())
    blob = chr(10).join(_read(p) for p in scan)
    for rel in root_md + docs_md:
        name = rel.split("/")[-1]
        if rel in blob or name in blob:
            referenced.add(rel)

    proposals = [r for r in root_md if r.startswith("PROPOSAL_") or "_PROPOSAL" in r]
    orphan_proposals = [r for r in proposals if r not in referenced and r not in authorities]
    orphan_bytes = sum((ROOT / r).stat().st_size for r in orphan_proposals if (ROOT / r).exists())

    return {
        "ok": True,
        "authorities": authorities,
        "root_md_count": len(root_md),
        "docs_md_count": len(docs_md),
        "proposal_count": len(proposals),
        "referenced_count": len(referenced),
        "orphan_proposal_count": len(orphan_proposals),
        "orphan_proposal_bytes": orphan_bytes,
        "orphan_proposals_sample": orphan_proposals[:15],
        "guidance": ("Do not add a permanent root .md for a one-off finding. Use "
                     "skynet_code_atlas.py pack to generate a disposable BRIEF.md in a run "
                     "dir, and put durable truth in an authority file or a test."),
    }


# ---------------------------------------------------------------- cli

def _print(payload, as_json: bool, human) -> None:
    if as_json:
        print(json.dumps(payload, indent=1, sort_keys=True, default=str))
    else:
        human(payload)


def _human_blast(d: dict) -> None:
    tr = d["transitive"]
    cov = d["coverage"]
    nl = chr(10)
    lines = [d["path"],
             f"  risk={d['risk_score']} {d['risk_band']} | tier={d['tier']} "
             f"fan_in={d['fan_in']} tested={d['tested']}",
             f"  dependents: {tr['count']} transitive ({tr['direct_count']} direct) | "
             f"code={tr['code_count']} tests={tr['test_count']} | max depth {tr['max_depth']}"]
    if tr["by_depth"]:
        spread = " ".join(f"d{k}={tr['by_depth'][k]}" for k in sorted(tr["by_depth"], key=int))
        lines.append(f"  spread: {spread}")
    lines.append(f"  coverage: {cov['covered_count']}/{cov['population']} reachable from "
                 f"tests, {cov['uncovered_count']} untested")
    for rel in cov["uncovered"][:8]:
        lines.append(f"    untested: {rel}")
    if len(cov["uncovered"]) > 8:
        lines.append(f"    ... and {len(cov['uncovered']) - 8} more (--json for all)")
    for r in d["reasons"]:
        lines.append(f"  - {r}")
    lines.append(f"  imports: {len(d['radius']['imports'])}"
                 f" | cli: {len(d['radius']['cli_callers'])}"
                 f" | tests: {len(d['radius']['tests'])}"
                 f" | docs: {len(d['radius']['docs'])}"
                 f" | skills: {len(d['radius']['skills'])}")
    lines.append("  " + str(d["gate_message"]))
    print(nl.join(lines))


def _build_parser() -> argparse.ArgumentParser:
    # Two separate declarations of the same two flags, on purpose.
    #
    # The subparser copies get default=SUPPRESS so that omitting --json after the
    # subcommand leaves whatever was parsed before it. They CANNOT be shared with
    # the top-level parser via parents=: argparse.set_defaults() mutates
    # action.default in place, and parents= copies action OBJECTS by reference, so
    # setting a default on the main parser silently rewrites the subparser default
    # to False and eats a --json given before the subcommand. That is a real trap
    # and it is locked by test_json_flag_still_works_BEFORE_the_subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--refresh", action="store_true", default=argparse.SUPPRESS,
                        help="force a full index rebuild")

    ap = argparse.ArgumentParser(
        description="Skynet AI File Blast Radius: find / map / blast / pack / md")
    ap.add_argument("--json", action="store_true", default=False)
    ap.add_argument("--refresh", action="store_true", default=False,
                    help="force a full index rebuild")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_find = sub.add_parser("find", parents=[common],
                            help="locate files by name/purpose/symbol")
    p_find.add_argument("query", nargs="+")
    p_find.add_argument("--limit", type=int, default=12)

    p_map = sub.add_parser("map", parents=[common],
                           help="what a file is: purpose, subcommands, symbols")
    p_map.add_argument("path")

    p_blast = sub.add_parser("blast", parents=[common],
                             help="transitive blast radius, coverage and risk score")
    p_blast.add_argument("path")
    p_blast.add_argument("--gate", action="store_true",
                         help="exit 2 when the radius is wide and untested")
    p_blast.add_argument("--max-depth", type=int, default=None,
                         help="stop the cascade after N hops (default: unbounded)")
    p_blast.add_argument("--max-listed", type=int, default=None,
                         help="cap the listed dependents; the counts stay exact")
    p_blast.add_argument("--why", default="",
                         help="print the shortest path that reaches this dependent")

    p_pack = sub.add_parser("pack", parents=[common],
                            help="build a bounded CDP advisor packet")
    p_pack.add_argument("paths", nargs="+")
    p_pack.add_argument("--question", default="")
    p_pack.add_argument("--mission", default="")
    p_pack.add_argument("--out-dir", default="")

    sub.add_parser("md", parents=[common], help="markdown authority vs orphan audit")
    return ap


def _cmd_find(args, index) -> int:
    res = find(" ".join(args.query), limit=args.limit, index=index)
    if not res:
        _print({"ok": False, "results": [], "error": "no match"}, args.json,
               lambda d: print("no match"))
        return 3

    def human(d):
        for r in d["results"]:
            print(f"{r['score']:>4}  {r['path']}")
            print(f"      {r['summary'][:110]}")
    _print({"ok": True, "results": res}, args.json, human)
    return 0


def _cmd_map(args, index) -> int:
    rel = _rel((ROOT / args.path).resolve())
    entry = index["entries"].get(rel)
    if not entry:
        _print({"ok": False, "error": f"not indexed: {args.path}"}, args.json,
               lambda d: print(d["error"]))
        return 3

    def human(d):
        nl = chr(10)
        print(f"{d['path']} ({d['bytes']} B)" + nl
              + f"  {d.get('summary', '')}" + nl
              + f"  subcommands: {', '.join(d.get('subcommands') or []) or '-'}" + nl
              + f"  flags: {', '.join((d.get('flags') or [])[:12]) or '-'}" + nl
              + f"  symbols: {', '.join((d.get('symbols') or [])[:15]) or '-'}")
    _print({"ok": True, **entry}, args.json, human)
    return 0


def _cmd_blast(args, index) -> int:
    res = blast(args.path, index=index, max_depth=args.max_depth,
                max_listed=args.max_listed)
    if not res.get("ok"):
        _print(res, args.json, lambda d: print(d["error"]))
        return 3
    ok, msg = blast_gate(res)
    res["gate_ok"], res["gate_message"] = ok, msg
    if args.why:
        want = _rel((ROOT / args.why).resolve())
        match = next((r for r in res["transitive"]["dependents"] if r["path"] == want), None)
        res["why"] = match or {"path": want, "reached": False,
                               "note": "not in the transitive dependent set"}
    _print(res, args.json, _human_blast)
    if args.why and not args.json:
        why = res.get("why") or {}
        chain = why.get("shortest_path")
        detail = " -> ".join(chain) if chain else why.get("note", "unknown")
        print(f"  why {why.get('path')}: {detail}")
    if args.gate and not ok:
        return 2
    return 0


def _cmd_pack(args, index) -> int:
    out_dir = Path(args.out_dir) if args.out_dir else \
        ROOT / "data" / "runs" / f"atlas_packet_{time.strftime('%Y%m%d_%H%M%S')}"
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    res = build_packet(args.paths, args.question, out_dir, mission=args.mission, index=index)

    def human(d):
        tail = ", TRUNCATED" if d["truncated"] else ""
        print(f"packet: {d['out_dir']}")
        print(f"  brief: {d['brief']} ({d['brief_bytes']} B{tail})")
        print(f"  attachments: {len(d['attachments'])}")
        for s in d["skipped"]:
            print(f"  skipped: {s['path']} -- {s['why']}")
        print("")
        print(d["convene_command"])
    _print(res, args.json, human)
    return 0


def _cmd_md(args) -> int:
    res = md_audit()

    def human(d):
        print(f"root .md: {d['root_md_count']} | docs .md: {d['docs_md_count']}")
        print(f"proposals: {d['proposal_count']} | orphan proposals: "
              f"{d['orphan_proposal_count']} ({d['orphan_proposal_bytes']} B)")
        print(f"authorities: {len(d['authorities'])}")
        print(d["guidance"])
    _print(res, args.json, human)
    return 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    index = build_index(force=args.refresh)
    if args.cmd == "find":
        return _cmd_find(args, index)
    if args.cmd == "map":
        return _cmd_map(args, index)
    if args.cmd == "blast":
        return _cmd_blast(args, index)
    if args.cmd == "pack":
        return _cmd_pack(args, index)
    if args.cmd == "md":
        return _cmd_md(args)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
