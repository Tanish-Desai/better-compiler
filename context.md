# Master Research Context

> **Status update (2026-08-27):** the "Blocker 1" hindrance referenced in
> `docs/IMPLEMENTATION.md` §9 — `opt` was never built, so nothing ran
> end-to-end — is **resolved for one bug**. On branch `feat/e2e-bootstrap`,
> `opt` was built for real for issue `115575` (a VectorCombine
> miscompilation) and the bug was confirmed to reproduce against that build.
> See `docs/IMPLEMENTATION.md` §9 for the full transcript and
> `scripts/bootstrap_first_repair.py` for the script that did it. Still
> outstanding: Phase 2 (an actual LLM repair attempt, needs `LAB_LLM_TOKEN`)
> and scaling past this one bug (Blocker 3).

## 1. Research Project

We are working on a research project in **automated program repair (APR) for compiler bugs**, specifically **real-world LLVM middle-end/optimization bugs**.

The starting point is an existing LLVM APR benchmark and LLM-based repair infrastructure. The project is **not** about implementing a compiler from scratch. LLVM is the compiler being studied, and our research investigates whether LLM-based agents can diagnose and repair LLVM optimization bugs more effectively when they receive better formal-verification feedback.

The central proposed research direction is:

> **Investigate whether LLVM-IR-aware, semantics-preserving minimization and structured representation of Alive2 counterexamples can improve the effectiveness, efficiency, and reliability of LLM-based repair of real LLVM compiler bugs.**

The important distinction is:

```text
Existing research:
LLM + LLVM + tools + testing + formal verification

Our research:
LLM + LLVM + formal verification
        +
better representation of the formal-verification feedback
        +
controlled comparison of feedback strategies
```

The research should focus on **the feedback representation**, rather than merely building another generic LLM coding agent.

---

# 2. Existing Repository / Starting Point

The provided repository is an LLVM APR benchmark and repair framework.

The benchmark consists of real LLVM middle-end bugs. The benchmark repository states that it contains **295 verified issues** as of May 19, 2025, including:

* 106 miscompilation bugs
* 181 crash bugs
* 8 hang bugs

The benchmark provides, depending on the issue:

* issue description
* bug type
* base/broken LLVM commit
* LLVM IR reproducer(s)
* relevant source location
* file/function/component hints
* reference patch
* regression-test information

The benchmark was subsequently migrated into the `llvm-autofix` ecosystem.

The benchmark is valuable because it provides reproducible real-world compiler failures rather than synthetic toy bugs.

---

# 3. What LLVM Bugs Are We Studying?

The target is primarily LLVM middle-end/optimization behavior.

Important bug classes are:

### Crash

```text
LLVM IR
   ↓
LLVM optimization
   ↓
Crash / assertion
```

### Hang

```text
LLVM IR
   ↓
LLVM optimization
   ↓
Nontermination / timeout
```

### Miscompilation

```text
Valid LLVM IR
      ↓
Optimization pass
      ↓
Invalid semantic transformation
      ↓
Incorrect behavior
```

Miscompilation is particularly important because the compiler may:

* successfully build,
* produce valid-looking LLVM IR,
* and still be semantically wrong.

Therefore:

> **Compilation success is not evidence of compiler-correctness.**

---

# 4. Why LLVM IR Matters

LLVM IR gives us a lower-level and more controlled representation of the compiler transformation being studied.

Instead of:

```text
C/C++
  ↓
Clang frontend
  ↓
LLVM IR
  ↓
Optimization
  ↓
Bug
```

the benchmark often lets us work directly with:

```text
LLVM IR
  ↓
Specific LLVM pass
  ↓
Bug
```

This reduces unrelated frontend behavior and gives us a semantic object that can be formally analyzed.

The research therefore requires familiarity with LLVM concepts including:

* SSA
* def-use relationships
* basic blocks
* control-flow graphs
* PHI nodes
* GEP
* `inbounds`
* `nuw`
* `nsw`
* poison
* undef/undefined behavior
* refinement
* LLVM optimization passes
* vectorization
* InstCombine
* ScalarEvolution
* etc.

---

# 5. Existing LLVM Repair Pipeline

The existing LLVM repair framework can be abstracted as:

