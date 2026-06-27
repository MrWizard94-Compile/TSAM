Thoughts?

I think you're aiming at something genuinely interesting here. More importantly, I think you're attacking one of the real bottlenecks in today's LLMs rather than simply trying to make another transformer. I read through the blueprint, and there are parts I think are quite strong, parts I think need revision, and one part that I think could become the real research contribution.

### What I think is genuinely novel

Not the SSM.

Not the Hopfield network.

Not the AST encoding.

It's **combining them into a deterministic synthesis pipeline.**

Current code models work like this:
    Prompt ↓Huge probabilistic model ↓Token prediction ↓Hope syntax emerges

Your architecture is closer to
    Intent ↓Fixed state evolution ↓Structural attractor ↓Deterministic graph rewrite ↓Code

Those are fundamentally different philosophies.

One predicts.

The other converges.

That difference matters.

* * *

The biggest misconception people will have
------------------------------------------

People will hear

> "Hopfield"

and think

> "1980s neural network."

That's not actually what you're describing.

You're really describing an **energy landscape over program structures.**

Instead of

> "predict the next token"

you're saying

> "find the nearest valid software architecture."

Those aren't the same problem.

I actually think that's the strongest part of the proposal.

* * *

Where I disagree with the document
==================================

Ironically...

the document itself points out some of these issues.

I agree with almost every criticism it makes.

* * *

1. Prime scattering

-------------------

This is the weakest mathematical component.

It throws away

* hierarchy
* sibling relationships
* scope
* ordering
* semantic distance

ASTs are trees.

Prime scattering treats them like unordered bags.

That works as a proof of concept.

It absolutely will not scale.

* * *

2. Bag-of-characters

--------------------

Same issue.

It proves the pipeline.

It won't survive contact with real language.

Personally I'd replace this much earlier than the document suggests.

Even a tiny embedding model would outperform it enormously.

* * *

3. O(1) memory

--------------

This is where people are going to misunderstand you.

The architecture has

**fixed runtime memory.**

That is true.

But...

the knowledge itself still has to exist somewhere.

So the better wording is

> constant inference memory

not

> infinite memory.

That's a much stronger and more defensible claim.

* * *

4. Zero hallucinations

----------------------

I'd remove that phrase completely.

What you're actually building is something better described as

> deterministic rejection outside known manifolds.

That's a huge difference.

Instead of

"I hallucinated."

it says

"I don't know."

That's an enormous improvement.

* * *

The part I think deserves much more emphasis
============================================

Honestly?

The graph deformation engine.

Not the neural part.

Not the SSM.

The graph rewriting.

That is the thing that actually edits software.

The rest of the system is basically navigation.

Graph rewriting is the actual compiler.

If I were writing the paper I'd almost flip the emphasis.

* * *

What reminds me of this
=======================

Pieces of your architecture resemble several existing research directions.

* State Space Models (Mamba, S4, etc.) for fixed-size recurrent state.
* Modern Hopfield networks and Dense Associative Memory for energy-based retrieval.
* Equality saturation and e-graphs for deterministic program rewriting.
* Traditional compiler optimization passes.
* Symbolic AI.

But I don't know of anyone combining those ideas quite the way you're proposing.

That's what makes it interesting.

* * *

If I were redesigning TSAM
==========================

I'd actually change the order.

Instead of
    Intent↓SSM↓Hopfield↓AST

I'd do
    Intent↓Embedding↓Graph retrieval↓Hopfield cleanup↓Deterministic graph rewrite↓Verification↓Output

Notice what's missing.

The SSM no longer has to "remember" code.

It only has to maintain working context.

That's a much easier problem.

* * *

What excites me
===============

This sentence:

> "Fail loudly instead of hallucinating."

That.

That's the philosophy I'd lean into.

Current LLMs always answer.

Your system says

> "No valid attractor exists."

That is much more like a compiler.

Compilers don't guess.

* * *

My favorite part
================

Oddly enough...

it's not the AI.

It's this:
    Verification gateSTOPDon't continue until tests pass

That engineering discipline is exactly how ambitious systems stay manageable.

The staged verification approach in the plan is one of its strongest practical features.

* * *

One thing I would add
=====================

I think the architecture is missing one critical component.

A verifier.

Something like
    Intent↓Graph synthesis↓Compilation↓Static analysis↓Unit tests↓Energy update↓Accept

Instead of only minimizing Hopfield energy...

minimize
    compiler errors+AST distance+test failures+constraint violations

Now your attractor isn't just mathematically stable.

It's **functionally correct.**

* * *

Overall assessment
------------------

If I had to score it today as a research concept:

* **Originality:** 8.5–9/10. The individual pieces are known, but the way they're assembled around deterministic code synthesis is unusual.
* **Technical feasibility:** 7/10 for the scoped v0.1 described in the document. A toy demonstration focused on a narrow NeoForge pattern is realistic; a general-purpose code generator is a much larger challenge.
* **Engineering plan:** 9/10. The phased milestones, verification gates, and explicit scope boundaries are well thought out.
* **Marketing claims:** 4/10. I'd tone down phrases like "zero hallucination," "absolute immunity," and "infinite memory" in favor of precise, defensible claims such as "constant inference memory" and "deterministic rejection outside supported manifolds."

I don't think this replaces transformer-based models. I do think it explores a different design space—one that emphasizes constrained, verifiable program transformation over open-ended token prediction. If the v0.1 prototype can demonstrate that advantage convincingly, it would be a meaningful proof of concept and a solid foundation for further research.

