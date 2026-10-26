"""Daily backup of Haazir to Google Drive (Phase 6, item 23 of reports/Library storage plan.md).

  database   every table of the public schema (plus the scheduler's jobs), each as gzip'd CSV, in one file
             backups/daily/<date>.tar; on the 1st of a month also backups/monthly/<month>.tar.
             Kept: 30 daily, 12 monthly.
  files      the uploaded files (court orders, papers) in Supabase storage, copied once each to backups/files/…
             (new ones only; a manifest records what is copied)

The tables' shape is in supabase/migrations: to restore, run the migrations on an empty project, then load each
CSV with COPY … FROM in the order the manifest lists them.

    python -m haazir.backup run         # back up now
    python -m haazir.backup check       # read the newest backup back and compare its row counts with the database
"""

import csv
import gzip
import io
import json
import sys
import tarfile
from datetime import date, datetime, timezone

import httpx
import psycopg

from . import config
from .storage import Drive

KEEP_DAILY, KEEP_MONTHLY = 30, 12
EXTRA = ["cron.job"]  # the scheduler's jobs live outside public


def tables(conn) -> list[str]:
    rows = conn.execute("select table_name from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE' order by 1").fetchall()
    return [f"public.{r[0]}" for r in rows] + EXTRA


def dump(conn) -> tuple[bytes, dict]:
    """All tables as gzip'd CSVs in one tar, and a manifest (rows and columns per table)."""
    buf = io.BytesIO()
    manifest = {"made_at": datetime.now(timezone.utc).isoformat(), "server": conn.execute("show server_version").fetchone()[0], "tables": []}
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for t in tables(conn):
            out = io.BytesIO()
            with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6) as gz:
                with conn.cursor().copy(f"COPY (SELECT * FROM {t}) TO STDOUT WITH (FORMAT csv, HEADER true)") as cp:
                    for chunk in cp:
                        gz.write(chunk)
            data = out.getvalue()
            rows = conn.execute(f"select count(*) from {t}").fetchone()[0]
            cols = [r[0] for r in conn.execute("select column_name from information_schema.columns where table_schema = %s and table_name = %s order by ordinal_position",
                                               t.split(".", 1)).fetchall()]
            info = tarfile.TarInfo(f"{t}.csv.gz"); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            manifest["tables"].append({"table": t, "rows": rows, "columns": cols, "bytes": len(data)})
        m = json.dumps(manifest, indent=1).encode()
        info = tarfile.TarInfo("manifest.json"); info.size = len(m)
        tar.addfile(info, io.BytesIO(m))
    return buf.getvalue(), manifest


def keep_newest(drive: Drive, folder: str, n: int) -> int:
    files = sorted((f for f in drive.names(folder) if f["name"].endswith(".tar")), key=lambda f: f["name"], reverse=True)
    for f in files[n:]:
        drive.delete(f"{folder}/{f['name']}")
    return max(0, len(files) - n)


def copy_files(drive: Drive) -> int:
    """New uploads in Supabase storage, copied to backups/files/<bucket>/<path>; the manifest lists what is done."""
    done = json.loads((drive.get("backups/files/manifest.json") or b"{}").decode() or "{}")
    base = config.SUPABASE_URL.rstrip("/")
    head = {"apikey": config.SUPABASE_SECRET_KEY, "Authorization": f"Bearer {config.SUPABASE_SECRET_KEY}"}
    with psycopg.connect(config.env("SUPABASE_DB_URL")) as conn:
        objs = conn.execute("select bucket_id, name, coalesce(metadata->>'eTag', updated_at::text) from storage.objects order by created_at").fetchall()
    copied = 0
    with httpx.Client(timeout=120) as http:
        for bucket, name, tag in objs:
            key = f"{bucket}/{name}"
            if done.get(key) == tag:
                continue
            r = http.get(f"{base}/storage/v1/object/{bucket}/{name}", headers=head)
            if r.status_code != 200:
                continue
            drive.put(f"backups/files/{key}", r.content, r.headers.get("content-type", "application/octet-stream"))
            done[key] = tag
            copied += 1
            if copied % 200 == 0:  # keep progress if a run is cut short
                drive.put("backups/files/manifest.json", json.dumps(done).encode(), "application/json")
    drive.put("backups/files/manifest.json", json.dumps(done).encode(), "application/json")
    return copied


def run() -> int:
    drive = Drive()
    today = date.today()
    with psycopg.connect(config.env("SUPABASE_DB_URL")) as conn:
        data, manifest = dump(conn)
    drive.put(f"backups/daily/{today.isoformat()}.tar", data, "application/x-tar")
    if today.day == 1 or not drive.names("backups/monthly"):
        drive.put(f"backups/monthly/{today.strftime('%Y-%m')}.tar", data, "application/x-tar")
    removed = keep_newest(drive, "backups/daily", KEEP_DAILY) + keep_newest(drive, "backups/monthly", KEEP_MONTHLY)
    files = copy_files(drive)
    rows = sum(t["rows"] for t in manifest["tables"])
    print(f"backup: {len(manifest['tables'])} tables, {rows:,} rows, {len(data) / 1e6:.1f} MB · {files} new files copied · {removed} old backups removed", flush=True)
    return 0


def check() -> int:
    """The newest daily backup read back from Drive: every table there, and row counts close to the database's now."""
    drive = Drive()
    newest = sorted((f["name"] for f in drive.names("backups/daily") if f["name"].endswith(".tar")), reverse=True)
    if not newest:
        print("no backup yet"); return 1
    data = drive.get(f"backups/daily/{newest[0]}")
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        manifest = json.load(tar.extractfile("manifest.json"))
        bad = []
        with psycopg.connect(config.env("SUPABASE_DB_URL")) as conn:
            for t in manifest["tables"]:
                rows = list(csv.reader(io.StringIO(gzip.decompress(tar.extractfile(f"{t['table']}.csv.gz").read()).decode())))
                if len(rows) - 1 != t["rows"]:
                    bad.append(f"{t['table']}: file has {len(rows) - 1}, manifest {t['rows']}")
                live = conn.execute(f"select count(*) from {t['table']}").fetchone()[0]
                if abs(live - t["rows"]) > max(50, live * 0.02):  # the database moves on after the backup
                    bad.append(f"{t['table']}: backup {t['rows']}, database now {live}")
    print(f"{newest[0]}: {len(manifest['tables'])} tables read back; " + ("all match" if not bad else "PROBLEMS: " + "; ".join(bad)))
    return 1 if bad else 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "run":
        return run()
    if cmd == "check":
        return check()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
