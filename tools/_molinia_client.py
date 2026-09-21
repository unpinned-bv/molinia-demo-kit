"""Minimal Molinia API client shared by tools/molinia.py and agent/parity.py.

Wire contract (read from the Molinia server code @ ee19594e):

  POST {API}/api/orgs/{org}/query/execute        body {"sql": "<one statement>"}
    200 -> {"queryId", "status": "success", "columns": [str], "columnTypeIds",
            "rows": [[...]], "rowCount", "durationMs", "profile", "warnings"?}
         (server/src/query/query.service.ts execute(); rows via getRowsJson, so
          DECIMAL arrives as a string, BIGINT/INTEGER/DOUBLE as numbers,
          DATE "2026-01-02", TIMESTAMP "2026-01-02T03:04:05.123456Z")
    4xx -> NestJS error body {"statusCode", "message": str | [str], "error"}
         (a failing statement is re-thrown as 400 with the engine message)

  POST {API}/api/orgs/{org}/datasources/{id}/ingest  body IngestDto
    {"targetTable", "filePath"?, "locationId"?, "fileFormat"?, "ingestMode"?}
    200 -> {"rowCount", "durationMs", "phases", "uri", "format", "ingestMode"}
         (server/src/connections/ingest.controller.ts / ingest.service.ts)

  Rate limits, both answered with HTTP 429:
    * @nestjs/throttler 6.5, 60 requests/min per client IP for the whole API
      (server/src/app.module.ts); sets a `Retry-After: <seconds>` header.
    * org-engine limit, 30 queries/min for a free-plan org
      (server/src/query/org-rate-limit.service.ts); NO Retry-After header, the
      body is {"code": "org_engine_rate_limit", "retryAfterSeconds": n, ...}.
      The daily budget uses the same status with code "org_engine_daily_budget";
      waiting a minute cannot help there, so it fails immediately.
    Both 429s are raised before the statement runs, so retrying is always safe.

This client paces itself (default 25 org-engine queries/min and 40 HTTP
requests/min) with a sliding window shared across processes through a small
state file in the system temp directory, so back-to-back tool invocations do
not each start with a fresh allowance. On 429 it waits for Retry-After, else the
body's retryAfterSeconds, else exponential backoff.

The API key is only ever placed in the Authorization header; it is never logged,
printed or included in an exception message.
"""
from __future__ import annotations

import contextlib
import email.utils
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _env  # noqa: E402
from _sql import is_read_only, split_statements  # noqa: E402

DEFAULT_QUERIES_PER_MIN = 25   # server: 30/min for a free-plan org
DEFAULT_REQUESTS_PER_MIN = 40  # server: 60/min per client IP (shared with the browser console)
WINDOW_SECONDS = 60.0


class MoliniaError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None,
                 body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body


class RateLimitedError(MoliniaError):
    pass


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- pacing

class Pacer:
    """Sliding-window limiter: at most `limits[kind]` acquisitions per `window`
    seconds. With `state_path` the window is shared by every process using the
    same file (flock-protected); without it, it is per process."""

    def __init__(self, limits: Dict[str, int], window: float = WINDOW_SECONDS,
                 state_path: Optional[Path] = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = _log) -> None:
        self.limits = dict(limits)
        self.window = window
        self.state_path = state_path
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self._mem: Dict[str, List[float]] = {}

    @contextlib.contextmanager
    def _state(self) -> Iterator[Dict[str, List[float]]]:
        if self.state_path is None:
            yield self._mem
            return
        try:
            import fcntl
            fh = open(self.state_path, "a+", encoding="utf-8")
        except Exception:  # unwritable temp dir etc.: degrade to per-process pacing
            self.state_path = None
            yield self._mem
            return
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0)
            try:
                state = json.loads(fh.read() or "{}")
                if not isinstance(state, dict):
                    state = {}
            except ValueError:
                state = {}
            yield state
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(state))
            fh.flush()
        finally:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()

    def acquire(self, kind: str) -> float:
        """Block until a slot of `kind` is free, take it, return seconds waited."""
        limit = self.limits.get(kind)
        if not limit:
            return 0.0
        waited = 0.0
        announced = False
        while True:
            with self._state() as state:
                now = self.clock()
                stamps = sorted(t for t in state.get(kind, [])
                                if isinstance(t, (int, float)) and 0 <= now - t < self.window)
                if len(stamps) < limit:
                    stamps.append(now)
                    state[kind] = stamps
                    return waited
                state[kind] = stamps
                wait = max(0.05, stamps[0] + self.window - now + 0.05)
            if not announced:
                self.log(f"[pace] {kind}: {limit}/min reached, waiting {wait:.1f}s")
                announced = True
            self.sleep(wait)
            waited += wait


