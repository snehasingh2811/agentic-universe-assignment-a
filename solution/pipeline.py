from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parents[1]
_CIPHERS: dict[int, Fernet] = {}


def normalize_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return f"+91{digits}" if len(digits) == 10 and digits[0] in "6789" else None


def parse_date(value: str) -> str | None:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_decimal(value: str) -> str | None:
    cleaned = re.sub(r"^(?:₹|rs\.?|inr)\s*", "", value.strip(), flags=re.I)
    try:
        amount = Decimal(cleaned.replace(",", ""))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def normalize_header(value: str) -> str:
    return re.sub(r"[_\s]+", " ", value.strip().lower())


def parse_timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=IST)
    return stamp.astimezone(IST)


def load_config() -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, bool]]]:
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted((ROOT / "assignment-a-data" / "schemas").glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schemas[schema["workflow_id"]] = schema
    dispositions: dict[tuple[str, str], dict[str, bool]] = {}
    with (ROOT / "assignment-a-data" / "schemas" / "dispositions.csv").open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            dispositions[(row["workflow_id"], row["code"])] = {
                "is_terminal": row["is_terminal"].lower() == "true",
                "is_success": row["is_success"].lower() == "true",
            }
    return schemas, dispositions


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    key_path = db_path.with_suffix(db_path.suffix + ".key")
    if key_path.exists():
        key = key_path.read_bytes()
    else:
        key = Fernet.generate_key()
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(key)
    cipher = Fernet(key)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            phone_hash TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            fields_json TEXT NOT NULL,
            UNIQUE(workflow_id, phone_hash)
        );
        CREATE TABLE IF NOT EXISTS ingest_drops (
            path TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            call_id TEXT,
            occurred_at TEXT NOT NULL,
            phone_hash TEXT,
            payload_json TEXT NOT NULL,
            ignored INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS quality (
            name TEXT PRIMARY KEY, value INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rejections (
            workflow_id TEXT, batch_id TEXT, row_number INTEGER, reason TEXT,
            PRIMARY KEY(workflow_id, batch_id, row_number)
        );
    """)
    db.commit()
    _CIPHERS[id(db)] = cipher
    return db


def phone_hash(db: sqlite3.Connection, phone: str) -> str:
    return hmac.new(_CIPHERS[id(db)]._signing_key, phone.encode(), hashlib.sha256).hexdigest()


def encrypt_pii(db: sqlite3.Connection, value: str) -> str:
    return _CIPHERS[id(db)].encrypt(value.encode()).decode()


def bump(db: sqlite3.Connection, name: str, amount: int = 1) -> None:
    db.execute("INSERT INTO quality(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value", (name, amount))


def convert(value: str, field: dict[str, Any]) -> tuple[Any, bool]:
    value = value.strip()
    if not value:
        return None, False
    kind = field["type"]
    if kind == "string":
        return value, False
    if kind == "phone":
        result = normalize_phone(value)
        return result, result is None
    if kind == "date":
        result = parse_date(value)
        return result, result is None
    if kind == "decimal":
        result = parse_decimal(value)
        return result, result is None
    if kind == "enum":
        result = value.lower()
        valid = {item.lower() for item in field["values"]}
        return (result, False) if result in valid else (None, True)
    raise ValueError(f"Unsupported schema type: {kind}")


def ingest_leads(db: sqlite3.Connection, drop: Path, schemas: dict[str, dict[str, Any]]) -> None:
    files = sorted((drop / "leads").glob("*/*.csv"), key=lambda item: (item.parent.name, item.name))
    for path in files:
        workflow = path.parent.name
        if workflow not in schemas:
            continue
        schema = schemas[workflow]
        fields = schema["fields"]
        with path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            aliases = {}
            for field in fields:
                for name in [field["name"], *field.get("aliases", [])]:
                    aliases[normalize_header(name)] = field["name"]
            columns = {header: aliases.get(normalize_header(header)) for header in (reader.fieldnames or [])}
            row_number = 0
            for raw in reader:
                if not any((value or "").strip() for value in raw.values()):
                    continue
                row_number += 1
                values: dict[str, str] = {}
                for header, value in raw.items():
                    target = columns.get(header)
                    if target and value is not None:
                        values[target] = value.strip()
                parsed: dict[str, Any] = {}
                reason = None
                warnings = 0
                for field in fields:
                    name = field["name"]
                    value, invalid = convert(values.get(name, ""), field)
                    if name == "phone" and invalid:
                        reason = "invalid_phone"
                        break
                    if field.get("required") and value is None:
                        reason = f"missing_required:{name}"
                        break
                    if invalid:
                        warnings += 1
                    parsed[name] = value
                bump(db, "lead_rows_total")
                if reason:
                    db.execute("INSERT OR IGNORE INTO rejections VALUES(?,?,?,?)", (workflow, path.stem, row_number, reason))
                    bump(db, "lead_rows_rejected")
                    bump(db, f"rejection:{reason}")
                    continue
                bump(db, "field_warnings", warnings)
                canonical = parsed["phone"]
                key = phone_hash(db, canonical)
                existing = db.execute("SELECT id,fields_json FROM leads WHERE workflow_id=? AND phone_hash=?", (workflow, key)).fetchone()
                stored = {k: v for k, v in parsed.items() if not next(f for f in fields if f["name"] == k).get("pii")}
                for field in fields:
                    if field.get("pii") and parsed.get(field["name"]) is not None:
                        stored[field["name"]] = encrypt_pii(db, parsed[field["name"]])
                if existing:
                    bump(db, "duplicate_lead_rows_merged")
                    merged = json.loads(existing["fields_json"])
                    for field in fields:
                        value = parsed.get(field["name"])
                        if value is not None:
                            merged[field["name"]] = encrypt_pii(db, value) if field.get("pii") else value
                    db.execute("UPDATE leads SET fields_json=? WHERE id=?", (json.dumps(merged, sort_keys=True), existing["id"]))
                else:
                    db.execute("INSERT INTO leads(workflow_id,phone_hash,batch_id,fields_json) VALUES(?,?,?,?)", (workflow, key, path.stem, json.dumps(stored, sort_keys=True)))


def ingest_events(db: sqlite3.Connection, drop: Path, dispositions: dict[tuple[str, str], dict[str, bool]]) -> None:
    for path in sorted((drop / "events").glob("*.jsonl"), key=lambda item: item.name):
        with path.open(encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                bump(db, "events_total")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    bump(db, "malformed_events")
                    continue
                event_id = event.get("event_id") if isinstance(event, dict) else None
                if not event_id:
                    bump(db, "malformed_events")
                    continue
                if db.execute("SELECT 1 FROM events WHERE event_id=?", (str(event_id),)).fetchone():
                    bump(db, "duplicate_events_dropped")
                    continue
                workflow = str(event.get("workflow_id", ""))
                event_type = str(event.get("event_type", ""))
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                code = payload.get("disposition_code")
                ignored = event_type.startswith("disposition.") and (workflow, str(code)) not in dispositions
                if ignored:
                    bump(db, "unknown_disposition_events")
                try:
                    stamp = parse_timestamp(str(event.get("occurred_at", ""))).isoformat()
                except (ValueError, TypeError):
                    # Keep malformed timestamps out of reporting while preserving event dedupe.
                    stamp = "1970-01-01T00:00:00+05:30"
                    ignored = True
                phone = normalize_phone(str(event.get("phone", ""))) if event.get("phone") else None
                db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)", (
                    str(event_id), workflow, event_type, str(event["call_id"]) if event.get("call_id") is not None else None,
                    stamp, phone_hash(db, phone) if phone else None, json.dumps(payload, sort_keys=True), int(ignored)))


def ingest(db: sqlite3.Connection, drop: Path, schemas: dict[str, dict[str, Any]], dispositions: dict[tuple[str, str], dict[str, bool]]) -> None:
    resolved = str(drop.resolve())
    if db.execute("SELECT 1 FROM ingest_drops WHERE path=?", (resolved,)).fetchone():
        return
    with db:
        ingest_leads(db, drop, schemas)
        ingest_events(db, drop, dispositions)
        db.execute("INSERT INTO ingest_drops(path) VALUES(?)", (resolved,))


def quality_report(db: sqlite3.Connection) -> dict[str, Any]:
    q = {r["name"]: r["value"] for r in db.execute("SELECT name,value FROM quality")}
    reasons = {r["reason"]: r["count"] for r in db.execute("SELECT reason,COUNT(*) AS count FROM rejections GROUP BY reason")}
    expected = {"invalid_phone": 0, "missing_required:customer_name": 0}
    expected.update(reasons)
    return {
        "lead_rows_total": q.get("lead_rows_total", 0), "lead_rows_rejected": q.get("lead_rows_rejected", 0),
        "rejections_by_reason": dict(sorted(expected.items())), "duplicate_lead_rows_merged": q.get("duplicate_lead_rows_merged", 0),
        "field_warnings": q.get("field_warnings", 0), "events_total": q.get("events_total", 0),
        "malformed_events": q.get("malformed_events", 0), "duplicate_events_dropped": q.get("duplicate_events_dropped", 0),
        "orphan_events": 0, "unknown_disposition_events": q.get("unknown_disposition_events", 0),
    }


def report(db: sqlite3.Connection, out: Path, schemas: dict[str, dict[str, Any]], dispositions: dict[tuple[str, str], dict[str, bool]]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    lead_rows = db.execute("SELECT * FROM leads ORDER BY workflow_id,batch_id").fetchall()
    events = [dict(row) for row in db.execute("SELECT * FROM events WHERE ignored=0 ORDER BY occurred_at,event_id")]
    # Late-arriving leads relink earlier orphans on every report run.
    by_phone = {(r["workflow_id"], r["phone_hash"]): r for r in lead_rows}
    by_call: dict[tuple[str, str], int] = {}
    for event in events:
        lead = by_phone.get((event["workflow_id"], event["phone_hash"])) if event["phone_hash"] else None
        if lead is not None and event["call_id"]:
            by_call[(event["workflow_id"], event["call_id"])] = lead["id"]
    linked: list[tuple[dict[str, Any], int]] = []
    for event in events:
        lead = by_phone.get((event["workflow_id"], event["phone_hash"])) if event["phone_hash"] else None
        if event["event_type"] == "disposition.override" and not lead and event["call_id"]:
            call_owner = by_call.get((event["workflow_id"], event["call_id"]))
            if call_owner is not None:
                lead = next(row for row in lead_rows if row["id"] == call_owner)
        if lead is None:
            continue
        linked.append((event, lead["id"]))
        if event["call_id"]:
            by_call[(event["workflow_id"], event["call_id"])] = lead["id"]
    orphan_count = len(events) - len(linked)
    quality = quality_report(db)
    quality["orphan_events"] = orphan_count
    with (out / "data_quality.json").open("w", encoding="utf-8", newline="\n") as file:
        json.dump(quality, file, indent=2, sort_keys=False)
        file.write("\n")

    event_by_lead: dict[int, list[dict[str, Any]]] = {}
    for event, lead_id in linked:
        event_by_lead.setdefault(lead_id, []).append(event)
    call_maps: dict[int, dict[str, dict[str, Any]]] = {}
    for lead_id, lead_events in event_by_lead.items():
        calls: dict[str, dict[str, Any]] = {}
        for event in lead_events:
            if event["event_type"] != "call.initiated" or not event["call_id"]:
                continue
            calls.setdefault(event["call_id"], {"started": event, "connected": False, "telephony_failed": False, "sets": [], "overrides": []})
        for event in lead_events:
            call = calls.get(event["call_id"])
            if call is None:
                continue
            call["connected"] |= event["event_type"] == "call.connected"
            call["telephony_failed"] |= event["event_type"] == "call.telephony_failed"
            if event["event_type"] == "disposition.set":
                call["sets"].append(event)
            if event["event_type"] == "disposition.override":
                call["overrides"].append(event)
        for call in calls.values():
            candidates = call["overrides"] or call["sets"]
            call["disposition"] = max(candidates, key=lambda item: (item["occurred_at"], item["event_id"])) if candidates else None
        call_maps[lead_id] = calls

    def write_csv(name: str, header: list[str], rows: list[list[Any]]) -> None:
        with (out / name).open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)

    bucket_rows = []
    lead_out = []
    batch_rows = []
    daily: dict[tuple[str, str], dict[str, Any]] = {}
    for lead in lead_rows:
        fields = json.loads(lead["fields_json"])
        for field in schemas[lead["workflow_id"]]["fields"]:
            if field.get("pii") and fields.get(field["name"]):
                fields[field["name"]] = _CIPHERS[id(db)].decrypt(fields[field["name"]].encode()).decode()
        schema = schemas[lead["workflow_id"]]
        fdefs = {f["name"]: f for f in schema["fields"]}
        calls = call_maps.get(lead["id"], {})
        terminal = any(call["disposition"] and dispositions.get((lead["workflow_id"], json.loads(call["disposition"]["payload_json"]).get("disposition_code", "")), {}).get("is_terminal") for call in calls.values())
        if not calls:
            bucket = "yet_to_reach_out"
        elif terminal:
            bucket = "completed"
        elif all(call["telephony_failed"] for call in calls.values()):
            bucket = "in_progress_telephony_failed"
        else:
            bucket = "in_progress_dialed"
        batch_rows.append((lead["workflow_id"], lead["batch_id"], bucket))
        ordered_calls = sorted(calls.values(), key=lambda c: (c["started"]["occurred_at"], c["started"]["event_id"]))
        connected = sum(call["connected"] for call in calls.values())
        latest = ordered_calls[-1]["started"] if ordered_calls else None
        current = next((call["disposition"] for call in reversed(ordered_calls) if call["disposition"]), None)
        ever_success = False
        for call in calls.values():
            disp = call["disposition"]
            if disp:
                code = json.loads(disp["payload_json"]).get("disposition_code", "")
                ever_success |= dispositions.get((lead["workflow_id"], code), {}).get("is_success", False)
            day = parse_timestamp(call["started"]["occurred_at"]).date().isoformat()
            key = (lead["workflow_id"], day)
            daily.setdefault(key, {"total": 0, "connected": 0, "success_ids": set()})
            daily[key]["total"] += 1
            daily[key]["connected"] += int(call["connected"])
            if disp:
                code = json.loads(disp["payload_json"]).get("disposition_code", "")
                if dispositions.get((lead["workflow_id"], code), {}).get("is_success", False):
                    daily[key]["success_ids"].add(lead["id"])
        phone = fields["phone"]
        ref_name = next(f["name"] for f in schema["fields"] if f.get("reference"))
        ref = fields.get(ref_name, "")
        masked = "+91******" + phone[-4:]
        current_code = json.loads(current["payload_json"]).get("disposition_code", "") if current else ""
        lead_out.append([lead["workflow_id"], ref, lead["batch_id"], masked, bucket, len(calls), connected,
                         latest and parse_timestamp(latest["occurred_at"]).isoformat() or "", current_code, str(bool(ever_success)).lower()])
    # Aggregate batch buckets after every lead has been classified.
    batches: dict[tuple[str, str], dict[str, int]] = {}
    bucket_names = ["yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]
    for workflow, batch, bucket in batch_rows:
        counts = batches.setdefault((workflow, batch), {name: 0 for name in bucket_names})
        counts[bucket] += 1
    write_csv("batch_summary.csv", ["workflow_id", "batch_id", "total_leads", *bucket_names], [
        [workflow, batch, sum(counts.values()), *(counts[name] for name in bucket_names)]
        for (workflow, batch), counts in sorted(batches.items())])
    write_csv("daily_overview.csv", ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"], [
        [workflow, day, values["total"], values["connected"], f"{values['connected']/values['total']:.4f}", len(values["success_ids"])]
        for (workflow, day), values in sorted(daily.items())])
    lead_out.sort(key=lambda row: (row[0], row[1]))
    write_csv("lead_report.csv", ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls", "connected_calls", "last_call_at", "current_disposition", "ever_success"], lead_out)
    write_csv("rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"], [list(row) for row in db.execute("SELECT workflow_id,batch_id,row_number,reason FROM rejections ORDER BY workflow_id,batch_id,row_number")])


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest lead and event drops and produce reports.")
    parser.add_argument("--db", type=Path, default=Path("ledger.sqlite3"), help="SQLite database path (and adjacent key file)")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest_parser = commands.add_parser("ingest")
    ingest_parser.add_argument("drop", type=Path)
    report_parser = commands.add_parser("report")
    report_parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    schemas, dispositions = load_config()
    db = connect(args.db)
    if args.command == "ingest":
        ingest(db, args.drop, schemas, dispositions)
    else:
        report(db, args.out, schemas, dispositions)
    db.close()


if __name__ == "__main__":
    main()