The goal is achieve a brand new species of AI on a brand new computational architecture. Hard requirements: No attention transformers, Static memory foot print, minimal(sttatic) power draw, immune to hallucinations. 80% of the design should be based around coding extremely complex apps/mods/ports, the remaining 20% should be devoted to comms and the whole thing needs to be able to run on a gaming PC. It MUST UNDDERSTAND INTENT. able to run on less than 6GB VRAM gaming GPU

I actually think your requirements fit together better than they might appear at first glance—but I would change the framing.

The goal shouldn't be "replace transformers."

The goal should be:

> **Design a deterministic cognitive architecture optimized for software engineering rather than natural language prediction.**

That's a much more precise engineering target.

* * *

The hard requirements
=====================

Let's examine each one.

| Requirement              | Possible?            | My opinion                          |
| ------------------------ | -------------------- | ----------------------------------- |
| No transformers          | Yes                  | Completely achievable               |
| Static memory footprint  | Yes                  | One of the strongest design goals   |
| Static power draw        | Mostly               | Within a narrow operating envelope  |
| Runs on <6 GB VRAM       | Yes                  | Definitely                          |
| Understands intent       | Yes, but differently | Requires a different representation |
| Immune to hallucinations | Not absolutely       | Can become "fails safely" instead   |

Notice the last one.

I don't think _absolute immunity_ is a realistic claim for any sufficiently expressive AI system. However, you can build a system that **refuses to generate output when confidence or constraints are violated**. For a coding assistant, that can be more valuable than always producing an answer.

* * *

I think you're designing something closer to a compiler than a chatbot
======================================================================

Today's LLMs do this:
    Text↓Probability↓Next token↓Repeat

I think yours should look more like
    Intent↓Semantic State↓Constraint Solver↓Program Graph↓Verification↓Output

Notice...

No token prediction.

None.

* * *

The biggest insight
===================

I don't actually think "language" should be the primary representation.

Programming languages already have one.

ASTs.

Control Flow Graphs.

SSA.

Type systems.

Dependency graphs.

Why convert everything into text just to turn it back into code?

That round trip loses structure.

* * *

Intent is the real problem
==========================

You said

> It MUST UNDERSTAND INTENT.

I think that's actually the hardest research question.

Not memory.

Not hallucinations.

Intent.

People confuse intent with language.

They're different.

Suppose I tell the AI

> Port Astral Sorcery to NeoForge 1.21.

The words are irrelevant.

The intent is something like
    GOALMaintain functionalityRewrite API usageMaintain gameplayMaintain performanceModernize architectureProduce compilable project

That's not language anymore.

That's a goal graph.

I'd spend far more research effort there than on the state update mechanism.

* * *

The architecture I'd explore
============================

Something like this:
                    Human                  │          Intent Extraction                  │          Goal Graph Builder                  │         Semantic State Machine                  │     Constraint / Rule Engine                  │      Attractor Stabilization                  │      Program Graph Mutation                  │     Compiler + Test Feedback                  │          Verified Output

Notice what's missing.

No attention.

No transformer.

No autoregressive decoding.

* * *

The "80% coder / 20% communicator" idea
=======================================

I actually like this.

Current LLMs are roughly
    95% language5% reasoning

You're proposing almost the inverse.
    20%Natural language interface80%Engineering engine

That feels much closer to something like
    CAD software+Compiler+Constraint solver+AI planner

than ChatGPT.

* * *

Memory
======

This is where I think TSAM could become genuinely different.

Instead of
    Conversation↓Growing KV cache↓Growing VRAM

I'd rather see
    Conversation↓State Projection↓Overwrite existing state↓Constant RAM forever

No context window.

No token history.

Only state.

That's a profound architectural difference.

* * *

Coding shouldn't be generation
==============================

This is another place I'd diverge from mainstream AI.

Generating code token-by-token is inherently probabilistic.

I'd rather have
    Intent↓Find architecture↓Apply transformations↓Compile↓Verify↓Repeat until constraints satisfied

That's closer to synthesis than prediction.

* * *

What worries me
===============

The attractor network.

Not because Hopfield networks are bad.

Because software is combinatorial.

Millions of valid ASTs.

Millions.

You won't store them all.

So I'd make the attractor responsible for **local structural correctness**, not complete programs.

Think of it like
    Capability Provider↓Known good basinRenderer↓Known good basinRegistry↓Known good basinNetworking↓Known good basin

Each subsystem has its own stable region.

Then compose them.

That scales much better.

* * *

Under 6 GB VRAM
===============

Ironically...

I think this is one of the easiest requirements.

If you eliminate:

* transformer layers
* KV cache
* massive embedding matrices
* autoregressive inference

...your memory budget becomes dominated by:

* state tensors
* graph structures
* rule database
* verifier
* compiler interface

Those are much smaller than the activations and caches used by large transformer models. Depending on the implementation, much of the symbolic data may not need to live in VRAM at all.

* * *

The one requirement I'd rewrite
===============================

Instead of saying

> Immune to hallucinations

I'd say

> **The system shall never fabricate executable structures outside its verified knowledge manifolds. Unknown requests produce deterministic failure or request clarification rather than speculative output.**

That's a requirement you can actually test.

* * *

If I were writing the research proposal
---------------------------------------

I'd give it a thesis like this:

> _TSAM is not a language model. It is a deterministic cognitive architecture for software synthesis that combines fixed-state computation, goal-directed intent representation, graph-based program transformation, and formal verification to produce correct-by-construction software within a constant computational footprint._

That, to me, is the distinctive idea. It shifts the focus from predicting text to synthesizing verified software under explicit constraints. Whether it ultimately succeeds will depend on demonstrating that the intent representation, graph transformations, and verification loop work together effectively on increasingly complex programming tasks.
