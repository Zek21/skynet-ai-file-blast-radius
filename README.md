# Skynet AI File Blast Radius

**Before you edit a file: what else breaks, how far does it go, and is any of it tested?**

```bash
pip install skynet-ai-file-blast-radius
blast-radius src/core/protocol.py
```

```
src/core/protocol.py
  risk=76.3 CRITICAL | tier=HIGH fan_in=94 tested=True
  dependents: 366 transitive (11 direct) | code=169 tests=197 | max depth 11
  spread: d1=11 d2=12 d3=38 d4=38 d5=38 d6=38 d7=55 d8=91 d9=37 d10=7 d11=1
  coverage: 134/170 reachable from tests, 36 untested
    untested: src/adapters/legacy_export.py
    untested: src/probes/viewport_probe.py
    ...
```

**Eleven direct importers.** That is the number a code-review tool shows you, and
it is the number you discount.

**Three hundred and sixty-six transitive dependents, at a depth of eleven, with
thirty-six of them untested.** That is the same change.

---

## The problem with counting importers

A file with two direct importers can sit beneath two hundred transitive
dependents. A file with twenty direct importers can be a leaf that nothing else
reaches. Direct fan-in cannot distinguish them — and it is the second-order
dependents, the files nobody opens during review, where the regression lands.

This tool walks the full transitive closure, records the depth at which each
dependent is reached, and tells you which of them no test can reach.

---

## What you get

```bash
blast-radius src/core/protocol.py --json
```

| Field | Meaning |
|-------|---------|
| `transitive` / `direct` | the amplification, side by side |
| `spread` | how many dependents at each depth |
| `coverage.uncovered` | the files with no test reaching them, **named** |
| `risk.formula` | the exact arithmetic behind the score |
| `risk.components` | every term: raw, normalised, weighted, and why |
| `risk.calibration` | the saturation constants and how they were derived |
| `graph_stats` | nodes, edges, and ambiguous module stems |

**Why is this file in my radius?**

```bash
blast-radius src/core/protocol.py --why src/ui/panel.py
# src/core/protocol.py -> src/net/session.py -> src/app/state.py -> src/ui/panel.py
```

**Gate a change in CI:**

```bash
blast-radius src/core/protocol.py --gate   # exit 2 when the radius is wide and untested
```

The gate message names the specific uncovered files. A gate that fails without
saying what to fix gets bypassed.

---

## The risk score, in full

```
score = 100 * (0.35*reach + 0.30*uncovered + 0.20*depth + 0.15*entrypoint)
```

Every term is normalised to [0,1] and reported with its raw value, so you can
disagree with a specific term rather than with the number.

- **reach** — transitive non-test dependents, scaled `log10(1+raw)/log10(1+sat)`.
  Sub-linear on purpose: 5 → 50 dependents matters far more than 300 → 350.
- **uncovered** — share of the radius no test reaches.
- **depth** — deepest cascade hop; the part a reviewer cannot see.
- **entrypoint** — 1.0 if a governing document names the file, 0.5 for a registry
  or plugin route, else 0.

**The saturation constants are measured, not invented.** They are the 95th
percentile of transitive dependent count and cascade depth across every Python
file in *your* tree, recomputed per run and reported in `risk.calibration`. A
constant tuned for a 200-file project is wrong for a 5,000-file one.

**This makes scores comparable inside one tree, and NOT between two.** Because
the scale is derived from the tree it measures, a 73 in one repository and a 73
in another are two different measurements wearing the same number. Rank files
against their own codebase; do not rank codebases against each other. On a very
small tree the 95th percentile saturates almost everything and the score stops
discriminating at all — the calibration block is reported precisely so you can
see when that has happened.

---

## Honesty about coverage

Coverage here is **test-reachability over the dependency graph**: a file is
covered when some file under your test root can reach it.

That is an **upper bound on real assertion coverage**, and the tool says so in
the `coverage.method` field a consumer actually reads — not in a footnote. A test
that can reach a module has not necessarily asserted anything about it.

It is still the right signal for this purpose: a file no test can even reach is
certainly not protected.