```text
Real LLVM Bug
      ↓
Bug Localization / Hints
      ↓
LLVM Source Context
      ↓
LLM
      ↓
Candidate C++ Patch
      ↓
Build LLVM
      ↓
Bug Reproducer
      ↓
Regression Tests
      ↓
Formal Verification where applicable
      ↓
Feedback
      ↓
LLM retries
```

The important point is that the current system already forms a **closed repair-and-verification loop**.

This means our research should avoid duplicating the entire system.

Instead, we should modify one important part of the loop:

```text
          EXISTING
              │
              ▼
        Alive2 Feedback
              │
              ▼
        [OUR RESEARCH]
              │
              ▼
      Better Feedback
              │
              ▼
             LLM
```

---

# 6. Existing LLM Compiler Repair Work: llvm-autofix

A major existing baseline is **llvm-autofix**, introduced in the paper *Agentic Harness for Real-World Compilers*.

The work argues that compiler bugs are unusually difficult for generic LLM agents because they require:

* compiler-specific knowledge,
* specialized tools,
* understanding of sparse bug reports,
* formal reasoning about compiler transformations.

It provides an LLVM-specific agentic harness, LLVM tools, and a benchmark of reproducible LLVM bugs. The published evaluation reports that frontier models experience a substantial performance drop on compiler bugs compared with common software bugs, and that the specialized minimal agent improves over the prior state of the art.

This is an essential baseline for our project.

We should therefore frame our work as:

> **An investigation into improving the feedback channel inside an existing LLVM-specific LLM repair workflow.**

The contribution is not “LLMs can fix LLVM bugs.” That has already been demonstrated.

---

# 7. Formal Verification with Alive2

For many LLVM miscompilation bugs, the benchmark uses **Alive2** to verify whether an optimized LLVM IR function correctly refines the original.

Conceptually:

```text
Source IR
   ↓
LLVM Transformation
   ↓
Target IR
   ↓
Alive2
   ↓
Valid / Invalid
```

If the transformation is invalid, Alive2 can provide a **counterexample** demonstrating a semantic situation in which the optimization is unsound.

Alive2 is an established LLVM optimization verification/translation-validation framework; it is therefore infrastructure we use rather than the novelty itself.

The benchmark itself explains that miscompilation issues are commonly checked with Alive2, which can provide counterexamples, while other cases can use execution-based checking.

---

# 8. Why Counterexamples Are Important

An Alive2 counterexample can provide much richer information than a simple test failure.

Instead of:

```text
FAIL
```

the verifier can expose a semantic reason why the transformation is invalid.

Conceptually:

```text
Original
   ↓
Optimization
   ↓
Invalid refinement
   ↓
Counterexample
```

This is potentially powerful feedback for an LLM.

However, the counterexample may contain more information than the LLM actually needs.

It may include:

* many LLVM instructions
* irrelevant instructions
* long SSA dependency chains
* control-flow context
* values unrelated to the root violation
* verbose verifier information

Thus the LLM may have to solve two problems simultaneously:

```text
1. Understand the counterexample.
2. Infer what compiler assumption is wrong.
```

This motivates our research.

---

# 9. Proposed Research Contribution

Our proposed contribution is:

## IR-Aware Semantic Counterexample Minimization

The system should transform:

```text
Raw Alive2 Counterexample
```

into:

```text
Compact Semantic Counterexample
```

while preserving the reason why the LLVM transformation is invalid.

The intended pipeline is:

```text
Candidate LLVM Patch
        ↓
Alive2
        ↓
Raw Counterexample
        ↓
IR-Aware Minimizer
        ↓
Semantic Validation
        ↓
Reduced Counterexample
        ↓
LLM
        ↓
New Candidate Patch
```

The key requirement is:

> **The reducer must preserve the semantic violation, not merely make the textual representation shorter.**

---

# 10. What "IR-Aware" Means

The minimizer should use LLVM-specific structure and semantics.

Potential information includes:

### SSA dependencies

Preserve values needed by the semantic witness.

### Def-use chains

Avoid deleting definitions required by the transformation or failing witness.

### CFG/basic-block structure

Respect branches, dominance, PHI nodes, and reachability.

### Instruction semantics

Reason about instructions such as:

* GEP
* PHI
* arithmetic
* casts
* comparisons
* memory operations
* vector instructions
* calls

### LLVM semantic properties

