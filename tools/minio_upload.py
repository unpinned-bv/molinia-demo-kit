#!/usr/bin/env python3
"""Upload the engagement's export tree to the MinIO bucket Molinia ingests from.

Every local file exports/<path> is uploaded to the key <path>, e.g. with
export_prefix `acme-snowflake-export` (engagement.yml):
  exports/acme-snowflake-export/raw/orders.parquet
    -> s3://<MINIO_BUCKET>/acme-snowflake-export/raw/orders.parquet

Needs .secrets/minio.env (MINIO_ENDPOINT, MINIO_BUCKET, MINIO_ACCESS_KEY,
MINIO_SECRET_KEY). Without it the tool prints the manual console steps and
exits 2. Path-style addressing (the bucket-subdomain endpoint has no valid TLS).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _env  # noqa: E402
from _engagement import ENGAGEMENT  # noqa: E402
from _molinia_client import format_table  # noqa: E402

EXPORTS_DIR = _env.EXPORTS_DIR
EXPORT_PREFIX = ENGAGEMENT.export_prefix
MINIO_VARS = ("MINIO_ENDPOINT", "MINIO_BUCKET", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY")
# No infrastructure is baked in: the endpoint and bucket come from .secrets/minio.env.
DEFAULT_ENDPOINT = "<MINIO_ENDPOINT>"
DEFAULT_BUCKET = "<MINIO_BUCKET>"


def plan_uploads(exports_dir: Path = EXPORTS_DIR) -> List[Tuple[Path, str, int]]:
    """[(local file, object key, size)] for everything under exports/<export_prefix>/."""
    root = exports_dir / EXPORT_PREFIX
    out: List[Tuple[Path, str, int]] = []
    if not root.is_dir():
        return out
    for f in sorted(root.rglob("*")):
        if not f.is_file() or any(part.startswith(".") for part in f.relative_to(exports_dir).parts):
            continue
        key = f.relative_to(exports_dir).as_posix()
        out.append((f, key, f.stat().st_size))
    return out


def manual_instructions(reason: str) -> str:
    endpoint = _env.optional("MINIO_ENDPOINT", DEFAULT_ENDPOINT)
    bucket = _env.optional("MINIO_BUCKET", DEFAULT_BUCKET)
    return f"""{reason}

Upload by hand instead (MinIO console):
  1. Open the MinIO console for {endpoint} and sign in.
  2. Open bucket '{bucket}'.
  3. Upload the local folder
       {EXPORTS_DIR / EXPORT_PREFIX}/
     so the objects land at
       {EXPORT_PREFIX}/raw/<table>.parquet
       {EXPORT_PREFIX}/expected/<model>.parquet
     (drag the whole '{EXPORT_PREFIX}' folder onto the bucket root; keep the names).
  4. Continue with: make molinia-ingest

Or automate it: copy .secrets/minio.env.example to .secrets/minio.env, fill in
MINIO_ACCESS_KEY / MINIO_SECRET_KEY, and run this tool again."""


def make_client(v):
    import boto3
    from botocore.config import Config

    cfg = Config(
        s3={"addressing_style": "path"},
        signature_version="s3v4",
        retries={"max_attempts": 5, "mode": "standard"},
        # Newer botocore adds CRC32 checksums by default; older MinIO builds reject
        # them. Only send checksums when an operation requires one.
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    return boto3.client(
        "s3",
        endpoint_url=v["MINIO_ENDPOINT"],
        aws_access_key_id=v["MINIO_ACCESS_KEY"],
        aws_secret_access_key=v["MINIO_SECRET_KEY"],
        region_name=_env.optional("MINIO_REGION", "us-east-1"),
        config=cfg,
    )


def main(argv: Optional[Sequence[str]] = None, secrets_dir: Optional[Path] = None,
         exports_dir: Optional[Path] = None, client_factory=None) -> int:
    p = argparse.ArgumentParser(
        prog="tools/minio_upload.py",
        description=f"Upload exports/{EXPORT_PREFIX}/** to the MinIO bucket under the same keys "
                    "(needs .secrets/minio.env; otherwise prints the manual console steps and exits 2).",
    )
    p.add_argument("--dry-run", action="store_true", help="list what would be uploaded; no network calls")
    args = p.parse_args(argv)

    exports = exports_dir if exports_dir is not None else EXPORTS_DIR
    plan = plan_uploads(exports)
    if not plan:
        print(f"error: nothing to upload under {exports / EXPORT_PREFIX} (run `make sf-unload` first)",
              file=sys.stderr)
        return 1

    has_env = _env.has_file("minio.env", secrets_dir)
    if has_env:
        _env.load_env(secrets_dir)
    bucket = _env.optional("MINIO_BUCKET", DEFAULT_BUCKET)
    rows = [[key, f"{size / 1024:,.1f}"] for _, key, size in plan]
    total = sum(s for _, _, s in plan)
    print(f"{len(plan)} files, {total / 1024 / 1024:.2f} MB -> s3://{bucket}/ (path-style)")
    print(format_table(["key", "KB"], rows, width=90, align_right=[False, True]))

    if not has_env:
        print(manual_instructions("\n.secrets/minio.env not found, so nothing was uploaded."))
        return 2
    try:
        v = _env.require(*MINIO_VARS, secrets_dir=secrets_dir)
    except _env.EnvError as exc:
        print(manual_instructions(f"\n{exc}"))
        return 2

    if args.dry_run:
        print(f"dry run: would upload to {v['MINIO_ENDPOINT']} bucket {v['MINIO_BUCKET']}; nothing sent")
        return 0

    s3 = (client_factory or make_client)(v)
    failures = 0
    for f, key, size in plan:
        try:
            s3.upload_file(str(f), v["MINIO_BUCKET"], key,
                           ExtraArgs={"ContentType": "application/vnd.apache.parquet"
                                      if f.suffix == ".parquet" else "application/octet-stream"})
            head = s3.head_object(Bucket=v["MINIO_BUCKET"], Key=key)
            remote = int(head.get("ContentLength", -1))
            ok = remote == size
            print(f"{'ok  ' if ok else 'SIZE'} {key}  ({size:,} bytes{'' if ok else f', remote {remote:,}'})")
            failures += 0 if ok else 1
        except Exception as exc:  # botocore ClientError, EndpointConnectionError, ...
            failures += 1
            code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
            detail = " ".join(str(exc).split())[:200]
            print(f"FAIL {key}: {code or type(exc).__name__}: {detail}")
    print(f"{len(plan) - failures}/{len(plan)} uploaded to s3://{v['MINIO_BUCKET']}/{EXPORT_PREFIX}/")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
