# Specification for a Source-Level Change Impact (Blast Radius) Analyser

**Document:** SKY-BR-1  
**Version:** 2.0.0 (output `schema_version: 2`)  
**Status:** Draft for review  
**Date:** 2026-08-21

Written in the structure and language conventions used by IEEE standards
(IEEE Std 830 / 1016 / 29148 lineage). It is **not** an IEEE-published standard
and makes no claim to that status. It is prepared so that it *could* be reviewed
under that process: every normative statement is numbered, testable, and mapped
in Annex A to an executable test.

---

## 1. Overview

### 1.1 Scope

This specification defines a **change impact analyser** that answers, before a
source file is modified:

- a) which other files can break as a consequence;
- b) how far the consequence propagates;
- c) whether that consequence is covered by tests;
- d) a reproducible risk figure derived from (a) through (c).

It applies to statically analysable source trees. It is defined here over Python,
but nothing in Clauses 4 through 8 is language-specific except the edge
extraction rules of 4.2.

### 1.2 Purpose

The defect this addresses is a **direct-importer count presented as impact**.

A file with two direct importers may sit beneath two hundred transitive
dependents. A file with twenty direct importers may be a leaf that nothing else
reaches. A tool reporting only direct importers cannot distinguish these, yet it
is the second-order dependents — the ones the reviewer never opens — where the
regression actually lands.

The reference implementation previously reported 120 direct importers for its
own protocol module. The transitive dependent set is **366**, reached at a
maximum depth of 11 hops. The first figure is not wrong; it is answering a
different question from the one a reviewer is asking.

### 1.3 Word usage

- **shall** — mandatory. An implementation violating a *shall* is non-conforming.
- **should** — recommended; deviation permitted where justified.
- **may** — optional.

### 1.4 Conformance

An implementation is conforming if it satisfies every *shall* in Clauses 4
through 8 and passes the corresponding tests in Annex A.

---

## 2. Normative references

- ISO/IEC/IEEE 24765, *Systems and software engineering — Vocabulary*
- Python Language Reference, §7.11 (`import`)

---

## 3. Definitions

**3.1 dependency edge** — a directed relation *A → B* meaning a change to *B*
can alter the behaviour of *A*.

**3.2 direct dependent** — a file with a dependency edge to the target.

**3.3 transitive dependent set** (*blast radius*) — the transitive closure of
direct dependents over the dependency graph.

**3.4 depth** — the number of hops in the shortest path from the target to a
given dependent.

**3.5 cascade depth** — the maximum depth over the transitive dependent set.

**3.6 reach** — the count of transitive dependents that are not test files.

**3.7 test-reachable** — a file is test-reachable when some file under the test
root can reach it through the dependency graph.

**3.8 uncovered radius** — members of the blast radius that are not
test-reachable.

**3.9 entrypoint weight** — a measure of whether the target is named by a
governing document, a machine registry, or neither.

**3.10 saturation constant** — the value of a raw measure above which its
normalised contribution to the risk score is 1.0.

---

## 4. Graph construction

**4.1** The analyser **shall** build a single directed graph over all analysable
files in the tree, and **shall** report its node and edge counts.

**4.2** Edges **shall** be derived from at least: `import` and `from ... import`
statements resolved to in-tree files, and references to a file by path in
non-source artefacts such as documents, registries, and skill definitions.

*Rationale: in an orchestrated system a large share of coupling is not an import.
A file named as a subprocess target in a JSON registry breaks just as thoroughly
when its interface changes, and an import-only graph cannot see it.*

**4.3** Where a module name is ambiguous — the same stem present at several paths
— the analyser **shall** count the ambiguity and report it, and **shall not**
silently choose one resolution. The reference implementation reports
`ambiguous_stems`.

**4.4** Graph construction **shall** be deterministic: identical input trees
produce identical graphs.

---

## 5. Reachability

**5.1** The analyser **shall** report the transitive dependent set (3.3), not
only direct dependents.

**5.2** The analyser **shall** report both counts distinctly, so a reader can see
the amplification. The reference implementation prints
`22 transitive (11 direct)`.

**5.3** The analyser **shall** report the depth (3.4) at which each dependent is
reached, and **shall** report the distribution across depths.

**5.4** The analyser **shall** terminate on cyclic graphs, and **shall not**
count any dependent more than once.

*Rationale: import cycles are common in mature trees. A naïve recursive walk
either does not terminate or inflates the count by revisiting.*

**5.5** The analyser **shall** be able to produce, for any dependent, the
shortest path connecting it to the target. *Rationale: a bare list of 366 files
is not actionable; a reviewer needs to see why a given file is in the set.*

**5.6** The analyser **may** accept a depth bound. When bounded, it **shall**
state that the result is bounded.

**5.7** Where the listed dependents are truncated for display, the reported
**counts shall remain exact**. *Rationale: silent truncation reads as complete
coverage.*

---

## 6. Risk scoring

**6.1** The analyser **shall** emit a risk score together with the formula that
produced it, the value of each component, and each component's weight.

*Rationale: an unexplained score is not actionable and cannot be disputed. A
reviewer who disagrees with a score must be able to identify which term they
disagree with.*

**6.2** The score **shall** combine at least: reach (3.6), uncovered share (3.8),
cascade depth (3.5), and entrypoint weight (3.9).

**6.3** Each component **shall** be normalised to [0,1] before weighting.

**6.4** Saturation constants (3.10) **shall** be derived from the analysed tree
rather than fixed in the source, and the derivation **shall** be reported.

