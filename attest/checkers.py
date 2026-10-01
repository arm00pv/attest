"""The checkers. None of them is a language model.

A language model may PROPOSE a claim. Only a checker may pass one. Each checker
here is real - a subprocess that runs, a database that answers, a proof
assistant that compiles - and each reports honestly when it cannot run at all.

Every checker returns a Verdict, and the only way any of them may report
verified=True is through Verdict.passed().
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

from .verdict import Verdict

HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------- aliases
# Accept the words people actually type. Measured on the machine this came from:
# asking for kind='lean' returned "unknown kind: lean" - a correct refusal, and a
# useless answer for someone who reasonably asked for Lean.
KIND_ALIASES = {
    "math": "math", "lean": "math", "lean4": "math", "mathlib": "math",
    "theorem": "math", "proof": "math", "prove": "math",
    "code": "code", "python": "code", "py": "code", "exec": "code", "run": "code",
    "graph": "graph", "aleph": "graph", "fact": "graph", "lookup": "graph",
    "triple": "graph",
}
KINDS = ("math", "code", "graph")


def resolve_kind(kind: Optional[str], claim: str) -> Optional[str]:
    """Map a caller's word onto a real kind, or infer one from the claim.

    Returns None if the caller named something that does not exist - the caller
    must be told, not guessed at.
    """
    if kind:
        k = KIND_ALIASES.get(str(kind).strip().lower())
        return k  # None means unknown, and the service reports that
    c = claim.strip()
    for prefix, target in (("math:", "math"), ("lean:", "math"),
                           ("py:", "code"), ("code:", "code"),
                           ("graph:", "graph"), ("aleph:", "graph")):
        if c.lower().startswith(prefix):
            return target
    if any(w in c for w in ("theorem", "lemma", ":=", " by ", "by\n")):
        return "math"
    if "|" in c and c.count("|") == 2:
        return "graph"
    return "code"


def strip_prefix(claim: str) -> str:
    for prefix in ("math:", "lean:", "py:", "code:", "graph:", "aleph:"):
        if claim.strip().lower().startswith(prefix):
            return claim.strip()[len(prefix):].strip()
    return claim


# ---------------------------------------------------------------------- code
CODE_SANDBOX_NOTE = (
    "This is a SUBPROCESS, NOT A JAIL. Code runs as the same user that runs this "
    "service, with a wall-clock timeout and, where the platform allows, CPU and "
    "address-space limits. It is not a security boundary and must not be treated "
    "as one. Do not expose this service to anyone you would not give a shell."
)


def check_code(source: str, timeout: int = 20) -> Verdict:
    """Run Python in a subprocess. Exit status 0 is a pass; anything else is a
    dispute. A timeout is UNKNOWN - the program did not answer, which is not the
    same as the program answering 'no'."""
    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="attest_exec_")
    path = os.path.join(tmpdir, "claim.py")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(source)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": tmpdir,
               "PYTHONDONTWRITEBYTECODE": "1", "ATTEST_SANDBOX": "1"}
        kwargs: Dict[str, Any] = {}
        if os.name == "posix":
            def _limit():
                try:
                    import resource
                    resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))
                    resource.setrlimit(resource.RLIMIT_AS,
                                       (2 * 1024 ** 3, 2 * 1024 ** 3))
                except Exception:
                    pass
            kwargs["preexec_fn"] = _limit
        r = subprocess.run([sys.executable, path], capture_output=True, text=True,
                           timeout=timeout, cwd=tmpdir, env=env, **kwargs)
        dt = time.time() - t0
        out = (r.stdout or "")[-600:]
        err = (r.stderr or "")[-600:]
        if r.returncode < 0 or r.returncode >= 128:
            # Reported as a STRUCTURED field, not only in prose. check_math did
            # this from the start and check_code did not, so a caller could not
            # tell "killed" from "timed out" without parsing an English
            # sentence. The control caught the inconsistency.
            sig = -r.returncode if r.returncode < 0 else r.returncode - 128
            return Verdict.unknown(
                "code", "the program was killed by signal %d - UNKNOWN, not a "
                        "disproof" % sig,
                seconds=dt, signal=sig, exit=r.returncode, stdout=out, stderr=err)
        if r.returncode == 0:
            return Verdict.passed("code", seconds=dt, exit=0, stdout=out)
        return Verdict.disputed("code", "exit status %d" % r.returncode,
                                seconds=dt, exit=r.returncode, stdout=out,
                                stderr=err)
    except subprocess.TimeoutExpired:
        return Verdict.unknown("code", "timeout after %ss - UNKNOWN, not a "
                                       "disproof" % timeout,
                               seconds=time.time() - t0, timeout=timeout)
    except Exception as exc:  # the checker itself failed -> unknown, never False
        return Verdict.unknown("code", "%s: %s" % (type(exc).__name__, exc),
                               seconds=time.time() - t0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------- graph
class GraphChecker:
    """Triples, stored with the same tier discipline as memory.

    Named after Aleph, which is what the system this came from called its
    knowledge graph. Kept, because renaming working vocabulary is churn.
    """

    def __init__(self, db):
        self.db = db

    def add(self, source: str, relation: str, target: str, tier: str = "asserted",
            who: str = "", why: str = "") -> Dict[str, Any]:
        self.db.db.execute(
            "INSERT OR IGNORE INTO triples (source, relation, target, tier, who, why, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (source[:300], relation[:120], target[:300], tier, who[:120],
             why[:400], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        self.db.db.commit()
        return {"ok": True, "source": source, "relation": relation,
                "target": target, "tier": tier}

    def check(self, source: str, relation: str, target: str) -> Verdict:
        """Is this triple present, contradicted, or not known?

        'Not known' is the honest and common answer, and it is reported as
        UNKNOWN - the graph not containing a fact is not evidence against it.
        """
        t0 = time.time()
        try:
            exact = self.db.db.execute(
                "SELECT tier FROM triples WHERE source=? AND relation=? AND target=?",
                (source, relation, target)).fetchone()
            if exact:
                return Verdict.passed("graph", seconds=time.time() - t0,
                                      triple=[source, relation, target],
                                      tier=exact["tier"])
            # A contradiction is the same source and relation pointing at a
            # DIFFERENT target. Absence is not a contradiction, and the first
            # version of this confused the two.
            other = self.db.db.execute(
                "SELECT target FROM triples WHERE source=? AND relation=? AND target<>?",
                (source, relation, target)).fetchall()
            if other:
                return Verdict.disputed(
                    "graph", "the graph holds a different target for this source "
                             "and relation: %s"
                             % ", ".join(r["target"] for r in other[:3]),
                    seconds=time.time() - t0, triple=[source, relation, target],
                    contradicts=[r["target"] for r in other[:3]])
            return Verdict.unknown(
                "graph", "the graph has nothing to say about this triple - that "
                         "is not evidence against it",
                seconds=time.time() - t0, triple=[source, relation, target])
        except Exception as exc:
            return Verdict.unknown("graph", "%s: %s" % (type(exc).__name__, exc),
                                   seconds=time.time() - t0)


# ---------------------------------------------------------------------- math
MATHLIB = os.environ.get("ATTEST_MATHLIB", "")

# Lean's way of saying "I do not have that". None of these means the claim is
# false; they mean the environment cannot evaluate it as written.
MISSING_DEPENDENCY_SIGNALS = (
    "unknown tactic", "unknown identifier", "unknown constant",
    "unknown namespace", "unknown module", "unknown declaration",
    "unknown package", "failed to resolve", "invalid import",
    "unknown option", "unknown attribute",
)


def mathlib_dir() -> Optional[str]:
    """Where Mathlib lives, if the user has one.

    Not assumed. 'lake env lean' needs a Lean project to be run from, and a
    bare 'lean' on PATH has no Mathlib - so a claim that imports Mathlib will
    fail there for a reason that has nothing to do with the claim. Which of the
    two is in play is reported in capabilities() rather than left to guesswork.
    """
    if MATHLIB and os.path.isdir(MATHLIB):
        return MATHLIB
    return None


def _lean_binary() -> Optional[str]:
    return shutil.which("lean")


def _lake_binary() -> Optional[str]:
    return shutil.which("lake")


def check_math(source: str, timeout: int = 600) -> Verdict:
    """Compile Lean against Mathlib. GROUND TRUTH, not an opinion.

    Returns UNKNOWN - not a disproof - when Lean is absent, when it times out,
    and when it is killed by a signal. That last case is not hypothetical: on the
    machine this came from, failing RAM made Lean segfault inside
    libleanshared.so and print nothing, which is byte-for-byte identical to what
    a genuinely false claim produces.
    """
    t0 = time.time()
    lean = _lean_binary()
    lake = _lake_binary()
    if not lean and not lake:
        return Verdict.unknown(
            "math", "no Lean toolchain on this machine - math claims CANNOT be "
                    "checked here. This is not a failed proof.",
            seconds=time.time() - t0, remedy="install Lean 4, or use kind='code'")

    lib = mathlib_dir()
    workdir = lib or tempfile.mkdtemp(prefix="attest_lean_")
    path = os.path.join(workdir, "Claim_%d.lean" % int(time.time()))
    name = "attest_claim_%d" % int(time.time())
    body = source
    if not body.lstrip().startswith("import"):
        # Only prepend the Mathlib import when there IS a Mathlib to import.
        body = ("import Mathlib\n\n" + body) if lib else body
    # Rename the theorem. Measured in the system this came from: a Mathlib name
    # collision was scored as a WRONG PROOF rather than as a naming problem.
    import re
    body = re.sub(r"^(\s*)(theorem|lemma)\s+[A-Za-z_][A-Za-z0-9_.]*",
                  lambda m: m.group(1) + m.group(2) + " " + name,
                  body, count=1, flags=re.M)
    # Lean accepts 'sorry' as a complete proof. It is not one.
    bypass = [tok for tok in ("sorry", "admit") if re.search(r"\b" + tok + r"\b", body)]
    if bypass:
        return Verdict.disputed(
            "math", "uses %s - Lean accepts it, this service does not"
                    % ", ".join(bypass), seconds=time.time() - t0)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        if lib and lake:
            cmd, cwd = [lake, "env", "lean", path], lib
        else:
            cmd, cwd = [lean or lake, path], workdir
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=cwd)
        dt = time.time() - t0
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if r.returncode < 0 or r.returncode >= 128:
            sig = -r.returncode if r.returncode < 0 else r.returncode - 128
            return Verdict.unknown(
                "math", "the Lean checker was killed by signal %d - UNKNOWN, not "
                        "a failed proof" % sig,
                seconds=dt, output=out[:400], signal=sig)
        lines = out.split("\n")
        # A MISSING DEPENDENCY IS NOT A DISPROOF.
        #
        # Measured on throne, 2026-10-01: 'theorem t : (1:Nat) + 1 = 2 := by
        # norm_num' came back DISPUTED, because Mathlib was not configured and
        # 'norm_num' therefore does not exist in bare Lean. The theorem is TRUE.
        # The checker ran, rejected it, and the rejection had nothing to do with
        # the claim.
        #
        # This is the exact failure this whole service exists to prevent, found
        # in this service by running it rather than by reading it. When no
        # Mathlib is configured, an "unknown tactic/identifier" is an
        # environment limitation, and the honest verdict is UNKNOWN with a
        # remedy. When Mathlib IS present, the same error means the proof is
        # genuinely broken and DISPUTED is correct.
        if lib is None:
            dep = [l for l in lines
                   if any(sig in l.lower() for sig in MISSING_DEPENDENCY_SIGNALS)]
            if dep:
                return Verdict.unknown(
                    "math",
                    "Lean could not resolve something this claim needs (%s). With "
                    "no Mathlib configured this is a MISSING DEPENDENCY, not a "
                    "disproof. Install Mathlib and set ATTEST_MATHLIB to get a "
                    "real verdict." % dep[0].strip()[:90],
                    seconds=dt, output="\n".join(dep)[:400],
                    needs="Mathlib", remedy="set ATTEST_MATHLIB to a Lean project "
                                            "containing Mathlib")
        lines_after = lines
        # Lean's own contract: an error fails, a warning does not. Measured in the
        # system this came from: four of five "failures" in a benchmark carried
        # warnings only, and correct proofs were being thrown away.
        err_lines = [l for l in lines_after
                     if re.search(r"\berror\b", l) and "warning" not in l.lower()]
        warn_lines = [l for l in lines_after if "warning" in l.lower()]
        if r.returncode == 0 and not err_lines:
            return Verdict.passed("math", seconds=dt, warnings=len(warn_lines),
                                  output=out[:200])
        return Verdict.disputed("math", "Lean rejected it", seconds=dt,
                                warnings=len(warn_lines),
                                output="\n".join(err_lines)[:600] or out[:600])
    except subprocess.TimeoutExpired:
        return Verdict.unknown(
            "math", "timeout after %ss - UNKNOWN, not a failed proof. A cold "
                    "Mathlib import alone can take minutes." % timeout,
            seconds=time.time() - t0)
    except Exception as exc:
        return Verdict.unknown("math", "%s: %s" % (type(exc).__name__, exc),
                               seconds=time.time() - t0)
    finally:
        # Only ever remove a directory this call created. Deleting a Mathlib
        # checkout because it happened to be our cwd would be a catastrophe
        # dressed as tidiness.
        if not lib:
            shutil.rmtree(workdir, ignore_errors=True)
