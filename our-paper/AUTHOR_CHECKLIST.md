# Author checklist

Manuscript: `main.tex` (build: `pdflatex`, `bibtex`, `pdflatex` x2; `IEEEtran` conference class).
Snapshot: `Tanish-Desai/better-compiler`, branch `feat/counterexample-toolkit`, commit
`b1d6bfe6236a0da7d7aeb85ae2a03bd2518dadbd`. The remote branch tip was still this commit on 2026-10-06
(`git ls-remote`), so nothing had advanced. A clone is in `../repo`.

Every `[...]` in red in the PDF is a placeholder. The sections below explain what each one needs.

## 1. Missing results (all outcome content is blank by design)

- Tables IV-VII and the paragraph after them: paired repair outcomes, pass@3/pass@1, iterations,
  McNemar counts b/c, differences, p and BH-adjusted p, reducer size/verifier-call measurements,
  promotion ablation, share of turns with an Alive2 failure, fallback causes, patch-application outcomes.
- Table II last row: number of bugs analyzed after run-time exclusions.
- Discussion bullets: nothing can be written until results exist.
- No `results/` directory exists in the repository (`git ls-tree`: 0 entries). The numbers in the
  repository docs (28 -> 4 instructions, 183/351/17 oracle calls, etc.) come from one hand-built sample,
  need `alive-tv`, and were deliberately left out.

## 2. Configuration to confirm (not determinable from this branch)

| Item | What the sources say |
|---|---|
| Model | Code default `deepseek-reasoner` (`examples/repair_experiment.py:118`). `docs/IMPLEMENTATION.md` and `docs/SLM_SELECTION.md` say `Qwen3-Coder-30B-A3B-Instruct`. `docs/PROJECT_STATUS.md` says `Qwen2.5-Coder-14B-Instruct`, but it describes branch `feat/h100-sweep` @`4c07c52`. The manuscript names no model. |
| Sampling | Temperature default 0.8 (`LAB_LLM_TEMP`). `SLM_SELECTION.md` s8 says "0.8 with top-p 0.95"; the client passes no top-p, no max tokens. |
| Serving | Quantization, context length, vLLM settings: only in docs for the other model/branch. |
| Self-declared cutoff | `LAB_LLM_BASEMODEL_CUTOFF` default 2023-12-31 (unverified by the harness). |
| Benchmark copy | `llvm-apr-benchmark/` is vendored with its `.git` removed; upstream commit not recorded. README there says it moved to `dtcxzyw/llvm-autofix`. |
| Alive2 / LLVM | Alive2 `f9a4f02f` (Dockerfile). `llvm-reduce` is whatever the per-bug LLVM build at `base_commit` produces, so its version varies by bug. |
| Preregistration | `docs/ANALYSIS_PLAN.md` (commit `0c4b854`, 2026-09-04) is repository-internal. Check whether the venue wants an external registry. |
| Authors, affiliations, funding, archival DOI | Placeholders. |

## 3. Implementation issues found while reading the code (decide before running the sweep)

1. **Verifier arguments are dropped in the reduction path.** `repair_experiment.py` never passes
   `extra_args`; the benchmark's per-test `additional_args` (e.g. `-src-unroll=4 -tgt-unroll=4`) are not in the
   `alive2_check` result dict. The oracle and the post-reduction re-verification therefore run `alive-tv`
   without them. Affects 5 of the 24 sample bugs: 126012, 114879, 91369, 180870, 91957. Effect unmeasured
   (probably a no-op reduction for those bugs, but I could not run `alive-tv`).
2. **Budget exhaustion discards the whole reduction.** The final `oracle.check` is refused when
   `calls >= max_calls`, so the code returns the original pair. I confirmed this with a stubbed verifier
   (budgets 1-3 returned 28 -> 28 instructions; 400/unlimited returned 28 -> 4). Same pattern in
   `reduce_generic` and `reduce_llvmreduce`.
3. **`ddmin` deviates from Zeller-Hildebrandt**: on failure it sets granularity to `len(current)` rather than
   `min(|c|, 2n)` (`ce/reduction.py:162`). The result is still 1-minimal if not stopped; the manuscript says so.
4. **`plain` and `structured` differ in content, not only layout** (`ce/structured.py`): structured omits the
   per-value traces that Alive2's report contains and adds a diff, dependency chain and interpretation.
5. **`raw-plain` is not the benchmark's baseline text.** `render_plain` docstring claims it reproduces the
   benchmark exactly; `baseline.py` sends `json.dumps(first failed test)` (includes test body, args, log dict).
6. **The runner is not "baseline.py with exactly one line changed"** (`docs/METHODOLOGY.md` s1; runner
   docstring). Differences: hint path only, function name omitted from the prompt, fixed message on window
   mismatch, skip rules, no record when the provider fails before the first reply, `Issue body` line absent.
7. **Window and prompt predate the Blocker 16 fixes.** This branch has the fixed +/-30-line window (no brace
   balancing), no no-op detection, and the original format requirement. The other branch's docs report these
   caused a 64.5% unchanged-reply rate in a discarded sweep. I did not use that number in the paper.
8. **Ablation direction.** `analyze_significance.py --ablation` tests one-sided "promotion on > promotion off".
   `docs/ANALYSIS_PLAN.md` frames it as "does disabling change outcomes" and calls a null the good outcome.
9. **`mean_iterations` averages over all runs including failures** (censored at 4). Define the iterations
   analysis before running.
