#!/usr/bin/env python3
"""panel_forecast.py - a worked example of the loop the ledger was built for.

WHAT THIS IS
-------------
Sample a panel of facts about a host. Write down, while the answer is still
unknown, each forecaster's probability that the next sample will match. Then,
when the next sample arrives, settle the old forecasts against what actually
happened and let the ledger say who was right.

That is the whole thing. Everything interesting is in the parts that refuse.

WHAT IT REFUSES TO DO
---------------------
  * It will not record a forecast whose outcome is already knowable. The ledger
    rejects a past due_at outright.
  * If a forecaster has no opinion, nothing is recorded for it. Absence is not
    0.5, and a model that was down must not score as a model that was unsure.
  * If the model is unreachable or answers with something unparseable, that is
    a refusal, not a probability.
  * It never settles a forecast it cannot check. Those stay OPEN and are
    reported as open, because "nobody checked" and "it was right" are different
    facts and only one of them is a result.

USAGE
-----
    python3 panel_forecast.py --config panel.json --db ~/.attest/attest.db
    python3 panel_forecast.py --config panel.json --once --dry-run

Run it on a timer at the panel's own cadence. Each run settles what came due and
writes the next batch.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from attest import Store, DecisionLedger                                   # noqa: E402
from attest.decisions import now_iso                                       # noqa: E402
from attest.forecast import (BaseRate, Constant, Forecaster, Item,           # noqa: E402
                             Persistence, record_forecasts, resolve_due,
                             head_to_head)

HISTORY_KEEP = 200


# ------------------------------------------------------------------ measuring
def _meminfo() -> dict:
    out = {}
    try:
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                v = v.strip().split()
                if v:
                    out[k.strip()] = float(v[0])   # kB
    except OSError:
        pass
    return out


def measure(spec: dict) -> bool | None:
    """One panel item. None means this host cannot answer, and None is not False.

    A measurement that could not be taken and a measurement that came back
    negative are different facts. Collapsing them is how an estate convinces
    itself a broken probe is good news.
    """
    kind = spec.get("kind")
    try:
        if kind == "load_under":
            return os.getloadavg()[0] < float(spec["value"])
        if kind == "mem_available_over_gib":
            m = _meminfo()
            return (m.get("MemAvailable", 0.0) / 1048576.0) > float(spec["value"])
        if kind == "swap_used_under_gib":
            m = _meminfo()
            used = (m.get("SwapTotal", 0.0) - m.get("SwapFree", 0.0)) / 1048576.0
            return used < float(spec["value"])
        if kind == "fs_used_under_pct":
            st = os.statvfs(spec.get("path", "/"))
            total = st.f_blocks
            free = st.f_bavail
            if total <= 0:
                return None
            return (100.0 * (total - free) / total) < float(spec["value"])
        if kind == "no_failed_user_units":
            r = subprocess.run(
                ["systemctl", "--user", "--failed", "--plain", "--no-legend",
                 "--no-pager"], capture_output=True, text=True, timeout=20)
            if r.returncode != 0:
                return None
            return len([ln for ln in r.stdout.splitlines() if ln.strip()]) == 0
        if kind == "tcp_open":
            with socket.create_connection(("127.0.0.1", int(spec["port"])), 3):
                return True
        if kind == "http_ok":
            with urllib.request.urlopen(spec["url"], timeout=5) as resp:
                return 200 <= int(resp.status) < 300
        if kind == "file_fresh_within_s":
            age = time.time() - os.path.getmtime(spec["path"])
            return age < float(spec["value"])
    except Exception:
        return None
    return None


def sample_panel(panel: dict) -> dict:
    return {name: measure(spec) for name, spec in panel.items()}


# -------------------------------------------------------------------- history
def load_history(path: str) -> dict:
    try:
        with open(path, "r") as fh:
            h = json.load(fh)
        return {k: [bool(x) for x in v] for k, v in h.items() if isinstance(v, list)}
    except Exception:
        return {}


def save_history(path: str, observed: dict, history: dict) -> None:
    """Remember what was measured - observations, not opinions.

    Kept apart from the ledger on purpose. The ledger holds what somebody
    PREDICTED; this holds what was SEEN. Merging them is how a system starts
    grading its own homework.
    """
    for k, v in observed.items():
        if v is None:
            continue
        history.setdefault(k, []).append(bool(v))
        history[k] = history[k][-HISTORY_KEEP:]
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(history, fh)
    os.replace(tmp, path)


# ---------------------------------------------------------------- the model
PROMPT = """You are forecasting the next sample of a Linux server's state.

