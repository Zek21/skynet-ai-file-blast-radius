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
  coverage: 134/170 reachable from tests, 36 graph-unreached
    graph-unreached: src/adapters/legacy_export.py
    graph-unreached: src/probes/viewport_probe.py
    ...
```

**Eleven direct importers.** That is the number a code-review tool shows you, and
it is the number you discount.

**Three hundred and sixty-six transitive dependents, at a depth of eleven, with
thirty-six of them reached by no test through the import graph.** That is the same change.

Not all 366 will break. They are what the change can reach — the potential impact
surface, derived statically. That is the set worth bounding before you decide how
careful to be.

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
blast-radius src/core/protocol.py --gate   # exit 2 when wide and not reached by tests
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
  Sub-linear on purpose, encoding the assumption that 5 → 50 dependents changes
  the risk more than 300 → 350. That is a design judgement, not a measurement.
- **uncovered** — share of the radius no test reaches.
- **depth** — deepest cascade hop; the part a reviewer cannot see.
- **entrypoint** — 1.0 if a governing document names the file, 0.5 for a registry
  or plugin route, else 0.

**The saturation constants are measured, not invented.** They are the 95th
percentile of transitive dependent count and cascade depth across every Python
file in *your* tree, recomputed per run and reported in `risk.calibration`. A
fixed constant may fail to transfer between repositories unless it was
independently justified.

**This makes scores comparable inside one tree, and NOT between two.** Because
the scale is derived from the tree it measures, a 73 in one repository and a 73
in another are two different measurements wearing the same number. Rank files
against their own codebase; do not rank codebases against each other. On a very
small tree the scale can flatten: in synthetic hub-and-importer trees, a 22-file
tree produced a reach saturation of 1 (below an 8-file tree's 6, so not even
monotonic) and put 20 of 22 files in a single band. Nothing saturated upward
there; discrimination was what was lost. Whether that holds for other small-tree
topologies was not measured. The calibration block is reported so you can see it
happening in your tree.

---

## Honesty about coverage

Coverage here is **test-reachability over the dependency graph**: a file is
covered when some file under your test root can reach it.

That is a **proxy, not a bound**, and the tool says so in the `coverage.method`
field a consumer actually reads rather than in a footnote. It is wrong in both
directions: it **over-counts**, because a test that can reach a module need not
assert anything about it, and it **under-counts**, because a test may arrive by
runtime import, subprocess invocation or a plugin registry — mechanisms this
graph does not model. Calling it an upper bound would be tidier and would
contradict the tool's own documented blind spots.

It is still the right signal for this purpose, with the negative stated as
carefully as the positive: a file outside the covered set has no *graph-visible*
test behind it. That is not proof it is unprotected — a test may reach it through
runtime imports, subprocess invocation, a plugin registry, or an end-to-end suite,
none of which this model sees. What you get is a bounded set of places to look.

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

**Three counts, three populations.** `366 transitive` includes the 197 test
files. `reach=169` is the non-test subset and is what feeds the risk score —
traversal stops at tests, which is an architectural assumption about the tree
being analysed (that production code does not depend on the suite) rather than a
property of trees in general.
`coverage 134/170` ranges over those 169 non-test dependents plus the target
itself; tests are excluded on that same assumption. And
`max_reach_observed=308` is the largest non-test reach in the repository, so it
is compared against 169, not 366. None of these contradict each other, but three
differently-scoped counts printed without their scope is how a reader concludes
the tool is lying.

**Coverage is reachability, not assertion.** A test that imports a module marks
it covered even if it asserts nothing about it. This is a proxy, wrong in both
directions — it also misses tests arriving by runtime import, subprocess or
plugin registry. A file outside the covered set has no *graph-visible* test
behind it: somewhere to look, not a verdict.

**Scores are comparable within a tree, not across trees** — see the calibration
note above. On a very small repository the scale flattens rather than saturating:
measured, a 22-file tree put 20 of 22 files in one band.

**Ambiguity is handled two different ways, and both are counted.** When two
files share a bare stem, `import thing` picks one candidate by a fixed
precedence — if the other was meant, that edge is wrong (`ambiguous_stems`).
When two files claim the same *dotted* name, the edge is dropped rather than
guessed (`ambiguous_dotted`). Both counts appear in `graph_stats`, because an
exclusion nobody reports is indistinguishable from an edge that never existed.

**A cold run parses everything.** On a ~1,200-file tree the first build takes
seconds; subsequent runs reuse an incremental per-file index and complete in
well under a second. The index is invalidated per file by mtime and size, and a
cold run and a warm run are asserted to produce identical output.

---

## Specification

[SPEC.md](SPEC.md) is a numbered, testable specification in the structure IEEE
standards use. It is **not** an IEEE-published standard and does not claim to be;
it is written so it could be reviewed under that process, which mostly means one
discipline: all 27 `shall` clauses carry an Annex A row naming their evidence —
an executable test where the requirement reduces to one, an inspectable output
field or flag where it does not.

```bash
pytest tests/     # 98 tests
```

## License

MIT. See [LICENSE](LICENSE).