10. **BH is applied over however many primary comparisons have data** (`len(primary)` after skips), not a fixed 4.
11. **No check that conditions have equal numbers of trials** before pass@k is compared; interrupted cells count
    as one record.
12. **Non-Alive2 feedback is not truncated** (`max_log_size` = 1e9): long build logs can exceed model context.
13. **Oracle timeout**: `Oracle` default is 60 s, but the pipeline passes 120 s.
14. **Reducer scope**: only the named function is reduced; edits drop comment-only lines attached to block labels.

## 4. Documentation versus code / other discrepancies (not resolved in the manuscript)

- `docs/PROJECT_STATUS.md` is on this branch but describes `feat/h100-sweep` (it is absent from that branch's
  tree). Blockers 15-16 and the h100 tooling are not in this branch's code or `IMPLEMENTATION.md`.
- `docs/METHODOLOGY.md` s3 says passes run "to a fixpoint"; the code stops after `max_rounds=4`.
- `docs/METHODOLOGY.md` s3 / `reduce_iraware.py` docstring say candidates are "valid IR by construction";
  the code only closes over def-use and branch conditions.
- `docs/SLM_SELECTION.md` s7: "10 of the 24" sample bugs have 2025+ cutoffs; recounted: **11**.
- `scripts/power_analysis.py` reproduces the documented table to within 0.01 (e.g. 0.32 vs 0.33).
- README still shows six-condition tables and "not yet run"; Dockerfile comments mention 295 bugs.
- Sample has 16 distinct primary component labels, one of which is `unknown` (bug 179613, no component hint).
- `our-paper/llvm_counterexample_reduction.pdf` (checked as an existing draft, not used as evidence):
  it says its method content comes from `feat/h100-sweep`; lists a different first primary comparison
  (`iraware-structured` vs `baseline`; the plan and code have `raw-plain` vs `baseline`); says the prompt
  includes the issue body (code sends the title only); counts "16 LLVM components" including `unknown`;
  cites `automated_program_repair.pdf` as "Survey of automated program repair" although it is an ICSME 2025
  fine-tuning study, and gives it the wrong directory.
- Counts re-derived: 491 files; 340/142/9; 135 Alive2-checked miscompilations; 103 single-function-fix
  miscompilations including 3 `lli`-only; 100 eligible; 99 pool; sample matches the committed list.

## 5. What I could not verify

- Anything needing `alive-tv`, `llvm-reduce`, or a built `opt`: 18 integration tests were skipped
  (45 of 63 tests ran, all passed). Parsing, IR round-trip (1462/1462), sample selection, power script ran.
- That all 24 bugs have a working build, the 20-hour build claim, and the reproduction logs (gitignored).
- Reducer behavior on any real witness; Alive2 output format drift across versions.

## 6. Literature: reading depth and gaps

No papers were attached to the chat; I used the repository's `ref_papers/` and `slm_research_papers/`.
All 18 PDFs were readable. `agentic_harness.pdf` is byte-identical in both folders (same MD5), so 17 unique
papers. I read all but the following in depth.

| Paper | Depth | Cited? |
|---|---|---|
| Alive2 (PLDI'21) | Sections 1-8 | yes |
| Csmith (PLDI'11) | abstract, intro, the reduction passage | yes |
| Delta debugging (TSE 2002) | algorithm, minimality definitions | yes |
| ReduceFix, PGS, ChatRepair | abstract, method, setup, relevant results | yes |
| llvm-harness (Zheng et al.) | abstract, intro, setup, expert-review section, McNemar appendix | yes |
| LLVM-Bench | abstract, intro, results summary | yes |
| LLM Compiler (Meta) | abstract, intro | yes |
| RepairAgent, survey (Yang et al.), RepairLLaMA | abstract and intro only | yes, for narrow claims |
| HEJ-Robust | abstract, intro | yes, one sentence |
| SLM-as-a-judge | statistics passage only | yes (McNemar + BH use) |
| How Small is Enough, SLMFix, ICSME'25 fine-tuning study | front matter only | **no** (model-selection material) |

Not in the provided set (bibliographic record checked on the web, papers not read): McNemar 1947,
Benjamini-Hochberg 1995. The `llvm-reduce` page was fetched and checked (it states the `--test` exit-code
mechanism and delta passes at several granularities); the LangRef URL is taken from the Alive2 paper.
Update (proofreading pass): ReduceFix, RepairAgent, RepairLLaMA, SLM-as-a-judge and PGS now cite their
published versions; DOIs/pages were checked against Crossref (PGS pages against a PMLR search result).
The ReduceFix figures (89.1%, 53.8%) were read in the arXiv v2 PDF/abstract; the ACM page was not accessible,
so confirm they are unchanged in the published version.
The manuscript does not cite HDD/Perses/ddSMT/J-Reduce, C-Reduce or the original Alive paper because none
was read; add them if the related-work section should cover syntax-aware reducers.

## 7. Venue items

- **AI-use disclosure**: the Acknowledgments contain a placeholder. IEEE venues generally ask for disclosure of
  AI-generated text; check the specific call.
- Template: `IEEEtran` conference class unmodified. The manuscript is 9 pages; check the page limit.
- Anonymity: the repository URL and commit are in the Implementation section; remove for double-blind review.
- The statistical plan is exploratory-scale (see s6.F of the paper); reviewers may ask for a power justification.
