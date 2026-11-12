from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import boto3

ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
D1_DATABASE_ID = os.getenv("CLOUDFLARE_D1_DATABASE_ID", "").strip()
D1_TOKEN = os.getenv("CLOUDFLARE_D1_API_TOKEN", "").strip()
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", ACCOUNT_ID).strip()
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "").strip()


def d1_configured() -> bool:
    return all((ACCOUNT_ID, D1_DATABASE_ID, D1_TOKEN))


def r2_client():
    if not all((R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME)):
        return None
    return boto3.client(
        "s3",
        endpoint_url="https://" + R2_ACCOUNT_ID + ".r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
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
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode())
    if not payload.get("success"):
        raise RuntimeError("Cloudflare D1 rejected a database operation")
    result = payload.get("result") or []
    return result[0].get("results", []) if result else []


def init_storage() -> None:
    r2_values = (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME)
    r2_ready = all(r2_values)
    if any((R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME)) and not d1_configured():
        raise RuntimeError("R2 is configured without D1; configure both so media and metadata remain recoverable")
    if d1_configured() and not r2_ready:
        raise RuntimeError("D1 is configured but R2 is incomplete; configure both to enable durable media and metadata storage")
    if d1_configured():
        d1("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data_json TEXT NOT NULL)")
        d1("CREATE TABLE IF NOT EXISTS clips (id TEXT PRIMARY KEY, data_json TEXT NOT NULL)")


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
    client = r2_client()
    if client is None:
        return str(local_path)
    client.upload_file(str(local_path), R2_BUCKET_NAME, key)
    return "r2://" + key


def get_media(location: str, destination: str | Path) -> Path:
    if not location.startswith("r2://"):
        return Path(location)
    client = r2_client()
    if client is None:
        raise RuntimeError("R2 storage is not configured")
    key = location[len("r2://"):]
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    client.download_file(R2_BUCKET_NAME, key, str(target))
    return target