def shared_pacer(api_url: str, org_id: str,
                 queries_per_min: int = DEFAULT_QUERIES_PER_MIN,
                 requests_per_min: int = DEFAULT_REQUESTS_PER_MIN) -> Pacer:
    digest = hashlib.sha256(f"{api_url}|{org_id}".encode()).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / f"molinia-demo-kit-pace-{digest}.json"
    return Pacer({"query": queries_per_min, "http": requests_per_min}, state_path=path)


# --------------------------------------------------------------------------- client

def _retry_after_seconds(headers: Any, body: Any, now: Optional[float] = None) -> Optional[float]:
    value = None
    if headers is not None:
        try:
            value = headers.get("Retry-After") or headers.get("retry-after")
        except Exception:
            value = None
    if value:
        value = str(value).strip()
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                dt = email.utils.parsedate_to_datetime(value)
                ref = time.time() if now is None else now
                return max(0.0, dt.timestamp() - ref)
            except Exception:
                pass
    if isinstance(body, dict):
        v = body.get("retryAfterSeconds")
        if isinstance(v, (int, float)) and v >= 0:
            return float(v)
    return None


def _error_message(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        msg = body.get("message")
        if isinstance(msg, list):
            return "; ".join(str(m) for m in msg)
        if msg:
            return str(msg)
        if body.get("error"):
            return str(body["error"])
    return fallback


class MoliniaClient:
    def __init__(self, api_url: str, org_id: str, api_key: str, *,
                 session: Any = None, pacer: Optional[Pacer] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 max_attempts: int = 8, retry_cushion: float = 1.0,
                 max_wait: float = 300.0, log: Callable[[str], None] = _log) -> None:
        self.api_url = api_url.rstrip("/")
        if self.api_url.endswith("/api"):
            self.api_url = self.api_url[: -len("/api")]
        self.org_id = org_id
        self._api_key = api_key
        if session is None:
            import requests
            session = requests.Session()
        self.session = session
        self.pacer = pacer if pacer is not None else shared_pacer(self.api_url, org_id)
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.retry_cushion = retry_cushion
        self.max_wait = max_wait
        self.log = log

    def __repr__(self) -> str:  # never show the key
        return f"MoliniaClient(api_url={self.api_url!r}, org_id={self.org_id!r})"

    @classmethod
    def from_env(cls, readonly: bool = False, secrets_dir: Optional[Path] = None, **kw: Any) -> "MoliniaClient":
        _env.load_env(secrets_dir)
        key_var = "MOLINIA_READONLY_KEY" if readonly else "MOLINIA_API_KEY"
        v = _env.require("MOLINIA_API_URL", "MOLINIA_ORG_ID", key_var, secrets_dir=secrets_dir)
        return cls(v["MOLINIA_API_URL"], v["MOLINIA_ORG_ID"], v[key_var], **kw)

    # -- low level -----------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "molinia-demo-kit/1.0",
        }

    @staticmethod
    def _json(resp: Any) -> Any:
        try:
            return resp.json()
        except Exception:
            return None

    def request(self, method: str, path: str, body: Optional[dict] = None, *,
                engine_query: bool = False, idempotent: bool = False,
                timeout: Sequence[float] = (15.0, 300.0)) -> Any:
        url = f"{self.api_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            if engine_query:
                self.pacer.acquire("query")
            self.pacer.acquire("http")
            try:
                resp = self.session.request(method, url, json=body, headers=self._headers(),
                                            timeout=tuple(timeout))
            except Exception as exc:  # requests.RequestException and friends
                if idempotent and attempt < self.max_attempts:
                    wait = self._backoff(attempt)
                    self.log(f"[retry] {type(exc).__name__} on {method} {path}; retrying in {wait:.0f}s")
                    self.sleep(wait)
                    continue
                raise MoliniaError(f"{method} {path} failed: {type(exc).__name__}: {exc}") from None

            status = int(resp.status_code)
            data = self._json(resp)

            if status == 429:
                code = data.get("code") if isinstance(data, dict) else None
                if code == "org_engine_daily_budget":
                    raise RateLimitedError(
                        "Molinia daily org-engine budget reached: " + _error_message(data, "HTTP 429"),
                        status=status, code=code, body=data)
                if attempt >= self.max_attempts:
                    raise RateLimitedError(
                        f"still rate limited after {attempt} attempts: " + _error_message(data, "HTTP 429"),
                        status=status, code=code, body=data)
                headers = getattr(resp, "headers", None)
                wait = _retry_after_seconds(headers, data)
                source = ("Retry-After" if _retry_after_seconds(headers, None) is not None
                          else "retryAfterSeconds" if wait is not None else "backoff")
                if wait is None:
                    wait = self._backoff(attempt)
                if wait > self.max_wait:
                    raise RateLimitedError(
                        f"server asked to wait {wait:.0f}s (> {self.max_wait:.0f}s); giving up",
                        status=status, code=code, body=data)
                wait += self.retry_cushion
                self.log(f"[429] {code or 'throttled'} on {method} {path}; waiting {wait:.1f}s "
                         f"({source}, attempt {attempt}/{self.max_attempts})")
                self.sleep(wait)
                continue

            if status in (502, 503, 504) and idempotent and attempt < self.max_attempts:
                wait = self._backoff(attempt)
                self.log(f"[retry] HTTP {status} on {method} {path}; retrying in {wait:.0f}s")
                self.sleep(wait)
                continue

            if 200 <= status < 300:
                return data

            code = data.get("code") if isinstance(data, dict) else None
            text = ""
            if data is None:
                try:
                    text = (resp.text or "")[:300]
                except Exception:
                    text = ""
            raise MoliniaError(f"HTTP {status}: " + _error_message(data, text or "no body"),
                               status=status, code=code, body=data)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return float(min(60, 2 ** attempt))

    # -- API -----------------------------------------------------------------

    def execute(self, sql: str, *, idempotent: Optional[bool] = None) -> Dict[str, Any]:
        """Run ONE statement on the org engine. Returns the server JSON."""
        stmts = split_statements(sql, dialect="duckdb")
        if len(stmts) != 1:
            raise ValueError(f"expected exactly one SQL statement, got {len(stmts)}")
        stmt = stmts[0]
        if idempotent is None:
            idempotent = is_read_only(stmt)
        data = self.request("POST", f"/api/orgs/{self.org_id}/query/execute", {"sql": stmt},
                            engine_query=True, idempotent=idempotent)
        if not isinstance(data, dict):
            raise MoliniaError("unexpected response from query/execute (not a JSON object)")
        return data

    def ingest(self, data_source_id: int, target_table: str, file_path: str, *,
               location_id: Optional[int] = None, file_format: Optional[str] = "parquet") -> Dict[str, Any]:
        body: Dict[str, Any] = {"targetTable": target_table, "filePath": file_path}
        if location_id is not None:
            body["locationId"] = int(location_id)
        if file_format:
            body["fileFormat"] = file_format
        data = self.request("POST", f"/api/orgs/{self.org_id}/datasources/{int(data_source_id)}/ingest",
                            body, engine_query=False, idempotent=False, timeout=(15.0, 900.0))
        if not isinstance(data, dict):
            raise MoliniaError("unexpected response from ingest (not a JSON object)")
        return data


