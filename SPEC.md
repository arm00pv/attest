# Provenance-Preserving Agent Memory

**A specification for memory that can tell you what it checked.**

Version 0.1 · Status: draft · Reference implementation: `attest`

---

## 1. The problem this specifies a solution to

An AI system stores something it was told, and later reads it back. At that
moment the stored item and the original claim look identical, so the reader has
no way to know whether anything ever checked it.

Two failures follow, and both are silent:

1. **Assertion launders into fact.** A model's guess is written to memory. A
   later read returns it with the same apparent authority as a verified result.
2. **Absence of a verdict launders into a negative verdict.** A checker that
   could not run — not installed, timed out, crashed — is recorded as `false`, which
   is indistinguishable from "checked, and it failed".

This specification exists to make both unrepresentable.

---

## 2. Normative language

MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

---

## 3. The verdict

A verdict is the result of putting one claim against one checker.

### 3.1 Three states

A verdict MUST carry:

- `verified` — boolean, REQUIRED.
- `checker_error` — boolean, MUST be present and true when, and only when,
  no verdict was reached.

Implementations MUST produce exactly one of:

| state | `verified` | `checker_error` | meaning |
|---|---|---|---|
| VERIFIED | `true` | absent or `false` | a checker ran and passed the claim |
| DISPUTED | `false` | absent or `false` | a checker ran and rejected the claim |
| UNKNOWN | `false` | `true` | no verdict was reached |

### 3.2 Rules

- **R1.** `verified` MUST NOT be defaulted to true. A claim that was never
  checked is not verified.
- **R2.** `verified: true` MUST imply that a checker ran. The verdict SHOULD
  name it.
- **R3.** A checker that did not reach a verdict MUST report UNKNOWN. It MUST NOT
  report DISPUTED.
- **R4.** A timeout MUST be UNKNOWN.
- **R5.** A checker terminated by a signal MUST be UNKNOWN, and SHOULD report the
  signal number as a structured field.
- **R6.** A checker that is absent, uninstallable, or misconfigured MUST be
  UNKNOWN, and the reason SHOULD name a remedy.
- **R7.** UNKNOWN MUST NOT be rendered to a human or a caller in any way that is
  indistinguishable from DISPUTED. Where only `verified` is presented, the
  presentation is non-conformant.
- **R8.** An implementation MUST NOT infer a verdict from the mere presence of a
  file, directory, or executable. Capabilities are measured by running something.

### 3.3 Rationale (informative)

R5 is not hypothetical. In the system this was extracted from, defective RAM made
the Lean proof checker die with `SIGSEGV` and emit no output. The surrounding code
then computed `verified: false` with no error lines — bit-for-bit identical to the
output for a genuinely false claim. Every crashed check was recorded as a
disproof until `checker_error` was introduced.

---

## 4. Stored facts

### 4.1 Tier

Every stored fact MUST carry a tier, and the tier MUST be one of:

- `verified` — a checker passed it.
- `asserted` — an agent or a human said it and nothing checked it.

**A fact with no tier MUST be unrepresentable.** Conforming implementations
SHOULD enforce this in the storage schema — a `CHECK` constraint, a column
type, a required field — rather than in application code, so that no future code
path can write an untiered row.

### 4.2 Provenance

A stored fact MUST record:

- `who` — the identifier of the source.
- `created_at` — when it was stored.
- `tier`.
- `checker` — the checking mechanism, when the tier is `verified`.
- `why` — the evidence, MAY be empty.

### 4.3 Retrieval

- **R9.** A read MUST return the tier with every result. A result without its tier
  is non-conformant.
- **R10.** An implementation that offers a "verified only" filter MUST NOT return
  `asserted` facts under it, whatever the ranking.
- **R11.** Where `verified` is derived from the tier, it MUST be `true` only for
  the `verified` tier. An `asserted` fact MUST report `verified: false`.
- **R12.** Ranking SHOULD prefer a verified fact over a better-matching asserted
  one. Retrieval quality and provenance are different axes; do not let the first
  silently override the second.

### 4.4 Writing memory from a verification

When a verification result is itself stored, the tier MUST be derived from the
verdict, and the three states MUST remain distinguishable in the stored text or
its metadata. An UNKNOWN result MUST NOT be stored as a dispute.

---

## 5. Capabilities

### 5.1

An implementation MUST be able to report what it can and cannot check.

### 5.2

- **R13.** Capabilities MUST be measured by executing a trivial probe against each
  checker, not inferred from configuration or from a filesystem check.
- **R14.** Each checker MUST report whether its probe passed, how long it took, and
  a human-readable verdict.
- **R15.** The aggregate verdict MUST name the checkers that cannot run. "All
  good" with no mention of what is missing is non-conformant.
- **R16.** If the measurement is cached, the cache age and a staleness flag MUST be
  reported. Staleness MUST NOT be hidden.
- **R17.** A state of "not yet measured" MUST NOT be presented in a way that is
  indistinguishable from "nothing works".

### 5.3 Rationale (informative)

R13 exists because the originating system once reported `GROUND TRUTH AVAILABLE`
on the strength of `os.path.isdir(MATHLIB)`. The directory did exist. The prover
could not check a single proof.

---

## 6. Silence

- **R18.** An implementation MUST NOT report its own inability to measure as a
  clean result.
- **R19.** Where an implementation cannot measure, it MUST say so explicitly and
  MUST use a distinct status from both success and failure.

---

## 7. Interfaces

### 7.1

An implementation MUST expose the operations over at least one machine-readable
interface. A plain JSON API is sufficient.

### 7.2

Where MCP is offered:

- **R20.** Each operation MUST be exposed as a named tool with a real, typed
  parameter schema derived from a concrete signature.
- **R21.** Tool descriptions MUST state the three-valued rule and MUST instruct the
  caller to set `verified: true` only when a checker passed the claim.

### 7.3

- **R22.** If the implementation can execute caller-supplied code, it MUST require
  authentication and MUST refuse to start without it.
- **R23.** It MUST document, in its own interface, that execution is not a security
  boundary.

---

## 8. Conformance

A conforming implementation provides a test suite in which:

- **R24.** Every rule above that can be tested by an automated control has one.
- **R25.** Each control is accompanied by a negative control demonstrating that the
  property can fail — a suite that cannot fail is not evidence.
- **R26.** At least one control asserts that the checker distinguishes UNKNOWN from
  DISPUTED. This is the load-bearing rule; without it the rest is decoration.
- **R27.** A control that cannot run reports a distinct SKIPPED status. SKIPPED
  MUST NOT be counted or presented as a pass.

---

## Appendix A — Non-conformance examples (informative)

| pattern | rule broken |
|---|---|
| `return {"verified": ok}` where `ok` is a subprocess return code | R3, R5 |
| `except TimeoutExpired: return {"verified": False}` | R4 |
| `if os.path.isdir(mathlib): caps["math"] = "available"` | R8 |
| storing a model's summary with no tier field | §4.1 |
| `recall(...)` returning `{fact, score}` with no tier | R9 |
| a UI showing a red cross for both DISPUTED and UNKNOWN | R7 |

## Appendix B — Reference implementation

`attest` v0.1.0 implements this specification. 38 automated controls; see
`tests/test_conformance.py`.