*Rationale: a constant appropriate for a 200-file repository is wrong for a
5,000-file one. The reference implementation uses the 95th percentile of
transitive dependent count and of cascade depth measured over every Python file
in the tree, and reports both the percentile and the resulting saturation values.*

**6.5** Reach **should** be scaled sub-linearly. *Rationale: the difference
between 5 and 50 dependents is far more significant than between 300 and 350.
The reference implementation uses `min(1, log10(1+raw)/log10(1+saturation))`.*

**6.6** Band thresholds **shall** be reported alongside the band.

**6.7** Scores derived from tree-local calibration **shall not** be presented as
comparable between different trees, and documentation **shall** state this.

*Rationale: this follows directly from 6.4 and is the obvious thing to get wrong.
If the scale is the analysed tree's own 95th percentile, then equal scores in two
repositories are two different measurements sharing a number. Ranking files
within a codebase is supported; ranking codebases against each other is not.*

**6.8** Where calibration saturates such that the score no longer discriminates —
characteristically on a very small tree — the reported calibration block **shall**
make that visible.

*Rationale: on a tree of a few files the 95th percentile approaches the maximum,
every file normalises to 1.0, and the scale silently collapses. A consumer must
be able to detect this from the output rather than by intuition.*

**6.7** The reference scoring function, given here as an existence proof rather
than as a requirement:

```
score = 100 * (0.35*reach + 0.30*uncovered + 0.20*depth + 0.15*entrypoint)
```

---

## 7. Coverage

**7.1** The analyser **shall** report which members of the blast radius are
test-reachable and which are not, and **shall name** the uncovered ones.

*Rationale: a coverage percentage tells a reviewer nothing about where to look.
The uncovered members are the actionable output.*

**7.2** The analyser **shall** state the method by which coverage was determined,
and **shall** state its limitations.

*Rationale: graph reachability from a test file establishes that a test can reach
the code, not that it asserts anything about it. The reference implementation
labels this "Upper bound on real assertion coverage" in its own output, in the
same field a consumer reads.*

**7.3** The analyser **shall not** report a target as covered solely because
tests exist in the tree.

---

## 8. Output contract

**8.1** Machine-readable output **shall** carry a schema version.

**8.2** The following fields **shall** be present: target path, direct dependent
count, transitive dependent count, cascade depth, depth distribution, uncovered
member list, risk score, risk band, scoring formula, component breakdown,
calibration parameters, and graph statistics.

**8.3** The analyser **shall** offer a gate mode that exits non-zero when the
radius is both wide and insufficiently covered, and the message **shall** name
the specific uncovered files.

*Rationale: a gate that fails without naming what to fix produces a bypass.*

**8.4** Human-readable output **shall** remain available and **shall not** be a
dump of the JSON.

**8.6** Where an implementation reports more than one measure of the same
apparent quantity — for example a loose textual mention count alongside a strict
graph-derived dependent count — it **shall** report them under distinct names and
**shall** document that they are not interchangeable and must not be summed.

*Rationale: the reference implementation prints `fan_in=94` beside `11 direct` for
the same file. Both are correct and they measure different things: the first
counts any file mentioning the module, including in a comment; the second counts
edges derived from the syntax tree. Presented without explanation, a reader
reasonably concludes one of them is a bug.*

**8.5** Analysis of a tree of at least 1,000 files **should** complete in under
30 seconds on commodity hardware. Any cache **shall** produce results identical
to a cold run.

---

## Annex A (normative) — Conformance test mapping

| Clause | Requirement | Test |
|--------|-------------|------|
| 4.1 | graph counts reported | `graph_stats` in output |
| 4.3 | ambiguity counted, not hidden | `ambiguous_stems` field |
| 4.4 | deterministic construction | index determinism tests |
| 5.1 | transitive set reported | transitive-depth tests |
| 5.2 | both counts distinct | `dependents: N transitive (M direct)` |
| 5.4 | cycles terminate, no double count | cycle-case tests |
| 5.5 | shortest path available | `--why <dependent>` |
| 5.7 | truncation preserves exact counts | `--max-listed` tests |
| 6.1 | formula and components emitted | `risk.formula`, `risk.components` |
| 6.4 | calibration derived and reported | `risk.calibration` |
| 7.1 | uncovered members named | `coverage.uncovered` |
| 7.2 | method and limits stated | `coverage.method` |
| 6.7 | not comparable across trees | documented in README + `risk.calibration` |
| 6.8 | saturation visible in output | `risk.calibration` block |
| 8.1 | schema version present | `schema_version` |
| 8.3 | gate names the files | `gate_message` |
| 8.6 | distinct measures named distinctly | `fan_in` vs `transitive.direct_count` |

## Annex B (informative) — Worked example

Measured on the reference tree: 1,191 nodes, 2,055 edges.

| Target | Direct | Transitive | Depth | Uncovered | Score | Band |
|--------|--------|-----------|-------|-----------|-------|------|
| protocol module | 94 | 366 | 11 | 36 | 76.3 | CRITICAL |
| publisher | 11 | 22 | 5 | 0 | 43.5 | MEDIUM |
| new leaf tool | 1 | 0 | 0 | 0 | 2.5 | LOW |

The first row is the case this specification exists for. Ninety-four direct
importers is a number a reviewer can hold in mind and discount. Three hundred
and sixty-six dependents at a depth of eleven, thirty-six of which no test
reaches, is a different decision.
