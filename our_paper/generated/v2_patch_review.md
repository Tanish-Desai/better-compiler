# Frozen v2 diagnostic and passing-patch review

Snapshot: `73f2ba151742c44cf88878b50c027ef7f06293c6`.

This is an offline source-inspection audit, not an independent human expert review or a new semantic validation run. Reference patches were inspected only after generation and were not sent to a model. All ten saved passing patches remain harness-plausible, with correctness review pending.

The seven passing records for bug 89390 fall into three guard families:

- IR-aware plain trial 2 and all three raw structured trials skip signed-remainder folding when the shuffle mask has poison. The reference handles all integer division/remainder opcodes. The narrower model guard needs additional opcode-generalization tests.
- LLVM-reduce plain trial 1 and LLVM-reduce structured trial 2 skip all integer division/remainder folding, irrespective of the mask. This is broader optimization suppression than the reference; passing tests do not measure the lost optimization opportunities.
- LLVM-reduce plain trial 2 skips signed/unsigned remainder folding for vector operands. This also suppresses folding beyond the poison-mask condition and needs review.

Both passing records for bug 135182 add a self-addition-specific guard. The reference guards the relevant same-operand widening case more generally. The proposals may repair the listed reproducer while leaving related cases uncovered; no additional semantic check has been run here.

The passing generic plain record for bug 126012 adds an identity-based nonrecursive-reasoning guard. The reference checks whether an incoming value has computable evolution in the loop. These are different mechanisms; reproducer and selected lit success do not establish their equivalence.

No passing record is promoted to expert-correct by this audit. No record is silently relabelled as incorrect solely because it differs from the reference.

Recorded resource totals (all saved runs):

- Wall-duration sum: 23.76 hours.
- Model input/output tokens: 6,936,568 / 1,506,874.
- Recorded oracle calls: 44,695.

The duration sum is not a timestamp-based throughput measurement or an ETA. No API-equivalent dollar cost is inferred for locally served Qwen.
