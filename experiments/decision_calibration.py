#!/usr/bin/env python3
"""Measure a decision model's calibration on THIS machine, with THIS ledger.

WHAT THIS ANSWERS
-----------------
Every write-up of Jev and of the open decision models repeats the same claim:
the probability means something. Measured on 108 claims in September 2026, the
advantage the coverage described was not there - 0.066 against 0.061 and 0.067
for two cheap chat models. At that sample size those are one number.

The only person who can measure it on YOUR decisions, with YOUR model and YOUR
outcomes, is you. So this asks a decision model a set of questions whose correct
answers are known by construction, records every decision in the attest ledger
with the confidence it was made at, resolves each one against the truth, and then
asks the ledger what it actually knows.

The interesting output is very likely to be INSUFFICIENT DATA. That is the point.
A ledger that will not yet tell you a number is worth more than one that will.

    python experiments/decision_calibration.py --url http://localhost:11434 \
        --attest http://127.0.0.1:8260 --token $TOK
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

CATEGORIES = {
    "storage": "disk space, volumes, filesystems, backups filling up",
    "runtime": "a process crashing, hanging, restarting or timing out",
    "auth": "credentials, tokens, passwords, permissions, access",
}

# Ground truth is by construction. Short, unambiguous operational messages.
ITEMS = [
    ("the volume holding /var is 98 percent full", "storage"),
    ("backups have not run in twelve days and the archive is growing", "storage"),
    ("write errors on the data disk", "storage"),
    ("an inode exhaustion on the mail spool", "storage"),
    ("the log partition filled overnight", "storage"),
    ("a snapshot is consuming sixty percent of the pool", "storage"),
    ("the nfs mount went read-only", "storage"),
    ("the temp directory is at its quota", "storage"),
    ("a disk is reporting smart errors", "storage"),
    ("the object store bucket reached its lifecycle limit", "storage"),

    ("the web worker exited with status 137 again", "runtime"),
    ("the queue consumer is stuck and not draining", "runtime"),
    ("the daemon restarted four times in ten minutes", "runtime"),
    ("a request timed out after thirty seconds", "runtime"),
    ("the scheduled job never finished and is still running", "runtime"),
    ("memory usage climbed until the process was killed", "runtime"),
    ("the service is up but not answering on its port", "runtime"),
    ("a deadlock between two threads in the importer", "runtime"),
    ("the container keeps exiting immediately after start", "runtime"),
    ("an unhandled exception in the payment handler", "runtime"),
    ("the cron job has been running for six hours", "runtime"),
    ("the background worker stopped picking up tasks", "runtime"),
    ("a segmentation fault in the native extension", "runtime"),

    ("the api token expired this morning", "auth"),
    ("a login was refused for the wrong password", "auth"),
    ("the service account lacks permission on that directory", "auth"),
    ("an ssh key was rejected by the host", "auth"),
    ("two factor verification failed for the operator", "auth"),
    ("the certificate is not valid for that hostname", "auth"),
    ("the user cannot sudo without a password", "auth"),
    ("an oauth refresh returned invalid grant", "auth"),
    ("the role binding does not allow reading the secret", "auth"),
    ("an expired session cookie was presented", "auth"),
    ("the ldap bind failed for the admin account", "auth"),
    ("a webhook signature did not verify", "auth"),
]


def post(url, payload, token=None, timeout=600):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"_http": e.code}
    except Exception as e:
        return {"_error": "%s: %s" % (type(e).__name__, e)}


def ask(decision_url, model, text, timeout=600):
    payload = {"model": model, "state": text,
               "questions": {"cat": {"type": "choice",
                                     "instructions": "Which category does this "
                                                     "operational message belong to?",
                                     "criteria": CATEGORIES}}}
    t0 = time.time()
    try:
        req = urllib.request.Request(decision_url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        return d, time.time() - t0
    except Exception as e:
        return {"_error": "%s: %s" % (type(e).__name__, e)}, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--model", default="nimble")
    ap.add_argument("--attest", default="http://127.0.0.1:8260")
    ap.add_argument("--token", default="")
    ap.add_argument("--limit", type=int, default=0, help="stop after N items")
    a = ap.parse_args()

    items = ITEMS[:a.limit] if a.limit else ITEMS
    durl = a.url.rstrip("/") + "/v1/systemone"
    print("decision model : %s at %s" % (a.model, durl))
    print("ledger         : %s" % a.attest)
    print("items          : %d" % len(items))
    print("=" * 76)

    right = wrong = unparsed = 0
    latencies = []
    for i, (text, truth) in enumerate(items, 1):
        res, secs = ask(durl, a.model, text)
        latencies.append(secs)
        ans = ((res.get("answers") or {}).get("cat") or {}) if "_error" not in res else {}
        if not ans:
            unparsed += 1
            print("  %2d  UNPARSED  %s" % (i, str(res)[:100]))
            continue
        choice = ans.get("choice")
        prob = (ans.get("probabilities") or {}).get(choice)
        rec = post(a.attest + "/attest/v1/record_decision", {
            "question": "Which category does this operational message belong to?",
            "answer": choice, "qtype": "choice",
            "probability": prob, "confidence": ans.get("confidence"),
            "state": text, "model": a.model, "who": "calibration-experiment",
            "alternatives": ans.get("probabilities")}, a.token)
        did = rec.get("id")
        correct = (choice == truth)
        if did:
            post(a.attest + "/attest/v1/resolve_decision",
                 {"id": did, "outcome": "the message was written as %s" % truth,
                  "correct": correct}, a.token)
        right += int(correct)
        wrong += int(not correct)
        mark = "ok  " if correct else "MISS"
        print("  %2d  %s  said %-8s truth %-8s p=%s  %.1fs"
              % (i, mark, choice, truth,
                 ("%.3f" % prob) if isinstance(prob, float) else "-", secs))

    n = right + wrong
    print("=" * 76)
    print("accuracy: %d/%d correct (%.1f%%), %d unparsed"
          % (right, n, (100.0 * right / n) if n else 0.0, unparsed))
    if latencies:
        print("latency : mean %.2fs, min %.2fs, max %.2fs"
              % (sum(latencies) / len(latencies), min(latencies), max(latencies)))
    print("")
    print("--- what the ledger actually knows ---")
    cal = post(a.attest + "/attest/v1/calibration",
               {"model": a.model}, a.token)
    print(json.dumps(cal, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
