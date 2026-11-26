# Author checklist

The manuscript reports two completed evaluations. On 24 fresh witnesses, the
standalone IR-aware reducer used fewer verifier calls than the default
source-first `llvm-reduce` wrapper on every case. Mean instruction removal was
8.6% versus 8.9% at a shared 32-call reducer ceiling. The hybrid missed its
prespecified compression criterion. In 192 v3c repair trials, the strongest
observed arm was raw structured feedback at 9 of 16 bugs with a plausible
repair within three trials, compared with 8 for IR-aware structured feedback.
The three prespecified repair comparisons were inconclusive. [Evidence map](EVIDENCE_MAP.md)

## Before submission

- [ ] Scope the main efficiency result to default source-first `llvm-reduce`
  on the 24 fresh witnesses at the recorded stopping points. Report calls and
  instructions removed together. The 32-call reducer ceiling excludes the
  shared establishment check, diagnostic refresh, and independent recheck.
  The 4.5 and 34 medians include refresh and recheck. The single timing run
  per method and case provides a descriptive runtime measurement.
- [ ] Report the focused LLVM reducer and trace-guidance observations as a
  development replay of the same 24 inputs with a different toolchain build.
  The focused LLVM pass schedule removed substantially more instructions in
  this replay, and disabling trace seed and path guidance improved mean
  compression. Retain both outcomes when stating how the contribution compares
  with existing tools. [Local exploratory replay](../run-provenance/witness-guidance-local-summary.json)
- [ ] Recover and deduplicate the 62 full-validation passing proposals by bug
  and applied patch. Review the unique patches for semantic correctness and
  preservation of useful optimizations against the original code and reference
  fix. Review the passing turn for reproducer-only examples because the final
  saved patch may be from a later turn. Use plausible repair for a build,
  listed reproducer, and selected lit pass pending that review.
- [ ] Match every table to the archived protocol, record hashes, and code
  version. Disclose the two v3c substitutions, 36 retained compatible trials,
  prior exposure to bug `115149`, and one hosted model. Counts use three
  completed trials per bug and arm. Preserve endpoint interruptions, missing
  records, output truncations, and compilation failures in the audit.
- [ ] Verify commit and tool hashes and record CPU resources, build concurrency,
  compiler versions, Alive2, `llvm-reduce`, the model identifier and endpoint,
  and effective token and sampling settings. Record current pytest counts and
  skip reasons with the native binaries in scope. Historical 45/18 and 63-pass
  counts, unchanged IR round trips, and bundled fixtures each cover an earlier
  snapshot or a narrower engineering property. [Native integration tests](../tests/test_integration.py)
- [ ] Audit quantitative claims against the cited primary sources. Check that
  the introduction ties the contribution to paired witness reduction and that
  the results distinguish guidance, call cost, compression, and repair.
- [ ] Compile the final PDF and inspect all pages, including citations, figures,
  tables, equations, and references. Check author information and the target
  venue's page limit, disclosure, and submission rules.

## Before the next evaluation

- [ ] Reproduce the focused LLVM reducer and guidance replay on the container.
  Record binary hashes and the exact pass schedule. Treat this as a replication
  on archived inputs. Freeze a disjoint cohort and the strongest sequential
  control before testing a new variant. Collect paired compression and call
  counts across fixed caps. Use repeated single-worker timing if elapsed time
  becomes a headline metric. [Replay runbook](../docs/FRESH_WITNESS_EVALUATION.md#trace-guidance-follow-up)
- [ ] Check whether the reduced source still triggers a violation when passed
  through the original `opt` pipeline. Where a reference fix is available,
  also run the reduced source with the fixed compiler. Report that endpoint
  alongside same-function, same-error-class preservation and study promotion
  separately when it introduces new input degrees of freedom.
- [ ] Use an excluded development case to calibrate the hosted endpoint and
  patch interface before paying for another repair sweep. Measure truncation,
  patch application, builds, reproducer passes, regression passes, token use,
  and cost per arm. Freeze model and endpoint settings, cohort, stopping rule,
  and a primary comparison against raw structured feedback before collecting
  new outcomes. Keep Vertex and Gemini Developer API evidence traceable to
  their respective endpoints.

## Historical design and implementation notes

- V2 is the development snapshot at `73f2ba151742c44cf88878b50c027ef7f06293c6`,
  with runner anchored at `38b381b`. It contains 205 trials on eight bugs and
  a matched panel of 189 trials on seven bugs in the first size tier. Its
  original 24-bug, nine-condition design and the October six-arm v3 proposal
  preceded the completed four-arm Vertex v3c protocol. The
  [protocol history](EVIDENCE_MAP.md#e-protocol-history-between-v2-and-v3c)
  records the later amendments.
- Feedback is available before the first model reply and on later Alive2
  failures. Each iteration stores the feedback consumed by that reply. V2
  used a bounded brace heuristic and a whole-window replacement. V3c uses
  strict SEARCH/REPLACE edits against the original source excerpt and rejects
  output-limit completions.
- V2 omitted per-test Alive2 arguments during reduction and refresh for five
  sampled bugs. V3 forwards those arguments and checks the refreshed failure
  class. Reduction is bounded to four rounds and depends on a line-oriented
  IR model. Independent Alive2 checks establish the recorded violation class
  in the named function under the chosen bounds.
- Structured feedback changes the information shown as well as its layout.
  The raw-plain condition uses the adapted runner and prompt. A direct
  comparison to the upstream repair loop requires matching model, cases,
  budget, and validation.
- The fresh witness study includes a reducer-only promotion ablation. The
  originally proposed promotion-off model repair comparison remains open.