Potentially preserve conditions involving:

* poison
* undef
* `inbounds`
* `nuw`
* `nsw`
* integer overflow semantics
* pointer semantics
* aliasing-related assumptions

### Alive2 validity

Every candidate reduction should be validated to ensure that it still demonstrates the relevant invalid refinement.

Therefore:

```text
smaller ≠ automatically better
```

The desired property is:

```text
smaller
+
still semantically valid as a counterexample
+
still relevant to the compiler bug
```

---

# 11. Important Existing Related Work

Our literature review must explicitly compare the proposed method with existing work.

## 11.1 llvm-autofix

**What it establishes:**

LLM-based agents can be specialized for repairing real LLVM compiler bugs using compiler-specific tooling and benchmarks.

**What it means for our research:**

This is a primary baseline.

We should ask:

> What happens when the repair agent receives different forms of verification feedback?

We should not claim that LLM-based LLVM repair itself is novel.

---

## 11.2 ReduceFix

The paper *Input Reduction Enhanced LLM-based Program Repair* introduces **ReduceFix**, which reduces failure-inducing test inputs before sending them to an LLM repair system.

Its motivation is that large failing inputs can overwhelm LLM context and cause loss of important information. ReduceFix automatically generates a reducer and then feeds the reduced input to the repair model. The paper reports substantial improvements in repair performance and compares against approaches including ddmin-style reduction.

This work is extremely relevant.

However, its target is:

```text
general failure-inducing program/test input
```

whereas ours is intended to target:

```text
LLVM IR
+
Alive2 formal-verification counterexample
+
compiler semantic violation
```

Therefore our research question becomes more specific:

> **Does compiler/IR-aware semantic reduction provide additional value over generic failure-input reduction when the repair target is an LLVM optimization bug?**

This comparison is essential.

---

## 11.3 PGS

The work *Effective LLM Code Refinement via Property-Oriented and Structurally Minimal Feedback* introduces PGS, which argues that LLM refinement benefits from feedback that is:

* property-oriented
* structurally minimal

PGS uses minimal failing counterexamples to provide more targeted feedback and reports improvements over debugging/TDD baselines. It also directly studies different notions of minimization and finds strong benefits from structurally simpler feedback.

This means we must NOT claim:

> "Nobody has shown that minimal counterexamples help LLMs."

That claim would be false.

Instead, our more precise research question is:

> **Do LLVM-specific, formally verified, semantically minimized counterexamples provide an advantage over generic minimal feedback for compiler repair?**

---

# 12. The Research Gap

The research gap should be formulated around the **intersection** of these areas:

```text
LLM-based APR
      +
Compiler-specific repair
      +
LLVM IR
      +
Formal verification
      +
Alive2 counterexamples
      +
Semantic/structural minimization
```

The important question is not simply:

> "Does minimizing input help?"

Prior work already gives evidence that it can.

The question is:

> **What kind of minimization is appropriate when the failing object is a formally generated LLVM IR counterexample representing an invalid compiler transformation?**

And:

> **Does LLVM-specific semantic awareness matter beyond generic reduction?**

That is the core research opportunity.

---

# 13. Proposed Main Research Question

The primary research question is:

> **Can LLVM-IR-aware semantic minimization of Alive2 counterexamples improve the effectiveness, efficiency, and reliability of LLM-based automated repair for real-world LLVM compiler optimization bugs?**

---

# 14. Secondary Research Questions

### RQ1 — Counterexample Reduction

Can an IR-aware reducer significantly reduce Alive2 counterexample complexity while preserving the semantic violation?

### RQ2 — Repair Effectiveness

Does the reduced counterexample improve the probability that an LLM generates a correct LLVM patch?

### RQ3 — IR Awareness

Does IR-aware reduction outperform generic/textual reduction?

### RQ4 — Efficiency

**Decided 2026-08-27 (Blocker 2, `docs/IMPLEMENTATION.md` §9):** each repair
iteration means an LLVM rebuild — minutes, not the seconds a prompt-token
difference would cost. Saving a few hundred prompt tokens is statistical
noise against that, so this RQ is claimed in terms of:

* **LLM iterations to fix** (primary — the thing an extra build actually costs)
* **number of builds** and **number of Alive2 calls** (both proportional to
  iterations, reported as corroborating detail)

