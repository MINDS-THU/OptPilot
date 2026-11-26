"""Shared supply, admission and evidence for versioned experiments.

The registry owns manually registered supply across projects using this registry.
A person's declaration or an OptPilot reservation is not a remote provider receipt.
Quantities in storage are decimal strings. Public amounts use the manifest's
JSON numbers. One resource ID denotes one independent inventory or quota pool.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import time

from .realm.refs import canonical_json_bytes, request_digest


MODES = {"capacity", "consumable", "quota"}
OUTCOMES = {"succeeded", "failed", "cancelled"}
_ZERO = Decimal(0)
_ELAPSED = "execution.wall_time"


def registry_directory():
    if path := os.environ.get("OPTPILOT_RESOURCE_STORE"):
        return Path(path).expanduser()
    if path := os.environ.get("OPTPILOT_DATA_DIR"):
        return Path(path).expanduser() / "experiment-resources"
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "OptPilot" / "experiment-resources"


@contextmanager
def _db(*, write=False):
    directory = registry_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(directory / "resources.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS offers (
                id TEXT PRIMARY KEY, spec TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS removed_offers (
                id TEXT PRIMARY KEY REFERENCES offers(id), removed TEXT NOT NULL);
            CREATE VIEW IF NOT EXISTS registered_offers AS
                SELECT * FROM offers WHERE NOT EXISTS (
                    SELECT 1 FROM removed_offers WHERE removed_offers.id=offers.id);
            CREATE TABLE IF NOT EXISTS owners (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, plan TEXT NOT NULL,
                outcome TEXT NOT NULL, finished TEXT, started INTEGER);
            CREATE TABLE IF NOT EXISTS allocations (
                owner TEXT NOT NULL REFERENCES owners(id), key TEXT NOT NULL,
                resource TEXT NOT NULL REFERENCES offers(id), amount TEXT NOT NULL,
                claim TEXT NOT NULL, history_key TEXT NOT NULL, created TEXT NOT NULL,
                state TEXT NOT NULL, observed TEXT NOT NULL DEFAULT '0', complete INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(owner, key));
            CREATE INDEX IF NOT EXISTS allocations_resource ON allocations(resource, state);
            CREATE INDEX IF NOT EXISTS allocations_history ON allocations(history_key, complete);
            CREATE TRIGGER IF NOT EXISTS allocations_require_registered_offer
                BEFORE INSERT ON allocations
                WHEN EXISTS (SELECT 1 FROM removed_offers WHERE id=NEW.resource)
                BEGIN SELECT RAISE(ABORT, '该实验资源已删除，请重新检查运行计划。'); END;
            CREATE TABLE IF NOT EXISTS evidence (
                resource TEXT NOT NULL, receipt TEXT NOT NULL, owner TEXT NOT NULL, key TEXT NOT NULL,
                capability TEXT NOT NULL, mode TEXT NOT NULL, unit TEXT NOT NULL,
                context TEXT NOT NULL, amount TEXT NOT NULL, complete INTEGER NOT NULL,
                created TEXT NOT NULL, epoch INTEGER NOT NULL, PRIMARY KEY(resource, receipt));
            CREATE INDEX IF NOT EXISTS evidence_owner ON evidence(owner, key);
            CREATE INDEX IF NOT EXISTS evidence_window ON evidence(resource, epoch);
            CREATE TABLE IF NOT EXISTS durations (
                owner TEXT NOT NULL, receipt TEXT NOT NULL, history_key TEXT NOT NULL,
                context TEXT NOT NULL, amount TEXT NOT NULL, outcome TEXT NOT NULL,
                created TEXT NOT NULL, PRIMARY KEY(owner, receipt));
            CREATE INDEX IF NOT EXISTS durations_history ON durations(history_key, outcome);
        """)
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield db
        db.commit()
    finally:
        db.close()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value):
    return canonical_json_bytes(value).decode("utf-8")


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError(f"{label} must be nonempty text (at most 2048 characters).")
    return value


