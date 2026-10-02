#!/usr/bin/env python3
"""fleet_trust.py - is this host still going to be this host when the work runs?

WHAT THIS ADDS TO THE EXISTING SELF-MODEL
-----------------------------------------
The estate already holds 98 assertions about three hosts and re-verifies every one of
them four times a day. That layer is good: it measures, it keeps the evidence, it
hashes it so it cannot be quietly edited. What it answers is "is this true NOW?".

Routing needs the other question, and nothing was asking it: will this still be true
in six hours, when the job is actually running? So each assertion becomes a forecast
with a due time at the next check, and the answer arrives by itself when the
self-model re-runs.

And the ROUTING CHOICE itself is recorded as a decision, because a policy that cannot
be wrong cannot be trusted. It asks: will the host I just chose get through to the next
check without changing its mind about anything? Its basis - every assertion it rests
on, as measured right now - is stored with it, so it can be settled honestly later.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, "/home/zixen15/attest-mcp2")
from attest import Store, DecisionLedger                                   # noqa: E402
from attest.decisions import now_iso                                       # noqa: E402
from attest.forecast import record_forecasts, resolve_due                # noqa: E402
from attest.trust import (AlwaysHolds, Assertion, FlipRate, HostStability,  # noqa: E402
                         assertions_to_items, current_value, host_stability, route)

HOME = "/home/zixen15"
FLEET = HOME + "/selfmodel/fleet_store.json"
HIST = os.path.join(HOME, ".omni_brain", "fleet_trust_history.json")
DB = os.environ.get("ATTEST_DB") or (HOME + "/.attest-ledger/attest.db")
STRATUM = "hosttrust"
ROUTE_STRATUM = "hostroute"
# Reserved key in the history file. It is not an assertion and must never be
# mistaken for one.
STAMP_KEY = "__selfmodel_built_at__"


def load_history(path):
    """FileNotFoundError is a first run; anything else is a fault.

    The same fix as the panel's: catching everything and returning {} turns a
    corrupt file into "no history yet", which degrades the base-rate and
    host-stability forecasters silently while every other signal stays green.
    """
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise RuntimeError(
            "the history file %s exists but cannot be read (%s: %s). Refusing to "
            "continue rather than silently treating it as empty."
            % (path, type(exc).__name__, exc))
    out = {}
    for k, v in raw.items():
        if k == STAMP_KEY:
            out[k] = v
        elif isinstance(v, list):
            out[k] = [bool(x) for x in v]
    return out


def save_history(path, h):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(h, fh)
    os.replace(tmp, path)


def read_fleet(path):
    """Every assertion the self-model currently holds, measured, not believed.

    Also returns the self-model's own build stamp. That stamp is what makes this
    driver idempotent: a run that has not been preceded by a NEW check has nothing
    to settle and nothing to forecast, and running it anyway would append a second
    observation a few minutes after the first and record a stable transition that
    never happened.
    """
    with open(path) as fh:
        fs = json.load(fh)
    out = []
    for host, blob in (fs.get("hosts") or {}).items():
        for r in blob.get("rows", []):
            # A row carries the value the assertion had WHEN CLAIMED and the
            # verdict on it now, but not observed_now. Taking observed alone means
            # a refuted row is read as True and a recovered one as False - see
            # attest.trust.current_value, which is where the two are put back
            # together. status rides along so a consumer can tell an aged row from
            # a current one.
            status = r.get("status")
            observed = current_value(r.get("observed"), status)
            if not isinstance(observed, bool):
                observed = None           # null on some hosts: NOT measurable
            out.append(Assertion(host, r.get("key", "?"), r.get("kind", "?"),
                                 observed, r.get("note", ""), status=status))
    return out, fs.get("built_at")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=330,
                    help="horizon. Shorter than the 6h check interval on purpose, so "
                         "the next check always clears it.")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--history", default=HIST)
    ap.add_argument("--fleet", default=FLEET)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="run even if the self-model has not re-checked since last time")
    args = ap.parse_args()

    assertions, built_at = read_fleet(args.fleet)
    measured = [a for a in assertions if a.observed is not None]
    unmeasured = [a for a in assertions if a.observed is None]
    # Ageing is not change, but it is not freshness either. Counted and reported so
    # that the basis of every forecast below is STATED rather than implied. A row
    # whose claim is older than the TTL its own author declared is evidence about
    # then; it is still used, but the run says how much of the evidence is aged, and
    # the routing decision carries the number with it.
    aged = [a for a in measured if not a.current]
    if not measured:
        print("the fleet store holds no measurable assertion; settling nothing")
        return 2

    observed = {"%s|%s" % (a.host, a.key): bool(a.observed) for a in measured}
    host_of = {"%s|%s" % (a.host, a.key): a.host for a in measured}
    CURRENT = {"%s|%s" % (a.host, a.key): a.current for a in measured}
    hosts = sorted({a.host for a in measured})

    history_probe = load_history(args.history)
    last_built = history_probe.get(STAMP_KEY)
    if built_at is not None and last_built is not None and built_at == last_built             and not args.force:
        print("no new self-model check since %s (built_at unchanged); nothing to "
              "settle and nothing to forecast" % built_at)
        print("   the assertions on file were already forecast when that check ran")
        return 0

    if args.dry_run:
        print(json.dumps({"hosts": hosts, "measured": len(measured),
                          "unmeasured": len(unmeasured)}, indent=2))
        return 0

    ledger = DecisionLedger(Store(args.db))
    history = load_history(args.history)

    def settle(dec):
        strat = dec.get("stratum")
        target = dec["target"]
        if strat == STRATUM:
            if target not in observed:
                return None               # the assertion is gone; not knowable
            now_v = observed[target]
            return ("still holds" if now_v else "no longer holds", now_v)
        if strat == ROUTE_STRATUM:
            # Settled against the basis stored when the choice was made.
            if target not in hosts:
                return None
            try:
                basis = json.loads(dec.get("alternatives") or "{}").get("basis") or {}
            except Exception:
                basis = {}
            if not basis:
                return None
            changed = [k for k, was in basis.items()
                       if observed.get(k) is not None and observed[k] != was]
            return ("host was unchanged" if not changed
                    else "host changed: " + ", ".join(sorted(changed)[:4]),
                    not changed)
            
        return None

    resolved = resolve_due(ledger, settle, now=now_iso())

    prev = len(history.get(next(iter(host_of)), []) or []) if host_of else 0
    st = host_stability(history, host_of)

    # ---- record: one forecast per assertion -------------------------------------
    items = assertions_to_items(measured, args.minutes)
    forecasters = [AlwaysHolds(observed), FlipRate(history, observed),
                   HostStability(history, observed, host_of)]
    written = record_forecasts(ledger, items, forecasters,
                               cohort=STRATUM + "-" + now_iso(),
                               due_at=now_iso(args.minutes * 60), who="fleet-trust",
                               history=history)

    # ---- record: the routing choice, with the basis it rests on ----------------
    choice = route(hosts, st)
    route_written = None
    if choice["chosen"]:
        chosen = choice["chosen"]
        basis = {k: v for k, v in observed.items() if host_of.get(k) == chosen}
        basis_aged = [k for k in basis if not CURRENT.get(k, True)]
        p = (choice["ranked"][0]["stable_probability"] or 0.0) ** max(1, len(basis))
        route_written = ledger.record(
            question="will %s get to the next check without changing its mind "
                     "about anything?" % chosen,
            answer="true" if p >= 0.5 else "false", qtype="noul",
            probability=p, confidence=p,
            state="chosen from %d host(s); basis is %d measured assertion(s), "
                  "%d of them aged past their declared ttl"
                  % (len(choice["ranked"]), len(basis), len(basis_aged)),
            model="router:max-stability", who="fleet-trust",
            alternatives={"basis": basis, "basis_aged": sorted(basis_aged),
                          "ranked": choice["ranked"],
                          "excluded": choice["excluded"]},
            due_at=now_iso(args.minutes * 60), stratum=ROUTE_STRATUM,
            cohort=ROUTE_STRATUM + "-" + now_iso(), target=chosen)

    # ---- remember what was seen (observations, not opinions) -------------------
    for target, v in observed.items():
        history.setdefault(target, []).append(bool(v))
        history[target] = history[target][-400:]
    history[STAMP_KEY] = built_at
    save_history(args.history, history)

    print("fleet trust %s: %d assertion(s) measured (%d current, %d aged past ttl), "
          "%d unmeasurable, %d host(s)"
          % (now_iso(), len(measured), len(measured) - len(aged), len(aged),
             len(unmeasured), len(hosts)))
    print("settled %d, still open %d, refused %d"
          % (resolved["settled"], len(resolved["still_open"]), len(resolved["refused"])))
    print("recorded %d assertion forecast(s) at +%dm" % (written["recorded"],
                                                         args.minutes))
    if route_written and route_written.get("ok"):
        print("routing: %s" % choice["verdict"])
    else:
        print("routing: no host had enough evidence; nothing was chosen. %s"
              % choice["verdict"][:150])
    return 0


if __name__ == "__main__":
    sys.exit(main())