Tokens and wall-clock time are still recorded (`ce/benchmark.py`'s `RunLog`),
but are **not** the efficiency claim — report them as descriptive context
only, never as the headline "X% more efficient" number.

Does minimization reduce these?

### RQ5 — Feedback Representation

Does structured semantic annotation improve repair beyond the raw reduced counterexample?

### RQ6 — Interaction Effects

Is the strongest performance obtained by combining:

```text
minimization
+
structured annotation
```

rather than either technique alone?

---

# 15. Main Experimental Design

The experiment should contain several controlled conditions.

## Condition A — Existing LLVM Repair Baseline

Use the existing repair workflow.

```text
Bug
 ↓
LLM
 ↓
Patch
 ↓
Build/Test/Verify
 ↓
Feedback
```

This establishes the baseline performance.

---

## Condition B — Raw Alive2 Feedback

Provide the raw Alive2 counterexample to the LLM.

```text
Bug
 ↓
LLM Patch
 ↓
Alive2
 ↓
Raw Counterexample
 ↓
LLM
```

This isolates the effect of formal verification feedback.

---

## Condition C — Generic Reduction

Reduce the counterexample using a generic reduction strategy that does not exploit LLVM-specific semantic structure.

For example:

```text
Raw Counterexample
       ↓
Generic / textual / delta-debugging reduction
       ↓
Reduced Counterexample
       ↓
LLM
```

This is required because previous work such as ReduceFix demonstrates that generic failure-input reduction can itself improve LLM repair.

---

## Condition D — IR-Aware Semantic Reduction

Use our proposed LLVM-specific reducer.

```text
Raw Alive2 Counterexample
        ↓
LLVM-aware reduction
        ↓
Semantic preservation check
        ↓
Reduced LLVM IR witness
        ↓
LLM
```

This is the primary proposed method.

---

## Condition E — Structured Feedback

Instead of presenting only raw/reduced LLVM IR, annotate the counterexample with structured information.

Possible fields:

```text
Bug type
Transformation being checked
Source instruction(s)
Target instruction(s)
Violated semantic property
Critical SSA values
Relevant dependency chain
Relevant basic blocks
Counterexample input/state
Why the target fails refinement
```

The purpose is to make the verifier output more actionable for an LLM.

---

## Condition F — IR-Aware Reduction + Structured Feedback

Combine both proposed components:

```text
Alive2
  ↓
IR-aware minimization
  ↓
Semantic annotation
  ↓
LLM
```

This should be the strongest proposed configuration.

---

# 16. A Useful Experimental Matrix

A particularly clean design is:

| Feedback     | Raw | Generic Reduced | IR-Aware Reduced |
| ------------ | --: | --------------: | ---------------: |
| Unstructured |   A |               B |                C |
| Structured   |   D |               E |            **F** |

This allows us to study:

1. Does reduction help?
2. Does structure/annotation help?
3. Does LLVM-aware reduction help beyond generic reduction?
4. Is there a benefit from combining reduction and annotation?

This is much stronger than a simple baseline-vs-our-method experiment.

---

# 17. Main Hypotheses

### H1

LLVM-IR-aware semantic minimization improves LLM repair success compared with raw Alive2 counterexamples.

### H2

LLVM-IR-aware reduction outperforms generic counterexample reduction.

### H3

Structured semantic annotation improves repair over unstructured counterexamples of similar size.

### H4

Combining semantic minimization and structured annotation provides the strongest repair performance.

### H5

The proposed method reduces repair cost. Per the RQ4 decision above, this is
claimed primarily as **fewer LLM iterations** (and the build/verification
cycles that scale with iteration count), not as fewer tokens or less
wall-clock time — those are recorded but are noise next to per-iteration
LLVM rebuild time.

The AI should treat these as **hypotheses to test**, not established facts.

---

# 18. Evaluation Metrics

The primary metric should be:

## Correct Repair Rate

```text
Successfully repaired bugs
--------------------------
Attempted bugs
```

But success must be defined carefully.

A patch should not count as successful merely because:

```text
C++ compiles
```

or:

```text
original reproducer passes
```

The repair should satisfy the benchmark's appropriate validation criteria, including broader regression testing and formal verification where applicable.

Secondary metrics — **efficiency claims are made via `number of iterations`
(and the correlated `total builds`/`total verifier calls`), per the RQ4
decision above; `wall-clock time` and `LLM token usage` are recorded but are
descriptive context, not the efficiency claim, since one iteration is an LLVM
rebuild measured in minutes**:

* repair success rate
* pass@k / success within N attempts
* number of iterations
* number of failed patches
* compilation failures
* regression-test failures
* Alive2 failures
* total builds
* total verifier calls
* wall-clock time
* LLM token usage
* prompt length
* counterexample size before reduction
* counterexample size after reduction
* reduction ratio
* patch size
* semantic distance/complexity metrics where appropriate

---

# 19. Important Distinction: Intermediate vs End-to-End Success

The research should not optimize only for:

```text
counterexample size ↓
```

A tiny counterexample is not automatically useful.

The true objective is:

```text
counterexample reduction
       ↓
better LLM reasoning
       ↓
better repair
```

Therefore:

> **Downstream repair performance is the primary scientific metric.**

Counterexample size is an intermediate metric.

A reducer that shrinks the counterexample by 90% but lowers repair success would not be a successful contribution.

---

# 20. What We Should Compare Against in the Literature

The literature review should classify relevant systems along dimensions such as:

| Dimension           | Questions                                                           |
| ------------------- | ------------------------------------------------------------------- |
| Repair target       | General programs, repositories, compilers, LLVM?                    |
| LLM role            | Generator, fixer, debugger, agent?                                  |
| Localization        | Given, automatic, search-based?                                     |
| Feedback            | Tests, compiler errors, traces, properties, counterexamples?        |
| Formal verification | None, SMT, translation validation, Alive2?                          |
| Reduction           | None, generic input reduction, delta debugging, semantic reduction? |
| Representation      | Raw test, structured explanation, IR, traces?                       |
| Iteration           | One-shot or iterative?                                              |
| Dataset             | Synthetic, benchmark, OSS-Fuzz, LLVM real bugs?                     |
| Correctness         | Tests only, formal verification, regression testing?                |
| Cost                | Tokens, time, builds, verifier calls?                               |

The AI must clearly distinguish:

```text
Existing published technique
Existing repository infrastructure
Our proposed extension
Our experimental hypothesis
Empirically demonstrated result
```

Never collapse them into one category.

---

# 21. What the Paper's Contribution Should NOT Be Claimed As

Do not claim:

> "We are the first to use LLMs to repair LLVM bugs."

Already established by llvm-autofix and related work.

Do not claim:

> "We are the first to reduce failing inputs for LLM repair."

ReduceFix already establishes this general concept.

Do not claim:

> "We are the first to show that minimal feedback helps LLMs."

PGS directly studies structurally minimal, property-oriented feedback.

Instead, investigate:

> **Whether LLVM-IR-aware semantic minimization of formally generated Alive2 counterexamples yields additional benefits for real LLVM compiler repair beyond generic reduction and generic minimal-feedback strategies.**

That is the narrower and stronger claim to test.

---

# 22. Proposed Architecture

The complete system should be conceptually:

```text
                    ┌───────────────────────┐
                    │  Real LLVM Bug        │
                    │  + LLVM IR Reproducer │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │ Localization /        │
                    │ Source Context        │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │         LLM           │
                    │   Candidate Repair    │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │   Build Modified      │
                    │       LLVM            │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │ Reproducer + Regression│
                    │ Tests                  │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │        Alive2         │
                    │ Formal Verification   │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │ Raw Counterexample    │
                    └───────────┬───────────┘
                                │
                   ┌────────────┴────────────┐
                   │                         │
                   ▼                         ▼
          Generic Reduction          IR-Aware Reduction
                   │                         │
                   │                  ┌──────┴──────┐
                   │                  │ LLVM SSA    │
                   │                  │ CFG         │
                   │                  │ Def-use     │
                   │                  │ Semantics   │
                   │                  │ Alive2      │
                   │                  └──────┬──────┘
                   │                         │
                   └────────────┬────────────┘
                                ▼
                    ┌───────────────────────┐
                    │ Structured Feedback  │
                    │ (optional factor)    │
                    └───────────┬───────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │         LLM           │
                    │ Diagnose + Repair     │
                    └───────────┬───────────┘
                                │
                                └──────► Iterate
```

---

# 23. Example LLVM Bug Intuition

One benchmark example involves an InstCombine bug related to propagation of GEP no-wrap information.

The optimizer can attach semantic guarantees such as:

```text
inbounds
nuw
nsw
```

to transformations.

If those guarantees are propagated when they are not actually justified, LLVM can produce an invalid optimization.

This illustrates the kind of problem we care about:

```text
The generated IR may look syntactically correct
but violate LLVM semantic refinement.
```

A formal verifier can identify this, but the resulting counterexample may still be larger than necessary for an LLM to reason about.

The minimization problem therefore becomes:

```text
Preserve:
   the exact semantic condition that makes
   the optimization invalid

Remove:
   irrelevant context/instructions
```

This is the central technical challenge.

---

# 24. Possible IR-Aware Reduction Strategy

The exact algorithm is intentionally not fixed yet.

Potential components include:

### Dependency slicing

Start from the instructions implicated in the Alive2 violation and follow SSA/def-use dependencies.

### CFG reduction

Remove irrelevant basic blocks/control-flow paths where possible.

### Instruction deletion

Attempt candidate removals and re-run the relevant verifier.

### Semantic delta debugging

Repeatedly reduce the IR while checking whether the same refinement failure remains.

### Hierarchical reduction

A possible sequence is:

```text
Modules/functions
   ↓
Basic blocks
   ↓
Instructions
   ↓
Operands
   ↓
Constants / values
```

### Hybrid strategy

Potentially combine static LLVM-IR analysis with dynamic/formal verification.

The final algorithm should be selected after studying the exact Alive2 counterexample representation and related program-reduction literature.

---

# 25. Structured Alive2 Feedback

The second proposed contribution is a **structured representation of the verifier result**.

Instead of only providing:

```text
Raw Alive2 output
```

provide something resembling:

```text
BUG TYPE:
    Miscompilation

VERIFICATION RESULT:
    Invalid refinement

SOURCE:
    <relevant source IR>

TARGET:
    <relevant target IR>

VIOLATED PROPERTY:
    <semantic property>

CRITICAL INSTRUCTIONS:
    <instruction IDs>

DEPENDENCY CHAIN:
    <SSA chain>

CONTROL FLOW:
    <relevant blocks>

COUNTEREXAMPLE:
    <minimal semantic witness>

INTERPRETATION:
    <concise explanation of why the target transformation is invalid>
```

The exact fields should be determined experimentally rather than hard-coded prematurely.

---

# 26. Why Structured Feedback Is a Separate Research Dimension

It is possible that reducing the IR alone is not enough.

An LLM may still struggle to interpret a formally precise but cryptic LLVM witness.

Therefore we want to separate:

```text
Information quantity
```

from:

```text
Information organization
```

For example:

```text
Raw, large counterexample
vs.
Raw, structured counterexample
vs.
Reduced, unstructured counterexample
vs.
Reduced, structured counterexample
```

This lets us answer whether the benefit comes from:

* less information,
* better organization,
* LLVM-specific semantics,
* or the combination.

---

# 27. Core Comparison Matrix

The preferred conceptual comparison is:

| Method                | Formal Feedback | Generic Reduction | LLVM/IR-Aware Reduction | Structured Feedback |
| --------------------- | --------------: | ----------------: | ----------------------: | ------------------: |
| Existing baseline     |               ✓ |                 — |                       — |                   — |
| Raw Alive2            |               ✓ |                 — |                       — |                   — |
| Generic reduction     |               ✓ |                 ✓ |                       — |                   — |
| IR-aware reduction    |               ✓ |                 — |                       ✓ |                   — |
| Structured feedback   |               ✓ |                 — |                       — |                   ✓ |
| IR-aware + structured |               ✓ |                 — |                       ✓ |                   ✓ |

The exact implementation details can vary, but the scientific comparison should isolate the contribution of each factor.

---

# 28. Threats to Validity

The research should explicitly consider:

### Model dependence

Results may depend on the LLM used.

Where possible, test more than one model or clearly restrict the claim to the evaluated models.

### Benchmark dependence

The benchmark consists of known LLVM bugs and may not represent every class of compiler failure.

### Localization assistance

The benchmark provides source-location hints, so the system is not fully autonomous from bug discovery through repair.

### Verification dependence

Alive2 does not cover every possible compiler correctness question or every bug class identically.

### Reducer bias

A reduction strategy may disproportionately benefit certain LLVM instruction patterns.

### Token-size confounding

A performance improvement may come from simply reducing prompt length rather than from deeper semantic understanding.

Therefore the generic-reduction baseline is important.

### Search/iteration budget

Every method must have comparable:

* LLM calls
* verification budget
* time budget
* attempt count

so that one approach is not given unfairly more opportunities.

---

# 29. What Would Constitute a Strong Result?

A strong result would look conceptually like:

```text
Raw Alive2
    ↓
Baseline repair success

Generic reduction
    ↓
Improved repair success

IR-aware reduction
    ↓
Further improvement

IR-aware + structured feedback
    ↓
Best performance
```

Even stronger would be evidence showing:

```text
similar counterexample size
        but
higher repair success
```

for the IR-aware representation.

That would suggest that the benefit is not merely compression.

The strongest scientific result would be:

> **LLVM semantic structure itself matters for converting formal verification output into useful LLM repair guidance.**

---

# 30. Desired End-to-End Research Story

The paper should tell the following story:

```text
Problem
------
LLVM compiler bugs are difficult for LLMs to repair because
correctness depends on compiler-specific semantics.

Existing Work
-------------
llvm-autofix demonstrates that compiler-specific LLM agents
can repair real LLVM bugs.

Related Work
------------
ReduceFix shows that reducing failure-inducing inputs can
improve LLM-based APR.

PGS shows that property-oriented and structurally minimal
feedback can improve iterative LLM refinement.

Gap
---
However, these observations do not directly answer whether
formal LLVM optimization counterexamples should be reduced
using LLVM-specific semantic structure.

Research Question
-----------------
Does IR-aware semantic minimization of Alive2 counterexamples
improve LLM-based LLVM bug repair?

Method
------
Build an LLVM-aware counterexample reducer and optionally
structure the resulting verifier feedback.

Evaluation
----------
Compare raw, generic-reduced, IR-aware-reduced, structured,
and combined feedback strategies on real LLVM bugs.

Outcome
-------
Measure repair correctness, efficiency, and feedback quality.
```

---

# 31. Working Paper Contribution Statement

A safe initial contribution statement is:

> **We investigate LLVM-IR-aware semantic counterexample minimization as a feedback optimization technique for LLM-based compiler bug repair. We integrate an IR-aware reduction stage into an LLVM repair workflow using Alive2 and systematically compare raw, generic-reduced, and LLVM-specific counterexample representations, with an additional study of structured semantic annotations.**

Do not replace “investigate” with “prove,” “establish,” or “demonstrate” until the experiments actually support those claims.

---

# 32. What the AI Assistant Should Do When Helping With This Project

When asked to help with this project, the AI should reason simultaneously about:

### LLVM/compiler correctness

Understand compiler transformations and LLVM semantics.

### Formal verification

Understand Alive2 and refinement failures.

### Program reduction

Reason about semantics-preserving counterexample reduction.

### LLM behavior

Consider context length, information density, feedback quality, iterative repair, and reasoning burden.

### Experimental methodology

Use controlled baselines and ablations.

### Research novelty

Constantly distinguish:

```text
already known
vs.
existing repository functionality
vs.
our proposed method
vs.
our hypothesis
vs.
our measured result
```

The AI must not overclaim novelty.

---

# 33. One-Line Research Thesis

The current working thesis is:

> **Existing LLVM LLM-repair systems show that compiler-specific agents can repair real LLVM bugs, while general APR research shows that reduced and structured failure feedback can improve LLM repair; our research asks whether LLVM-IR-aware, semantics-preserving minimization and structured representation of Alive2 counterexamples can provide a stronger feedback signal and thereby improve real LLVM compiler-bug repair.**

---

# 34. Final Research Objective

The ultimate goal is **not simply to make counterexamples smaller**.

The goal is:

```text
Better formal feedback
        ↓
Better LLM understanding
        ↓
More correct LLVM patches
        ↓
Fewer repair iterations
        ↓
Lower cost
        ↓
More reliable automated compiler repair
```

The central scientific question is therefore:

> **Does the representation of formal verification feedback materially affect LLM-based compiler repair, and does LLVM-specific semantic awareness outperform generic input reduction?**