def _amount(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Resource quantities must be JSON numbers.")
    if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError("Resource quantities must be finite and nonnegative.")
    return Decimal(str(value))


def _public(value):
    return int(value) if value == value.to_integral_value() else float(value)


def _offer(spec):
    if not isinstance(spec, dict):
        raise ValueError("Resource registration requires an object.")
    allowed = {"id", "name", "capability", "unit", "mode", "capacity", "provider", "properties",
               "location", "note", "windowSeconds", "aliases", "owner", "access", "verifiedAt"}
    extra = set(spec) - allowed
    if extra:
        raise ValueError(f"Unknown resource registration fields: {', '.join(sorted(extra))}.")
    out = {k: _text(spec.get(k), k) for k in ("id", "name", "capability", "unit", "mode")}
    if out["mode"] not in MODES or spec.get("provider", "manual") not in {"manual", "managed"}:
        raise ValueError("Unsupported resource mode or provider.")
    out["capacity"] = _public(_amount(spec.get("capacity")))
    out["provider"] = spec.get("provider", "manual")
    out["access"] = spec.get("access", "allocate")
    if out["access"] not in ("allocate", "inspect"):
        raise ValueError("Resource access must be allocate or inspect.")
    if spec.get("verifiedAt") is not None:
        try:
            verified = datetime.fromisoformat(spec["verifiedAt"].replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("verifiedAt requires an ISO 8601 timestamp with a time zone.") from exc
        if verified.tzinfo is None:
            raise ValueError("verifiedAt requires a time zone.")
        out["verifiedAt"] = verified.astimezone(timezone.utc).isoformat()
    props = spec.get("properties", {})
    if not isinstance(props, dict) or any(not isinstance(k, str) for k in props):
        raise ValueError("Resource properties require a JSON object.")
    out["properties"] = props
    aliases = spec.get("aliases", [])
    if not isinstance(aliases, list):
        raise ValueError("Resource capability aliases require an array.")
    out["aliases"] = sorted({_text(x, "alias") for x in aliases})
    for k in ("location", "note", "owner"):
        if k in spec:
            if not isinstance(spec[k], str):
                raise ValueError(f"{k} must be text.")
            out[k] = spec[k]
    window = spec.get("windowSeconds")
    if window is not None:
        if out["mode"] != "quota" or type(window) is not int or window < 1:
            raise ValueError("windowSeconds must be a positive whole number for a quota.")
        out["windowSeconds"] = window
    return out


def _remaining_hold(mode, amount, observed):
    return amount if mode == "capacity" else max(_ZERO, amount - observed)


def _supplies(db):
    supplies = {}
    epoch = int(time())
    for row in db.execute("SELECT * FROM registered_offers ORDER BY id"):
        spec = json.loads(row["spec"])
        spec.update(updatedAt=row["updated"], reserved=_ZERO, consumed=_ZERO)
        supplies[spec["id"]] = spec
    for row in db.execute("SELECT resource, amount, observed, state FROM allocations WHERE state IN ('held','pending')"):
        spec = supplies[row["resource"]]
        spec["reserved"] += _remaining_hold(spec["mode"], Decimal(row["amount"]), Decimal(row["observed"]))
    for row in db.execute("""SELECT e.resource, e.amount, e.epoch FROM evidence e
                             JOIN registered_offers r ON r.id=e.resource
                             WHERE e.mode IN ('consumable','quota')"""):
        spec = supplies[row["resource"]]
        window = spec.get("windowSeconds")
        if not window or epoch // window == row["epoch"] // window:
            spec["consumed"] += Decimal(row["amount"])
    for spec in supplies.values():
        spec["available"] = _amount(spec["capacity"]) - spec["reserved"] - spec["consumed"]
        for k in ("reserved", "consumed", "available"):
            spec[k] = _public(spec[k])
    return supplies


def inventory():
    """List offers and the immediately available OptPilot-managed quantities."""
    with _db() as db:
        return list(_supplies(db).values())


def delete(resource_id):
    """Withdraw a supply from matching, retaining identities used in previous allocations."""
    _text(resource_id, "resourceId")
    with _db(write=True) as db:
        row = db.execute("SELECT spec FROM registered_offers WHERE id=?", (resource_id,)).fetchone()
        if not row:
            return {"resourceId": resource_id, "deleted": False}
        states = dict(db.execute("SELECT state, count(*) FROM allocations WHERE resource=? GROUP BY state",
                                 (resource_id,)))
        held, pending = states.get("held", 0), states.get("pending", 0)
        if held or pending:
            name = json.loads(row["spec"])["name"]
            raise ValueError(f"「{name}」有 {held} 条占用中的分配、{pending} 条待核销的分配。"
                             "请先在评测详情中结束占用或核实实际用量，再删除资源。")
        if states:
            db.execute("INSERT INTO removed_offers(id,removed) VALUES (?,?)", (resource_id, _now()))
        else:
            db.execute("DELETE FROM offers WHERE id=?", (resource_id,))
        return {"resourceId": resource_id, "deleted": True}


def register(spec):
    """Register or amend a supply. Capacity is gross; editing it never erases withdrawals."""
    spec = _offer(spec)
    with _db(write=True) as db:
        old = db.execute("SELECT spec FROM offers WHERE id=?", (spec["id"],)).fetchone()
        if old:
            prior = json.loads(old["spec"])
            occupied = db.execute("SELECT 1 FROM allocations WHERE resource=? LIMIT 1", (spec["id"],)).fetchone()
            identity = ("capability", "unit", "mode", "provider", "windowSeconds")
            if occupied and any(prior.get(k) != spec.get(k) for k in identity):
                raise ValueError("A used supply's capability, units, provider and quota period are immutable.")
            active = db.execute("SELECT 1 FROM allocations WHERE resource=? AND state IN ('held','pending') LIMIT 1",
                                (spec["id"],)).fetchone()
            if active and any(prior.get(k) != spec.get(k) for k in ("properties", "location", "aliases")):
                raise ValueError("Reconcile active allocations before changing a supply's capabilities or location.")
        db.execute("""INSERT INTO offers VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET
                      spec=excluded.spec, updated=excluded.updated""", (spec["id"], _json(spec), _now()))
        db.execute("DELETE FROM removed_offers WHERE id=?", (spec["id"],))
        return _supplies(db)[spec["id"]]


def _history_key(claim):
    return request_digest({k: claim.get(k) for k in ("capability", "unit", "mode", "properties", "location")}
                          | {"context": claim.get("context", {})})


def _estimate(db, claim):
    fingerprint = _history_key(claim)
    parameters = [fingerprint]
    if claim["capability"] == _ELAPSED:
        query = """SELECT amount AS sample, owner, receipt, amount, NULL AS resource
                   FROM durations WHERE history_key=? AND outcome='succeeded'
                   ORDER BY created DESC, rowid DESC"""
    else:
        # Each completed allocation contributes one sample. Its final receipt may
        # contain only the last increment of a multi-receipt withdrawal.
        query = """SELECT a.observed AS sample, a.owner, e.receipt, e.amount, a.resource
                   FROM allocations a JOIN owners o ON o.id=a.owner
                   JOIN evidence e ON e.owner=a.owner AND e.key=a.key AND e.complete=1
                   WHERE a.history_key=? AND a.complete=1 AND o.outcome='succeeded'"""
        if resource := claim.get("resourceId"):
            query += " AND a.resource=?"
            parameters.append(resource)
        query += " ORDER BY e.created DESC, e.rowid DESC"
    count, total, low, high = 0, _ZERO, None, None
    sources = []
    for row in db.execute(query, parameters):
        sample = Decimal(row["sample"])
        count += 1
        total += sample
        low = sample if low is None else min(low, sample)
        high = sample if high is None else max(high, sample)
        if len(sources) < 3:
            amount = Decimal(row["amount"])
            source = {"owner": row["owner"], "receipt": row["receipt"], "amount": _public(amount)}
            if row["resource"] is not None:
                source["resourceId"] = row["resource"]
            if amount != sample:
                source["sampleAmount"] = _public(sample)
            sources.append(source)
    return {"count": count, "unit": claim["unit"], "min": _public(low) if count else None,
            "max": _public(high) if count else None, "mean": _public(total / count) if count else None,
            "sources": sources}


def estimate(claim):
    with _db() as db:
        return _estimate(db, claim)


def estimates(claims):
    with _db() as db:
        cached = {}
        result = []
        for claim in claims:
            key = _history_key(claim), claim.get("resourceId")
            if key not in cached:
                cached[key] = _estimate(db, claim)
            result.append(cached[key])
        return result


def _claim_candidates(claim, supplies, *, match_capability=True):
    for k in ("key", "capability", "unit", "mode"):
        _text(claim.get(k), k)
    if claim["mode"] not in MODES:
        raise ValueError("Invalid resource requirement mode.")
    values = {k: _amount(claim[k]) for k in ("min", "max", "expected") if claim.get(k) is not None}
    if "max" in values and values.get("min", _ZERO) > values["max"]:
        raise ValueError("Resource minimum exceeds maximum.")
    props = claim.get("properties", {})
    if not isinstance(props, dict):
        raise ValueError("Resource property conditions require an object.")
    constraints = claim.get("constraints", {})
    if (not isinstance(constraints, dict) or set(constraints) - {"sameLocation", "distinctResources"}
            or any(not isinstance(v, str) or not v.strip() for v in constraints.values())):
        return [], None, "unsupported", "This provider cannot verify the declared resource relationships."
    for k in ("requires", "window", "group", "relations"):
        if claim.get(k):
            return [], None, "unsupported", f"The registered provider cannot verify {k}."
    amount = values.get("max")
    if amount is None and claim["mode"] == "capacity":
        amount = values.get("min")
    if amount is None:
        return [], None, "unknown", "Declare a quantity or an authorized upper bound before reserving."
    matches = [s for s in supplies.values()
               if (not match_capability or claim["capability"] in [s["capability"], *s.get("aliases", [])])
               and s["mode"] == claim["mode"] and s["unit"] == claim["unit"]
               and (not claim.get("location") or claim["location"] == s.get("location"))
               and (not claim.get("resourceId") or claim["resourceId"] == s["id"])
               and all(k in s["properties"] and s["properties"][k] == v for k, v in props.items())]
    if not matches:
        return [], amount, "missing", "Register a supply matching the capability, units, location and properties."
    reservable = [s for s in matches if s.get("access", "allocate") == "allocate"]
    if not reservable:
        names = ", ".join(s["name"] for s in matches)
        return [], amount, "unsupported", f"Read-only supply ({names}). Confirm allocation access with its owner."
    matches = reservable
    if "sameLocation" in constraints:
        matches = [s for s in matches if s.get("location", "").strip()]
        if not matches:
            return [], amount, "unknown", "Record the physical or logical location of the matching supplies."
    if claim["mode"] == "quota":
        matches = [s for s in matches if s.get("windowSeconds")]
        if not matches:
            return [], amount, "unknown", "Configure and verify the shared quota pool and accounting window."
    return [s["id"] for s in matches], amount, "ready", "Registered supply can cover this requirement."


def link_capability(claim, resource_id):
    """Record the operator's explicit capability confirmation against the current supply."""
    # Preview resourceId may describe the planner's selection. Only the ID frozen
    # in the requirement constrains which underlying supply can receive the alias.
    claim = dict(claim, resourceId=claim.get("requiredResourceId"))
    capability = claim["capability"]
    with _db(write=True) as db:
        row = db.execute("SELECT spec FROM registered_offers WHERE id=?", (resource_id,)).fetchone()
        if not row:
            raise ValueError("The selected resource is no longer registered.")
        spec = json.loads(row["spec"])
        eligible, _, _, reason = _claim_candidates(claim, {resource_id: spec}, match_capability=False)
        if not eligible:
            raise ValueError(f"Resource cannot satisfy this requirement. {reason}")
        if capability not in [spec["capability"], *spec.get("aliases", [])]:
            if claim["status"] not in ("missing", "insufficient"):
                raise ValueError("The requirement has changed. Refresh the plan before linking a resource.")
            spec["aliases"] = sorted([*spec.get("aliases", []), capability])
            db.execute("UPDATE offers SET spec=?, updated=? WHERE id=?", (_json(spec), _now(), resource_id))
        return _supplies(db)[resource_id]


def _assign(requests, supplies, items):
    """Try the tightest adequate supply first, then alternative bindings when necessary."""
    original = {k: Decimal(str(s["available"])) for k, s in supplies.items()}
    balances = original.copy()
    # Values contain only relations actually requested for the same acquisition.
    locations = defaultdict(dict)
    distinct = defaultdict(set)

    def eligible(index, supply_id, amount):
        if balances[supply_id] < amount:
            return False
        relation = items[index].get("constraints", {})
        same = relation.get("sameLocation")
        if same and locations[same] and supplies[supply_id]["location"] not in locations[same]:
            return False
        different = relation.get("distinctResources")
        return not different or supply_id not in distinct[different]

    def move(index, supply_id, amount, take):
        direction = 1 if take else -1
        balances[supply_id] -= direction * amount
        relation = items[index].get("constraints", {})
        if same := relation.get("sameLocation"):
            place = supplies[supply_id]["location"]
            locations[same][place] = locations[same].get(place, 0) + direction
            if not locations[same][place]:
                del locations[same][place]
        if different := relation.get("distinctResources"):
            if take:
                distinct[different].add(supply_id)
            else:
                distinct[different].remove(supply_id)

    bindings = {}
    for index, choices, amount in requests:
        candidates = [i for i in choices if eligible(index, i, amount)]
        if candidates:
            chosen = min(candidates, key=lambda i: (balances[i] - amount, i))
            bindings[index] = (chosen, balances[chosen])
            move(index, chosen, amount, True)
    if len(bindings) == len(requests) or all(len(choices) == 1 for _, choices, _ in requests):
        return bindings, True
    if len(requests) > 32:
        return bindings, False

    # Enumeration is bounded. Report unresolved rather than claiming mathematical
    # infeasibility if the limit is reached; allocations still commit all-or-none.
    remaining_nodes = 12000
    alternatives = {}
    balances = original.copy()
    locations.clear()
    distinct.clear()

    def visit(position):
        nonlocal remaining_nodes
        if position == len(requests):
            return True
        if remaining_nodes <= 0:
            return False
        remaining_nodes -= 1
        index, choices, amount = requests[position]
        for chosen in sorted(choices, key=lambda i: (balances[i], i)):
            if eligible(index, chosen, amount):
                alternatives[index] = (chosen, balances[chosen])
                move(index, chosen, amount, True)
                if visit(position + 1):
                    return True
                move(index, chosen, amount, False)
                del alternatives[index]
        return False

    if visit(0):
        return alternatives, True
    return bindings, remaining_nodes > 0


def _plan(db, claims, supplies=None, history=None, *, suggest_links=False):
    if not isinstance(claims, list) or not all(isinstance(c, dict) for c in claims):
        raise ValueError("Resource claims require an array of objects.")
    if supplies is None:
        supplies = _supplies(db)
    if history is None:
        history = {}
    items, requests, keys = [], [], set()
    for claim in claims:
        matches, amount, status, reason = _claim_candidates(claim, supplies)
        key = claim["key"]
        if key in keys:
            raise ValueError("Resource claim keys must be unique per reservation owner.")
        keys.add(key)
        fingerprint = _history_key(claim), claim.get("resourceId")
        if fingerprint not in history:
            history[fingerprint] = _estimate(db, claim)
        item = dict(claim, status=status, reason=reason, estimate=history[fingerprint])
        if suggest_links and claim.get("resourceId"):
            item["requiredResourceId"] = claim["resourceId"]
        if amount is not None:
            item["amount"] = _public(amount)
        if matches:
            item["available"] = max(supplies[i]["available"] for i in matches)
            requests.append((len(items), matches, amount))
        items.append(item)
    requests.sort(key=lambda r: (len(r[1]), -r[2], r[0]))
    bindings, definitive = _assign(requests, supplies, items)
    for index, choices, amount in requests:
        if index not in bindings:
            if definitive:
                items[index].update(status="insufficient", reason="Registered supply cannot satisfy the simultaneous capacity and placement constraints.")
            else:
                items[index].update(status="unknown", reason="Choose a specific supply or split the request to verify placement.")
            continue
        supply_id, available = bindings[index]
        items[index].update(resourceId=supply_id, resourceName=supplies[supply_id]["name"],
                            available=_public(available))
        fingerprint = _history_key(claims[index]), supply_id
        if fingerprint not in history:
            history[fingerprint] = _estimate(db, items[index])
        items[index]["estimate"] = history[fingerprint]
    if suggest_links:
        for item, claim in zip(items, claims):
            if item["status"] not in ("missing", "insufficient"):
                continue
            eligible, _, _, _ = _claim_candidates(claim, supplies, match_capability=False)
            candidates = []
            for resource_id in eligible:
                spec = supplies[resource_id]
                if claim["capability"] not in [spec["capability"], *spec.get("aliases", [])]:
                    candidates.append({k: spec[k] for k in ("id", "name", "available", "location") if k in spec})
            if candidates:
                item["linkCandidates"] = candidates
    return {"ready": all(i["status"] == "ready" for i in items), "items": items}


def plan(claims):
    """Read-only snapshot. A successful preview does not book supply."""
    with _db() as db:
        return _plan(db, claims)


def plan_groups(groups):
    """Independent alternative or sequential groups against the same read snapshot."""
    with _db() as db:
        supplies = _supplies(db)
        history = {}
        return [_plan(db, claims, supplies, history, suggest_links=True) for claims in groups]


def acquire(owner, claims):
    """Reserve every claim atomically, or none. Replays never reopen a settled allocation."""
    _text(owner, "owner")
    with _db(write=True) as db:
        digest = request_digest(claims)
        prior = db.execute("SELECT * FROM owners WHERE id=?", (owner,)).fetchone()
        if prior:
            if prior["digest"] != digest:
                raise ValueError("Reservation owner is already bound to different claims.")
            return dict(json.loads(prior["plan"]), owner=owner, ownerState=prior["outcome"])
        result = _plan(db, claims)
        if result["ready"] and claims:
            db.execute("INSERT INTO owners VALUES (?,?,?,'active',NULL,NULL)", (owner, digest, _json(result)))
            for c, item in zip(claims, result["items"]):
                db.execute("""INSERT INTO allocations
                           (owner,key,resource,amount,claim,history_key,created,state) VALUES (?,?,?,?,?,?,?,'held')""",
                           (owner, c["key"], item["resourceId"], str(_amount(item["amount"])),
                            _json(c), _history_key(c), _now()))
        return dict(result, owner=owner, ownerState="active" if result["ready"] else "waiting")


def _where(column, owners):
    if owners is None:
        return "", ()
    if isinstance(owners, str):
        return f" WHERE {column}=?", (owners,)
    return f" WHERE {column} IN (SELECT value FROM json_each(?))", (_json(list(owners)),)


def _allocations(db, owners):
    query = """SELECT a.*, o.outcome,o.finished FROM allocations a JOIN owners o ON a.owner=o.id"""
    clause, args = _where("a.owner", owners)
    rows = db.execute(query + clause + " ORDER BY a.created, a.key", args)
    result = []
    for row in rows:
        claim = json.loads(row["claim"])
        result.append(dict(claim, owner=row["owner"], resourceId=row["resource"], amount=_public(Decimal(row["amount"])),
                           state=row["state"], outcome=row["outcome"], observed=_public(Decimal(row["observed"])),
                           complete=bool(row["complete"]), createdAt=row["created"], finishedAt=row["finished"]))
    return result


def reservations(owner=None):
    with _db() as db:
        return _allocations(db, owner)


def _usage(db, owners):
    condition, args = _where("owner", owners)
    evidence = [
        {"owner": r["owner"], "key": r["key"], "resourceId": r["resource"], "capability": r["capability"],
         "mode": r["mode"], "unit": r["unit"], "context": json.loads(r["context"]),
         "amount": _public(Decimal(r["amount"])), "receipt": r["receipt"],
         "complete": bool(r["complete"]), "createdAt": r["created"]}
        for r in db.execute("SELECT * FROM evidence" + condition, args)
    ]
    for r in db.execute("SELECT * FROM durations" + condition, args):
        evidence.append(_duration_evidence(r))
    return sorted(evidence, key=lambda e: (e["createdAt"], e["owner"], e["receipt"]))


def usage(owner=None):
    with _db() as db:
        return _usage(db, owner)


def owner_records(owners):
    with _db() as db:
        return {"allocations": _allocations(db, owners), "observations": _usage(db, owners)}


def graph_resources(project_db):
    """Read one registry snapshot for the activities identified by this project's journal."""
    activities = project_db.execute("""
        SELECT owner, run, json_extract(spec,'$.acquired') AND json_array_length(spec,'$.claims')>0,
               settled AND json_extract(settlement,'$.elapsedSeconds') IS NOT NULL
        FROM resource_activities""")
    owners, needs_allocation, needs_duration = {}, set(), set()
    for owner, run, acquired, measured in activities:
        owners[owner] = run
        if acquired:
            needs_allocation.add(owner)
        if measured:
            needs_duration.add(owner)
    if not owners:
        return {}
    items, elapsed = defaultdict(dict), defaultdict(Decimal)
    try:
        with _db() as registry:
            clause, args = _where("a.owner", owners)
            # The cached observed amount is updated atomically with each unique receipt.
            # Receipt presence distinguishes an explicit zero from an unmeasured allocation.
            allocations = registry.execute("""
                SELECT a.owner,a.resource,a.amount,a.observed,a.state,
                       json_extract(s.spec,'$.name') AS name, json_extract(s.spec,'$.mode') AS mode,
                       json_extract(s.spec,'$.unit') AS unit,
                       EXISTS(SELECT 1 FROM evidence e WHERE e.owner=a.owner AND e.key=a.key) AS measured
                FROM allocations a JOIN offers s ON s.id=a.resource""" + clause, args)
            for row in allocations:
                owner, resource, mode = row["owner"], row["resource"], row["mode"]
                needs_allocation.discard(owner)
                item = items[owners[owner]].setdefault(resource, dict(
                    resourceId=resource, name=row["name"], unit=row["unit"], mode=mode))
                observed = Decimal(row["observed"])
                state = row["state"]
                if state in ("held", "pending"):
                    hold = _remaining_hold(mode, Decimal(row["amount"]), observed)
                    item[state] = item.get(state, _ZERO) + hold
                elif state == "released":
                    item["released"] = True
                if row["measured"]:
                    if mode == "capacity":
                        item["observedPeak"] = max(item.get("observedPeak", _ZERO), observed)
                    else:
                        item["consumed"] = item.get("consumed", _ZERO) + observed
            clause, args = _where("owner", owners)
            for owner, amount in registry.execute("SELECT owner,amount FROM durations" + clause, args):
                elapsed[owners[owner]] += Decimal(amount)
                needs_duration.discard(owner)
    except (OSError, sqlite3.DatabaseError):
        # The project graph is still readable when its shared registry is unavailable.
        return {run: {"items": [], "unavailable": True} for run in owners.values()}

    missing = {owners[owner] for owner in needs_allocation | needs_duration}
    summaries = {}
    for run in items.keys() | elapsed.keys() | missing:
        entries = sorted(items[run].values(), key=lambda e: (e["name"], e["resourceId"]))
        summary = {"items": [{k: _public(v) if isinstance(v, Decimal) else v for k, v in entry.items()}
                             for entry in entries]}
        if run in elapsed:
            summary["elapsedSeconds"] = _public(elapsed[run])
        if run in missing:
            summary["unavailable"] = True
        summaries[run] = summary
    return summaries


def _detail(db, owner):
    row = db.execute("SELECT outcome FROM owners WHERE id=?", (owner,)).fetchone()
    return {"owner": owner, "state": row[0] if row else None,
            "allocations": _allocations(db, owner), "observations": _usage(db, owner)}


def _record(db, owner, key, amount, receipt, complete):
    amount = _amount(amount)
    _text(receipt, "receipt")
    if type(complete) is not bool:
        raise ValueError("A usage receipt needs a boolean completion flag.")
    row = db.execute("""SELECT a.*,o.outcome,o.started FROM allocations a JOIN owners o ON a.owner=o.id
                        WHERE a.owner=? AND a.key=?""", (owner, key)).fetchone()
    if not row:
        raise ValueError("No such resource allocation. Check the execution owner and claim key.")
    prior = db.execute("SELECT * FROM evidence WHERE resource=? AND receipt=?",
                       (row["resource"], receipt)).fetchone()
    if prior:
        if (prior["owner"], prior["key"], Decimal(prior["amount"]), bool(prior["complete"])) != (
                owner, key, amount, complete):
            raise ValueError("This resource receipt is already bound to different usage.")
        return
    if row["complete"] or row["started"] == 0:
        raise ValueError("This resource allocation has already been finalized.")
    claim = json.loads(row["claim"])
    epoch = int(time())
    db.execute("""INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
               (row["resource"], receipt, owner, key, claim["capability"], claim["mode"], claim["unit"],
                _json(claim.get("context", {})), str(amount), int(complete), _now(), epoch))
    # Consumption receipts are increments; for reusable capacity they are observed peaks.
    observed = Decimal(row["observed"])
    observed = max(observed, amount) if claim["mode"] == "capacity" else observed + amount
    state = "reconciled" if complete and claim["mode"] != "capacity" else row["state"]
    db.execute("UPDATE allocations SET observed=?,complete=?,state=? WHERE owner=? AND key=?",
               (str(observed), int(complete), state, owner, key))


def record_usage(owner, key, amount, receipt, *, complete=True):
    """Enter a usage receipt. Amount is an increment for consumption and a peak for capacity.

    complete=True confirms the last measurement for this allocation. A final zero
    explicitly confirms that nothing further was used. An absent measurement is unknown.
    """
    with _db(write=True) as db:
        _record(db, owner, key, amount, receipt, complete)
        return _detail(db, owner)


def settle(owner, outcome, *, observations=None, started=True):
    """Release reusable capacity on terminal outcomes; retain uncertain consumption holds."""
    if outcome not in OUTCOMES or type(started) is not bool:
        raise ValueError("Invalid terminal outcome or execution-start flag.")
    with _db(write=True) as db:
        prior = db.execute("SELECT * FROM owners WHERE id=?", (owner,)).fetchone()
        if not prior:
            raise ValueError("No reservation with this owner.")
        if prior["outcome"] != "active" and (prior["outcome"], bool(prior["started"])) != (outcome, started):
            raise ValueError("The reservation has already been settled with another outcome.")
        for item in observations or []:
            _record(db, owner, item["key"], item["amount"], item["receipt"], item.get("complete", True))
        if not started and db.execute("SELECT 1 FROM evidence WHERE owner=? LIMIT 1", (owner,)).fetchone():
            raise ValueError("Recorded usage conflicts with a claim that execution never started.")
        if prior["outcome"] == "active":
            for r in db.execute("SELECT key,claim,complete FROM allocations WHERE owner=?", (owner,)):
                if not started or json.loads(r["claim"])["mode"] == "capacity":
                    state = "released"
                else:
                    state = "reconciled" if r["complete"] else "pending"
                db.execute("UPDATE allocations SET state=? WHERE owner=? AND key=?", (state, owner, r["key"]))
            db.execute("UPDATE owners SET outcome=?,finished=?,started=? WHERE id=?",
                       (outcome, _now(), int(started), owner))
        return _detail(db, owner)


def _duration_evidence(row):
    return {"owner": row["owner"], "key": _ELAPSED, "resourceId": None, "capability": _ELAPSED,
            "unit": "s", "mode": "capacity", "amount": _public(Decimal(row["amount"])),
            "receipt": row["receipt"], "complete": row["outcome"] == "succeeded", "outcome": row["outcome"],
            "context": json.loads(row["context"]), "createdAt": row["created"]}


def record_elapsed(owner, seconds, receipt, context, *, outcome):
    """Record an independently measured wall duration, even when no supply was declared."""
    _text(owner, "owner")
    _text(receipt, "receipt")
    amount = _amount(seconds)
    if outcome not in OUTCOMES or not isinstance(context, dict):
        raise ValueError("Elapsed-time evidence requires execution context and terminal outcome.")
    fingerprint = _history_key({"capability": _ELAPSED, "unit": "s", "mode": "capacity", "context": context})
    with _db(write=True) as db:
        row = db.execute("SELECT * FROM durations WHERE owner=? AND receipt=?", (owner, receipt)).fetchone()
        if row:
            if (fingerprint, amount, outcome) != (row["history_key"], Decimal(row["amount"]), row["outcome"]):
                raise ValueError("Elapsed-time receipt is already bound to a different observation.")
        else:
            db.execute("INSERT INTO durations VALUES (?,?,?,?,?,?,?)",
                       (owner, receipt, fingerprint, _json(context), str(amount), outcome, _now()))
            row = db.execute("SELECT * FROM durations WHERE owner=? AND receipt=?", (owner, receipt)).fetchone()
        return _duration_evidence(row)