The server is sampled every {mins} minutes. Below is the CURRENT sample, and for \
each item how often it has been true over recent samples.

Give the probability, from 0.00 to 1.00, that each item will be TRUE at the NEXT \
sample, {mins} minutes from now.

Rules, and they matter more than the numbers:
- A stable item that has never changed should get a probability close to 1.00 or \
0.00, not 0.50. Do not hedge towards the middle out of politeness.
- If an item is genuinely unpredictable, say so with a probability near 0.50.
- Use the recent rate as your starting point, then move it only if the current \
sample or the trend gives you a reason to.
- Output JSON only. No prose, no explanation, no markdown fence.

CURRENT SAMPLE:
{current}

RECENT HISTORY (true/total over the last {keep} samples):
{rates}

Output exactly this shape:
{{"items": {{{{{keys}}}}}}}
"""


class ModelForecaster(Forecaster):
    """Asks a local model for the whole panel in one call, and caches the answer.

    One request per run rather than one per item: the item-loop interface is
    convenient for the ledger and it would be dishonest to let it turn into
    len(panel) model calls.

    Any failure to answer is None, never 0.5. A model that was unreachable and a
    model that genuinely thought it was a coin flip are not the same record, and
    only one of them belongs in a calibration table.
    """

    def __init__(self, url: str, model: str, current: dict, history: dict,
                 minutes: int, timeout: int = 300, keep_alive: str = "30m",
                 name: str | None = None):
        self.url = url.rstrip("/")
        self.model = model
        self.current = current
        self.history = history
        self.minutes = minutes
        self.timeout = timeout
        # Without this the server unloads the model between runs and every
        # sample pays the full cold-load again. On this hardware that is minutes
        # of GPU per run to answer ten yes/no questions.
        self.keep_alive = keep_alive
        self.name = name or ("model:" + model)
        self._cache = None
        self.error = None
        self.warning = None

    def _ask(self) -> dict:
        cur = {k: v for k, v in self.current.items() if v is not None}
        rates = {}
        for k in cur:
            h = self.history.get(k) or []
            rates[k] = "%d/%d" % (sum(1 for x in h if x), len(h)) if h else "no history"
        # The example MUST NOT contain a number. An earlier version built this
        # with '"%s": 0.00' for every key, and the model returned 0.0 for all
        # fourteen items - including two that were provably true at that moment.
        # It had been handed a template and it filled in the template. A
        # placeholder cannot be mistaken for an answer.
        keys = ", ".join('"%s": <probability>' % k for k in cur)
        prompt = PROMPT.format(mins=self.minutes, current=json.dumps(cur, indent=2),
                               rates=json.dumps(rates, indent=2),
                               keep=HISTORY_KEEP, keys=keys)
        body = json.dumps({"model": self.model, "prompt": prompt, "stream": False,
                           "format": "json", "keep_alive": self.keep_alive,
                           "options": {"temperature": 0.0, "num_predict": 900}}).encode()
        req = urllib.request.Request(self.url + "/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode())
        text = payload.get("response", "")
        try:
            parsed = json.loads(text)
        except Exception as exc:
            self.error = "model did not return JSON: %s" % exc
            return {}
        items = parsed.get("items", parsed)
        out = {}
        for k, v in items.items():
            if k in cur:
                try:
                    out[k] = min(1.0, max(0.0, float(v)))
                except (TypeError, ValueError):
                    continue
        if not out:
            self.error = "model returned JSON with no usable probabilities"
        elif len(out) >= 4 and len(set(out.values())) == 1:
            # Recorded, not suppressed: the model really did say this. But a
            # single probability repeated across a whole panel is an echo, not a
            # forecast, and it should never be read as one.
            self.warning = ("model gave the same probability (%.3f) for all %d "
                            "items - an echo, not a forecast"
                            % (list(out.values())[0], len(out)))
        return out

    def predict(self, item: Item, history=None):
        if self._cache is None:
            try:
                self._cache = self._ask()
            except Exception as exc:
                self.error = "%s: %s" % (type(exc).__name__, exc)
                self._cache = {}
        return self._cache.get(item.target)


# ----------------------------------------------------------------- the loop
def run_once(ledger: DecisionLedger, panel: dict, args, history: dict) -> dict:
    now = time.time()
    observed = sample_panel(panel)
    measurable = {k: v for k, v in observed.items() if v is not None}

    # 1. Settle everything that has come due, against the sample just taken.
    def settle(dec):
        if dec.get("stratum") != args.stratum:
            return None
        if dec["target"] not in measurable:
            return None
        actual = bool(measurable[dec["target"]])
        return ("true at the next sample" if actual else "false at the next sample",
                actual)

    resolved = resolve_due(ledger, settle, now=now_iso())

    # 2. Record the next batch, about a future that has not happened yet.
    items = [Item(name, "will %s be true at the next sample?" % name,
                  context="panel sample at " + now_iso(), stratum=args.stratum)
             for name in measurable]

    # THE NULL CONTROL IS NOT OPTIONAL. Constant(0.5) has a Brier score of exactly
    # 0.25 on any population whatsoever, because (0.5-1)^2 and (0.5-0)^2 are both
    # 0.25. That makes it a fixed reference line that does not move when the estate
    # does, and it is the number any real forecaster is measured against: worse than
    # 0.25 means worse than knowing nothing at all. Without it in the population, a
    # table of Brier scores is a set of numbers with no scale.
    forecasters = [Constant(0.5, "null-0.50"), BaseRate(history),
                   Persistence(observed)]
    model_stats = None
    if args.model and not args.no_model:
        mf = ModelForecaster(args.model_url, args.model, measurable, history,
                             args.minutes, timeout=args.model_timeout,
                             keep_alive=args.model_keep_alive)
        forecasters.append(mf)
        forecasters, model_stats = forecasters, mf

    written = record_forecasts(ledger, items, forecasters,
                               cohort=args.stratum + "-" + now_iso(),
                               due_at=now_iso(args.minutes * 60),
                               who=args.who, history=history)

    if model_stats is not None:
        written["model_error"] = model_stats.error
        written["model_warning"] = model_stats.warning
        written["model_answered"] = len(model_stats._cache or {})

    save_history(args.history, observed, history)
    return {"at": now_iso(), "panel": len(panel), "measurable": len(measurable),
            "unmeasurable": sorted(k for k, v in observed.items() if v is None),
            "resolved": resolved, "written": written}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True, help="panel definition (JSON)")
    ap.add_argument("--db", default=os.environ.get("ATTEST_DB") or
                    os.path.join(os.path.expanduser("~"), ".attest", "attest.db"))
    ap.add_argument("--history", default=os.path.join(
        os.path.expanduser("~"), ".omni_brain", "panel_history.json"))
    ap.add_argument("--minutes", type=int, default=15,
                    help="horizon of each forecast, matching the timer interval")
    ap.add_argument("--stratum", default="panel",
                    help="names this cadence, so horizons are never pooled")
    ap.add_argument("--who", default="panel-forecast")
    ap.add_argument("--model", default=os.environ.get("PANEL_MODEL", ""),
                    help="optional local model to compare against the baselines")
    ap.add_argument("--model-url", default=os.environ.get(
        "PANEL_MODEL_URL", "http://127.0.0.1:11434"))
    ap.add_argument("--model-timeout", type=int, default=300)
    ap.add_argument("--model-keep-alive", default="30m",
                    help="keeps the model resident between runs; without it every "
                         "sample pays the cold load again")
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="sample and settle nothing; show what would happen")
    args = ap.parse_args()

    with open(args.config, "r") as fh:
        panel = json.load(fh)
    if not panel:
        print("panel is empty - nothing to forecast", file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps({"dry_run": True, "sample": sample_panel(panel)},
                         indent=2, sort_keys=True))
        return 0

    store = Store(args.db)
    ledger = DecisionLedger(store)
    history = load_history(args.history)
    out = run_once(ledger, panel, args, history)

    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print("panel %d item(s), %d measurable" % (out["panel"], out["measurable"]))
        if out["unmeasurable"]:
            print("NOT MEASURED: " + ", ".join(out["unmeasurable"]))
        print("settled %d due forecast(s)" % out["resolved"]["settled"])
        print("recorded %d new forecast(s) for +%dm"
              % (out["written"]["recorded"], args.minutes))
        if out["written"].get("refused"):
            print("refused %d" % len(out["written"]["refused"]))
        if out["written"].get("model_error"):
            print("model did not answer: %s" % out["written"]["model_error"])
        if out["written"].get("model_warning"):
            print("model WARNING: %s" % out["written"]["model_warning"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
