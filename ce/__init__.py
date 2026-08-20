"""``ce`` = **c**ounter**e**xample toolkit.

WHAT THIS PACKAGE IS FOR
------------------------
A compiler optimization is supposed to make code faster without changing what
it does.  Sometimes LLVM gets this wrong and the "optimized" code behaves
differently -- that is a *miscompilation*.

A tool called **Alive2** can automatically prove that a specific optimization
is wrong, and when it does, it prints a **counterexample**: a concrete input
where the original code and the optimized code disagree.

That counterexample is the most useful thing you can hand an AI model that is
trying to fix the bug.  The problem is that Alive2's counterexample is
cluttered -- it shows every instruction and every intermediate value, most of
which have nothing to do with the actual bug.

This package sits in the middle:

    Alive2  -->  raw counterexample  -->  [ ce ]  -->  a cleaner message  -->  LLM

and does two separate things to it:

1. **Shrink it** -- delete everything that is not part of the bug, while
   *proving* (by re-running Alive2) that the bug is still there.
2. **Organise it** -- relabel it into clear sections instead of a wall of text.

The research question is whether either of those actually helps the AI fix
more bugs.  So the package also lets you turn each one on and off
independently, which is what makes this an experiment rather than just a tool.

HOW THE FILES FIT TOGETHER
--------------------------
Read them in this order; each one builds on the ones above it.

    alive.py       Run Alive2 and turn its text output into Python objects.
    irmodel.py     Read LLVM IR code into Python objects we can edit.
    oracle.py      The referee: "after that edit, is it still the same bug?"
    reduction.py   Shared helpers for both shrinkers.
    reduce_generic.py   Shrinker #1: dumb, text-only. The control group.
    reduce_iraware.py   Shrinker #2: understands LLVM. The actual contribution.
    structured.py  Turn a counterexample into labelled sections.
    feedback.py    The experiment: pick a shrinker + a format, get a message.
    benchmark.py   Glue into the existing llvm-apr-benchmark repair loop.
    cli.py         Command-line access to all of the above.

WHERE IT PLUGS IN
-----------------
The benchmark repo (``llvm-apr-benchmark``) already runs Alive2 for us, in a
function called ``llvm_helper.alive2_check``.  It returns a dictionary::

    {"src": <original IR>, "tgt": <optimized IR>, "log": <Alive2's output>}

Everything in this package consumes exactly that dictionary.  We do not
modify the benchmark; we only replace the step where its output gets pasted
into the prompt.
"""

__version__ = "0.1.0"
