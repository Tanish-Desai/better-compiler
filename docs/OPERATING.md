# Running the sweep, day to day

This is the short, plain page. Setup lives in
[`RUNBOOK_NATIVE.md`](RUNBOOK_NATIVE.md) (or [`RUNBOOK.md`](RUNBOOK.md) if you
can use Docker). Once setup is done, this is all you need.

**You do not need to set any environment variables.** `scripts/setup_native.sh`
wrote them to `~/better-compiler-runtime/env.sh` and made `~/.bashrc` load that
file, so every new shell already has them. If you ever want to check:

```bash
echo $LAB_LLM_URL        # should print http://127.0.0.1:8000/v1
```

If that prints nothing, run `source ~/better-compiler-runtime/env.sh` once, or
open a fresh shell.

---

## 1. Start the model server

It must be running before the sweep, and must stay running the whole time.

```bash
tmux new -s vllm

vllm serve Qwen/Qwen2.5-Coder-14B-Instruct \
    --served-model-name qwen2.5-coder-14b \
    --quantization fp8 \
    --max-model-len 12288 \
    --gpu-memory-utilization 0.25 \
    --enforce-eager \
    --host 0.0.0.0 --port 8000 \
    --api-key local-sweep
```

Press `Ctrl-b` then `d` to leave it running in the background.

Check it answers before going further:

```bash
python3 scripts/check_llm_endpoint.py --repeat 3
```

## 2. Start the sweep

```bash
tmux new -s sweep

python3 examples/repair_experiment.py \
    --sample --repeat 3 --out results/ \
    --build-jobs 64 \
    --condition baseline raw-plain generic-plain llvmreduce-plain iraware-plain \
                raw-structured generic-structured llvmreduce-structured \
                iraware-structured
```

`Ctrl-b` then `d` to leave it. It will take days.

## 3. Check on it

```bash
tmux attach -t sweep          # watch it; Ctrl-b then d to leave again
ls results/*.json | wc -l     # how many of the 648 cells are done
nvidia-smi                    # is the GPU still there
```

To see results so far:

```bash
python3 examples/summarize_results.py results/
```

---

## 4. If something breaks

**The short version: stop it, fix the thing, run the exact same command again.**
It picks up where it left off. It never redoes finished work, and it never
keeps a broken result.

### The sweep stopped

Run the same command from step 2 again. Cells that finished are skipped.

### The GPU server (vLLM) died

Start it again (step 1), then start the sweep again (step 2). Any cell that was
interrupted by the server dying gets redone automatically — it is not counted
as a failed repair.

### vLLM will not start: "Free memory ... is less than desired"

Someone else's job is using the GPU. Check how much is actually free:

```bash
nvidia-smi --query-gpu=memory.free --format=csv
```

If it is under about 20 GB, lower the numbers in step 1 to
`--max-model-len 8192` and `--gpu-memory-utilization 0.24`. If it is under
about 17 GB, wait — there is not enough room for the model right now.

### vLLM will not start: "No available memory for the cache blocks"

Same fix: lower `--max-model-len`, keep `--gpu-memory-utilization` where it is.

### The machine rebooted

Open a new shell, start vLLM (step 1), start the sweep (step 2). Nothing is
lost except the cell that was mid-run.

### Something else

Look at the last few lines before it stopped. If a cell failed, the sweep
prints the error and moves on to the next cell — one bad cell does not stop
the run.

---

## 5. Two things worth checking as it runs

### Is the counterexample reducer actually working?

There is a known open question here — see Blocker 15 in
[`IMPLEMENTATION.md`](IMPLEMENTATION.md). On some bugs the `iraware` reducer
does nothing, which makes that bug useless for the main comparison. Check
after roughly 10 bugs have finished:

```bash
python3 -c "
import json, glob, collections
noop = collections.Counter(); total = collections.Counter()
for f in glob.glob('results/*.json'):
    d = json.load(open(f))
    if not d['condition'].startswith('iraware'): continue
    for it in d['iterations']:
        fb = it['feedback']
        if not fb.get('counterexample'): continue
        total[d['bug_id']] += 1
        if not fb.get('passes_applied'): noop[d['bug_id']] += 1
bad = sorted(b for b in total if noop[b] == total[b])
print(f'bugs checked: {len(total)}   bugs where iraware did nothing: {len(bad)}')
print(bad)
"
```

- **2 or fewer out of 10** — fine, keep going.
- **4 or more out of 10** — stop and tell someone. Roughly 10 of the 24 bugs
  would be wasted, and the experiment would be biased toward finding nothing.

### How long is this going to take?

Time how long the **first** bug takes, start to finish. Multiply by 24. That is
your estimate. The pilot measured about 76 seconds per turn once a bug is
built, but building each bug for the first time is the slow part and has never
been measured on this machine.

---

## 6. When it finishes

```bash
python3 examples/summarize_results.py results/
python3 examples/analyze_significance.py results/
```

Copy `results/` somewhere safe before doing anything else — it is days of
compute, and it is not in git (`results/` is gitignored on purpose).

---

## 7. Command reference

| what | command |
|---|---|
| start model server | see step 1 |
| check server works | `python3 scripts/check_llm_endpoint.py --repeat 3` |
| start / resume sweep | see step 2 |
| watch it | `tmux attach -t sweep` |
| leave it running | `Ctrl-b` then `d` |
| how far along | `ls results/*.json \| wc -l` (648 when done) |
| results so far | `python3 examples/summarize_results.py results/` |
| final statistics | `python3 examples/analyze_significance.py results/` |
| redo one cell | add `--overwrite --condition <name> <bug_id>` |
