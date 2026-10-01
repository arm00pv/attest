# attest

**Your agent's memory cannot tell you what it checked from what it made up. Attest can.**

Ask an AI assistant something, and it answers. Nothing in that answer says whether
anyone checked it. Ask a second assistant, and it starts from nothing. Every
assistant is an island with no ground truth and no memory it can trust.

`attest` is one small service that gives an agent two things it does not
have: **memory that carries its own provenance**, and **claims checked against
something that is not a language model**.

A language model may *propose* a claim. Only a checker may *pass* one.

---

## The one rule

> **A verified claim and an unverified one must never look alike.**

Most systems that "check" something return a boolean, and a boolean has no room
for the answer that matters most: *I could not tell*. Collapsing that into `false`
is how a checker that never ran becomes a disproof, and it is the most common
way software misleads without ever saying anything false.

So a verdict here has **three** states, not two:

| `verified` | `checker_error` | meaning |
|---|---|---|
| `true` | absent | a real checker ran, and passed it |
| `false` | absent | a real checker ran, and **disputed** it |
| `false` | `true` | **UNKNOWN** — no verdict was reached |

The third row is not a failure of the claim. It means the checker was not
installed, timed out, or crashed — and a caller that reads only `verified`, which
is most callers, can still tell a disproof from the absence of one.

This is not a style preference. It is measured. On the machine this was
extracted from, failing RAM made Lean die with `SIGSEGV` inside
`libleanshared.so` and print nothing. The next line of code computed `False` with an
empty error list — which is **byte for byte identical** to what a genuinely false
claim produces. Every crashed check was being recorded as a disproof.

---

## What it is

Five operations. That is the whole surface.

| operation | what it does |
|---|---|
| `capabilities` | what this machine can and cannot **actually** check — measured, not asserted |
| `verify` | put a claim against a real checker and get a verdict |
| `remember` | leave a finding, with its epistemic tier attached |
| `recall` | read findings back, still carrying that tier |
| `memcheck` | test this machine's RAM, because the checker runs on it |

**Three real checkers, none of them a language model:**

- **`code`** — runs the program in a subprocess. Exit status decides.
- **`graph`** — a triple store that answers, and distinguishes *contradicted* from
  *never heard of it*.
- **`math`** — compiles Lean 4 against Mathlib. Ground truth, when you have it.

**Two interfaces:** a plain JSON API over the standard library, and MCP, so any
MCP host gets all five as native tools.

---

## Quick start

```bash
pip install attest-mcp            # core, no MCP SDK
pip install "attest-mcp[mcp]"     # with the MCP server
```

```bash
# what can this machine actually check?
attest capabilities

# check a claim
attest verify "print(sum(range(10)) == 45)" --kind code

# leave a finding that nothing checked
attest remember "the pump is warm" --why "the operator said so"

# read findings back, only the checked ones
attest recall "pump" --only-verified

# run it for an agent
attest serve --port 8250          # HTTP JSON
attest mcp --transport stdio      # MCP, for a desktop host
```

Claude Desktop sees it as:

```json
{ "mcpServers": { "attest": { "command": "attest", "args": ["mcp"] } } }
```

---

## It runs locally, and that is deliberate

The checkers need a real toolchain: a Lean installation, a Python interpreter, a
database. That means `attest` runs **on your machine**, not on someone's cloud.
Nothing leaves the box, there is no account, and there is no per-call cost.

It also means the free tier can be genuinely free, which is not true of most
things in this space.

---

## What it does **not** do

Stated plainly, because a tool that oversells itself is exactly what this project
exists to argue against.

- **It is not a vector store.** `recall` is a token match over the text,
  deliberately. [Mem0](https://github.com/mem0ai/mem0), [Zep](https://github.com/getzep/zep)
  and [Letta](https://github.com/letta-ai/letta) do similarity search far better
  than this and are funded to keep doing it. Bring your own embeddings. What
  `attest` adds is the **tier** — theirs are all equally confident.
- **`code` is a subprocess, not a jail.** It runs as the same user, with a
  timeout and — on POSIX — CPU and address-space limits. It is **not a security
  boundary**. Do not expose this service to anyone you would not give a shell.
- **`math` is slow and often unavailable.** A cold Mathlib import can take
  minutes, and if you do not have Lean, math claims return **UNKNOWN** — never a
  false "no".
- **It does not decide truth.** It reports what a checker did. If no checker can
  speak to a claim, the honest answer is UNKNOWN, and that is what you get.

---

## The conformance suite

```bash
python tests/test_conformance.py   # 38 controls
python tests/smoke_http.py         # end-to-end over real HTTP
```

The controls are the argument. A few of them:

- **C002 (anti-blindness)** — if a checker times out, the verdict must be
  UNKNOWN. *If this control cannot fire, nothing else here means anything.*
- **C003** — the negative control for C002: a claim that genuinely fails must
  come back **DISPUTED**. Without it, a suite that returned UNKNOWN for
  everything would pass C002 and be useless.
- **C004** — a checker killed by a signal is UNKNOWN, and the signal is a
  structured field, not a sentence in a log.
- **C005** — a structural control over the **AST**: outside `verdict.py`, no module
  may construct `verified=True`. It also proves it can detect a planted
  violation, because a control that cannot fail is worse than no control.
- **C009** — a fact with no tier is *unrepresentable*. The SQLite `CHECK`
  constraint, not the application code, is what makes it impossible.

**A skipped control is never reported as a pass.** On Windows, C004 skips (no
POSIX signals); on Linux with Lean installed, C013 skips (the absence cannot be
tested). The two platforms skip different controls and together cover all of them.

---

## Where this came from

Extracted from a personal system that spent **three days** reporting a host-side
USB fault as three broken boards, because nothing in it could tell *"it's broken"*
from *"I can't see it."* The instrument that fixed that — and the discipline of
negative controls, three-valued verdicts and never letting silence mean health —
is the same discipline in this repository.

The full normative version of the rule is in [SPEC.md](SPEC.md).

## Licence

MIT.