---

## Works on your layout

Import resolution handles the forms real projects use:

```python
import module                              # flat
from package.module import thing           # package-absolute
from .module import thing                  # relative
from . import module                       # relative, module-as-name
from ..package.module import thing         # parent-relative
```

...and resolves them under `src/`, `lib/`, `app/`, a flat root, or wherever your
code lives, because dotted names are matched against every source-root-relative
form of each file's path.

Two of those forms were broken until this tool was first pointed at a repository
other than the one it grew up in. Both produced the same symptom — a heavily
imported module reporting **zero dependents and zero coverage** — because a
relative import returned the imported *symbols* instead of the module name, and a
package-absolute import was only ever tried as a path from the repo root, which
is never correct under a `src/` layout. `tests/test_portability.py` exists so
neither can come back.

Beyond imports, a **string constant naming a file** also counts as an edge —
`subprocess.run(["python", "tools/report.py"])` is real coupling. Docstrings and
long embedded payloads are excluded, each because it manufactured a false edge on
a real repository.

Cycles are safe: breadth-first with a visited set, so nothing is counted twice
and nothing loops.

---

## Configuration

| Variable | Default |
|----------|---------|
| `BLAST_RADIUS_ROOT` | nearest git working tree, else cwd |
| `BLAST_RADIUS_CODE_DIRS` | `src,lib,app,tools,tests,test,scripts,pkg,internal,.` |
| `BLAST_RADIUS_TEST_PREFIX` | `tests/` |
| `BLAST_RADIUS_AUTHORITY_DOCS` | `README.md,ARCHITECTURE.md,CONTRIBUTING.md,SPEC.md,AGENTS.md,CLAUDE.md` |

---

## Other commands

```bash
blast-radius find "the thing"        # where does it actually live?
blast-radius map path/to/file.py     # entrypoints, symbols, tests, registrations
blast-radius md                      # which docs are authority, which are dead weight
blast-radius pack path/to/file.py --question "is this patch safe?"
```

`pack` builds a size-capped review packet — the file, its highest-risk
dependents, and the uncovered ones — for handing to a reviewer or a model.
Bounded on purpose: a packet that does not fit in a reviewer's attention is a
packet that was not read.

---

## Limitations

Read these before quoting a number from this tool.

**`fan_in` and the direct-dependent count are different measurements.** In the
sample output above, `fan_in=94` sits next to `11 direct`. That is not a bug and
they are not interchangeable. `fan_in` is a deliberately loose *mention* count —
it includes a file that merely names the module in a comment or a docstring — and
it is kept because it is a useful "who talks about this at all" signal. The
graph, and therefore every transitive number, uses strict AST-derived edges only.
Both are printed side by side. **Never add them together**, and when you want
"who would break", the direct count is the one you mean.

**Coverage is reachability, not assertion.** A test that imports a module marks
it covered even if it asserts nothing about it. This is an upper bound, and it is
only decisive in one direction: a file *outside* the covered set has no test
behind it at all.

**Scores are comparable within a tree, not across trees** — see the calibration
note above. On a very small repository the calibration saturates and the score
stops discriminating.

**Ambiguous module stems resolve deterministically, which can be wrong.** When
two files share a stem, a bare `import thing` picks one candidate by a fixed
precedence. If the other was meant, that edge is wrong. The count is reported as
`graph_stats.ambiguous_stems` so you can see how much of the graph is exposed to
this.

**A cold run parses everything.** On a ~1,200-file tree the first build takes
seconds; subsequent runs reuse an incremental per-file index and complete in
well under a second. The index is invalidated per file by mtime and size, and a
cold run and a warm run are asserted to produce identical output.

---

## Specification

[SPEC.md](SPEC.md) is a numbered, testable specification in the structure IEEE
standards use. It is **not** an IEEE-published standard and does not claim to be;
it is written so it could be reviewed under that process, which mostly means one
discipline: every `shall` maps to an executable test in Annex A.

```bash
pytest tests/     # 97 tests
```

## License

MIT. See [LICENSE](LICENSE).
