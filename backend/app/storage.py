from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import boto3

ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
D1_DATABASE_ID = os.getenv("CLOUDFLARE_D1_DATABASE_ID", "").strip()
D1_TOKEN = os.getenv("CLOUDFLARE_D1_API_TOKEN", "").strip()
B2_ENDPOINT = os.getenv("B2_ENDPOINT", "https://s3.us-west-004.backblazeb2.com").strip()
B2_KEY_ID = os.getenv("B2_KEY_ID", "").strip()
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY", "").strip()
B2_BUCKET_NAME = os.getenv("B2_BUCKET_NAME", "").strip()


def d1_configured() -> bool:
    return all((ACCOUNT_ID, D1_DATABASE_ID, D1_TOKEN))


def b2_client():
    if not all((B2_ENDPOINT, B2_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME)):
        return None
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APPLICATION_KEY,
        region_name=os.getenv("B2_REGION", "us-west-004").strip(),
        config=Config(s3={"addressing_style": "path"}),
    )


def d1(sql: str, params: list[str] | None = None) -> list[dict]:
    if not d1_configured():
        return []
    url = ("https://api.cloudflare.com/client/v4/accounts/" + ACCOUNT_ID
           + "/d1/database/" + D1_DATABASE_ID + "/query")
    body = json.dumps({"sql": sql, "params": params or []}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Authorization": "Bearer " + D1_TOKEN,
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1200]
        raise RuntimeError(f"Cloudflare D1 API error ({exc.code}): {detail}") from exc
    if not payload.get("success"):
        raise RuntimeError("Cloudflare D1 rejected a database operation")
    result = payload.get("result") or []
    return result[0].get("results", []) if result else []


def init_storage() -> None:
    b2_values = (B2_ENDPOINT, B2_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME)
    b2_ready = all(b2_values)
    if any((B2_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME)) and not d1_configured():
        raise RuntimeError("Object storage is configured without D1; configure both so media and metadata remain recoverable")
    if d1_configured() and not b2_ready:
        raise RuntimeError("D1 is configured but Backblaze B2 object storage is incomplete; configure both to enable durable media and metadata storage")
    if d1_configured():
        d1("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data_json TEXT NOT NULL)")
        d1("CREATE TABLE IF NOT EXISTS clips (id TEXT PRIMARY KEY, data_json TEXT NOT NULL)")
        d1("CREATE TABLE IF NOT EXISTS workspace_activity (id TEXT PRIMARY KEY, last_activity INTEGER NOT NULL)")


def load_state() -> tuple[dict[str, dict], dict[str, dict]]:
    if not d1_configured():
        return {}, {}
    jobs = {row["id"]: json.loads(row["data_json"]) for row in d1("SELECT id, data_json FROM jobs")}
    clips = {row["id"]: json.loads(row["data_json"]) for row in d1("SELECT id, data_json FROM clips")}
    return jobs, clips


def save_job(job: dict) -> None:
    if d1_configured():
        d1("INSERT INTO jobs (id, data_json) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json",
           [str(job["id"]), json.dumps(job, separators=(",", ":"))])


def save_clip(clip: dict) -> None:
    if d1_configured():
        d1("INSERT INTO clips (id, data_json) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json",
           [str(clip["id"]), json.dumps(clip, separators=(",", ":"))])


def put_media(local_path: str | Path, key: str) -> str:
    client = b2_client()
    if client is None:
        return str(local_path)
    client.upload_file(str(local_path), B2_BUCKET_NAME, key)
    return "b2://" + key


def get_media(location: str, destination: str | Path) -> Path:
    if not location.startswith("b2://"):
        return Path(location)
    client = b2_client()
    if client is None:
        raise RuntimeError("Backblaze B2 object storage is not configured")
    key = location[len("b2://"):]
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    client.download_file(B2_BUCKET_NAME, key, str(target))
    return target


def reset_persistent_data() -> dict:
    """Permanently delete every version under ClipForge media prefixes and clear D1 tables."""
    client = b2_client()
    if client is None or not d1_configured():
        raise RuntimeError("Both Backblaze B2 and Cloudflare D1 must be configured before reset")

    deleted = {"uploads": 0, "renders": 0}
    paginator = client.get_paginator("list_object_versions")
    for prefix in ("uploads/", "renders/"):
        for page in paginator.paginate(Bucket=B2_BUCKET_NAME, Prefix=prefix):
            entries = []
            for item in page.get("Versions", []):
                entries.append({"Key": item["Key"], "VersionId": item["VersionId"]})
            for item in page.get("DeleteMarkers", []):
                entries.append({"Key": item["Key"], "VersionId": item["VersionId"]})
            for offset in range(0, len(entries), 1000):
                batch = entries[offset:offset + 1000]
                if not batch:
                    continue
                response = client.delete_objects(
                    Bucket=B2_BUCKET_NAME,
                    Delete={"Objects": batch, "Quiet": True},
                )
                if response.get("Errors"):
                    raise RuntimeError(f"Backblaze could not permanently delete every version under {prefix}")
                deleted["uploads" if prefix == "uploads/" else "renders"] += len(batch)

    d1("DELETE FROM jobs")
    d1("DELETE FROM clips")
    return deleted


def get_last_activity() -> int | None:
    if not d1_configured():
        return None
    rows = d1("SELECT last_activity FROM workspace_activity WHERE id = 'global'")
    return int(rows[0]["last_activity"]) if rows else None


def touch_activity(timestamp: int) -> None:
    if d1_configured():
        d1("INSERT INTO workspace_activity (id, last_activity) VALUES ('global', ?) ON CONFLICT(id) DO UPDATE SET last_activity=excluded.last_activity", [str(timestamp)])