# --------------------------------------------------------------------------- output helpers

def cell(v: Any, width: int = 60) -> str:
    if v is None:
        s = "NULL"
    elif isinstance(v, bool):
        s = "true" if v else "false"
    elif isinstance(v, (dict, list)):
        s = json.dumps(v, separators=(",", ":"))
    else:
        s = str(v)
    s = s.replace("\n", "\\n")
    return s if len(s) <= width else s[: width - 3] + "..."


def format_table(headers: Sequence[str], rows: Sequence[Sequence[Any]], width: int = 60,
                 align_right: Optional[Sequence[bool]] = None) -> str:
    h = [cell(x, width) for x in headers]
    body = [[cell(x, width) for x in r] for r in rows]
    widths = [len(x) for x in h]
    for r in body:
        for i, x in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(x))
            else:
                widths.append(len(x))
    right = list(align_right) if align_right is not None else [False] * len(widths)
    right += [False] * (len(widths) - len(right))

    def fmt(r: Sequence[str]) -> str:
        parts = []
        for i, w in enumerate(widths):
            x = r[i] if i < len(r) else ""
            parts.append(x.rjust(w) if right[i] else x.ljust(w))
        return "  ".join(parts).rstrip()

    lines = [fmt(h), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in body]
    return "\n".join(lines)


def to_int(v: Any) -> Optional[int]:
    """Counts arrive as numbers or (HUGEINT/DECIMAL) strings."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    return int(float(v)) if "." in str(v) or "e" in str(v).lower() else int(str(v))
