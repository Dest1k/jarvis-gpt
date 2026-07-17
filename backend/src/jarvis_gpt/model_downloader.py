"""Custom multithreaded, resumable HuggingFace model downloader.

Owner requirement (2026-07-17): write our own downloader — do NOT use huggingface_hub.
It downloads a repo's files in parallel AND splits each large file into byte-range
segments fetched concurrently, resumes cleanly (докачка) from an interrupted run,
verifies every file's SHA256 + size against the repo's LFS metadata, and renders honest
console progress (per-file + overall bytes, %, speed, ETA). The HF token is read from a
token file (``hf_token.txt`` at the project root) and never logged.

This is the console/startup downloader for the local-model migration; ``ModelHubManager``
stays the API/UI downloader. Network access goes through an injected ``httpx.Client`` so
the whole flow is unit-testable with ``httpx.MockTransport`` (no network).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

HF_ENDPOINT = "https://huggingface.co"
DEFAULT_SEGMENT_SIZE = 16 * 1024 * 1024  # 16 MiB byte-range segments for large files
DEFAULT_FILE_WORKERS = 4
DEFAULT_SEGMENT_WORKERS = 4
_HTTP_TIMEOUT = httpx.Timeout(connect=30.0, read=120.0, write=120.0, pool=30.0)


class ModelDownloadError(RuntimeError):
    """A download could not complete or failed an integrity check."""


@dataclass(frozen=True)
class RemoteFile:
    """One downloadable repo file with the metadata needed to verify it."""

    path: str
    size: int
    sha256: str | None = None  # LFS oid (hex) — present for the big weight shards
    lfs: bool = False


@dataclass
class FileOutcome:
    path: str
    size: int
    resumed_from: int = 0
    skipped: bool = False
    verified: bool = False


# --------------------------------------------------------------------------- token


def read_hf_token(token_path: str | Path | None) -> str | None:
    """Read the HF token from a file. Tolerates a ``KEY=VALUE`` line, surrounding quotes,
    comments and blank lines; prefers a line that looks like an ``hf_...`` token. Never
    logs the value."""

    if not token_path:
        return None
    path = Path(token_path)
    if not path.exists() or not path.is_file():
        return None
    candidates: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # A KEY=VALUE line yields the value; a bare token has no "=" (HF tokens never do).
        if "=" in line:
            line = line.split("=", 1)[1]
        line = line.strip().strip('"').strip("'").strip()
        token = line.split()[0] if line.split() else ""
        if token:
            candidates.append(token)
    for token in candidates:
        if token.startswith("hf_"):
            return token
    return candidates[0] if candidates else None


def _auth_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


# ----------------------------------------------------------------- repo file listing


def parse_tree_entries(entries: Iterable[dict]) -> list[RemoteFile]:
    """Turn HuggingFace ``/api/models/<repo>/tree`` entries into RemoteFile records.

    LFS files (the weight shards) carry ``lfs.oid`` (sha256) and ``lfs.size``; plain
    files carry a top-level ``size`` and a git-blob ``oid`` (sha1, not content sha256),
    so those are verified by size only.
    """

    files: list[RemoteFile] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "file":
            continue
        path = str(entry.get("path") or "").strip()
        if not path:
            continue
        lfs = entry.get("lfs") if isinstance(entry.get("lfs"), dict) else None
        if lfs:
            size = int(lfs.get("size") or entry.get("size") or 0)
            oid = str(lfs.get("oid") or "").strip().lower()
            sha = oid if len(oid) == 64 and all(c in "0123456789abcdef" for c in oid) else None
            files.append(RemoteFile(path=path, size=size, sha256=sha, lfs=True))
        else:
            files.append(RemoteFile(path=path, size=int(entry.get("size") or 0), lfs=False))
    return files


def list_repo_files(
    repo: str,
    *,
    revision: str = "main",
    token: str | None = None,
    client: httpx.Client,
) -> list[RemoteFile]:
    url = f"{HF_ENDPOINT}/api/models/{repo}/tree/{revision}"
    response = client.get(
        url, params={"recursive": "1"}, headers=_auth_headers(token), timeout=_HTTP_TIMEOUT
    )
    if response.status_code == 401:
        raise ModelDownloadError(f"HuggingFace rejected the token for {repo} (401).")
    if response.status_code == 404:
        raise ModelDownloadError(f"Repository or revision not found: {repo}@{revision}.")
    if response.status_code >= 400:
        raise ModelDownloadError(f"Listing {repo} failed: HTTP {response.status_code}.")
    payload = response.json()
    if not isinstance(payload, list):
        raise ModelDownloadError(f"Unexpected tree response for {repo}.")
    files = parse_tree_entries(payload)
    if not files:
        raise ModelDownloadError(f"No downloadable files found for {repo}@{revision}.")
    return files


def resolve_url(repo: str, path: str, *, revision: str = "main") -> str:
    return f"{HF_ENDPOINT}/{repo}/resolve/{revision}/{path}"


# -------------------------------------------------------------------- verification


def sha256_of(path: Path, *, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_file(path: Path, *, size: int, sha256: str | None) -> bool:
    """A file is valid when it exists, matches the expected size (when known) and, for
    LFS shards, matches the expected SHA256."""

    if not path.exists() or not path.is_file():
        return False
    if size and path.stat().st_size != size:
        return False
    if sha256:
        return sha256_of(path) == sha256.lower()
    return True


# ----------------------------------------------------------------------- progress


@dataclass
class _Progress:
    """Thread-safe aggregate byte counter driving the console readout."""

    total_bytes: int
    total_files: int
    _lock: threading.Lock = field(default_factory=threading.Lock)
    done_bytes: int = 0
    done_files: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def add_bytes(self, count: int) -> None:
        with self._lock:
            self.done_bytes += count

    def add_baseline(self, count: int) -> None:
        # Bytes already present on disk from a prior run (counted, not "downloaded now").
        with self._lock:
            self.done_bytes += count
            self.started_at = self.started_at  # keep speed based on this run's transfer

    def file_done(self) -> None:
        with self._lock:
            self.done_files += 1

    def render(self) -> str:
        with self._lock:
            done, total = self.done_bytes, self.total_bytes
            files, total_files = self.done_files, self.total_files
            elapsed = max(1e-6, time.monotonic() - self.started_at)
        pct = (done / total * 100.0) if total else 0.0
        speed = done / elapsed
        eta = (total - done) / speed if speed > 0 and total else 0.0
        return (
            f"[{pct:5.1f}%] {_human(done)}/{_human(total)} "
            f"files {files}/{total_files}  {_human(int(speed))}/s  ETA {_eta(eta)}"
        )


def _human(num: float) -> str:
    step = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if step < 1024 or unit == "TB":
            return f"{step:.1f}{unit}" if unit != "B" else f"{int(step)}B"
        step /= 1024
    return f"{step:.1f}TB"


def _eta(seconds: float) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "--:--"
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{sec:02d}" if hours else f"{minutes:02d}:{sec:02d}"


class _ConsoleReporter:
    """Prints the aggregate progress line on a throttled background thread."""

    def __init__(self, progress: _Progress, *, interval: float = 0.25, stream=sys.stderr):
        self._progress = progress
        self._interval = interval
        self._stream = stream
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jarvis-dl-progress", daemon=True)

    def __enter__(self) -> _ConsoleReporter:
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._write(self._progress.render() + "\n")

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._write("\r" + self._progress.render() + " " * 4)

    def _write(self, text: str) -> None:
        try:
            self._stream.write(text)
            self._stream.flush()
        except (OSError, ValueError):
            pass


# ------------------------------------------------------------------ file download


def _segment_meta_path(part: Path) -> Path:
    return part.with_suffix(part.suffix + ".json")


def _plan_segments(size: int, segment_size: int) -> list[tuple[int, int]]:
    """[(start, end_inclusive)] covering [0, size)."""

    if size <= 0:
        return []
    segments: list[tuple[int, int]] = []
    start = 0
    while start < size:
        end = min(start + segment_size, size) - 1
        segments.append((start, end))
        start = end + 1
    return segments


def _server_supports_range(client: httpx.Client, url: str, token: str | None) -> bool:
    try:
        response = client.get(
            url,
            headers={**_auth_headers(token), "Range": "bytes=0-0"},
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 206 or "accept-ranges" in {
        key.lower() for key in response.headers
    }


def download_file(
    client: httpx.Client,
    url: str,
    dest: Path,
    *,
    size: int,
    sha256: str | None,
    token: str | None = None,
    segment_size: int = DEFAULT_SEGMENT_SIZE,
    segment_workers: int = DEFAULT_SEGMENT_WORKERS,
    on_bytes: Callable[[int], None] | None = None,
) -> FileOutcome:
    """Download a single file: segmented + parallel when the size is known and the server
    honours Range, resumable via a ``.part`` file and a segment-completion sidecar, then
    verified (size + sha256) before the atomic rename to ``dest``."""

    dest.parent.mkdir(parents=True, exist_ok=True)
    if verify_file(dest, size=size, sha256=sha256):
        return FileOutcome(path=dest.name, size=dest.stat().st_size, skipped=True, verified=True)

    part = dest.with_suffix(dest.suffix + ".part")
    use_segments = bool(size) and _server_supports_range(client, url, token)
    if use_segments:
        resumed = _download_segmented(
            client, url, part, size=size, token=token, segment_size=segment_size,
            segment_workers=segment_workers, on_bytes=on_bytes,
        )
    else:
        resumed = _download_streaming(client, url, part, size=size, token=token, on_bytes=on_bytes)

    if size and part.stat().st_size != size:
        raise ModelDownloadError(
            f"{dest.name}: size mismatch after download "
            f"(got {part.stat().st_size}, expected {size})."
        )
    if sha256 and sha256_of(part) != sha256.lower():
        part.unlink(missing_ok=True)
        _segment_meta_path(part).unlink(missing_ok=True)
        raise ModelDownloadError(f"{dest.name}: SHA256 mismatch — re-download required.")
    os.replace(part, dest)
    _segment_meta_path(part).unlink(missing_ok=True)
    return FileOutcome(path=dest.name, size=size or dest.stat().st_size, resumed_from=resumed,
                       verified=True)


def _load_segment_meta(part: Path, *, size: int, segment_size: int) -> set[int]:
    meta_path = _segment_meta_path(part)
    if not part.exists() or not meta_path.exists():
        return set()
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if meta.get("size") != size or meta.get("segment_size") != segment_size:
        return set()
    if part.stat().st_size != size:
        return set()
    done = meta.get("done")
    return {int(i) for i in done} if isinstance(done, list) else set()


def _download_segmented(
    client: httpx.Client,
    url: str,
    part: Path,
    *,
    size: int,
    token: str | None,
    segment_size: int,
    segment_workers: int,
    on_bytes: Callable[[int], None] | None,
) -> int:
    from concurrent.futures import ThreadPoolExecutor

    segments = _plan_segments(size, segment_size)
    completed = _load_segment_meta(part, size=size, segment_size=segment_size)
    if not part.exists() or part.stat().st_size != size:
        # (Re)allocate the target file to full size so threads can seek+write in place.
        with part.open("wb") as handle:
            handle.truncate(size)
        completed = set()
    resumed_bytes = 0
    for index, (start, end) in enumerate(segments):
        if index in completed:
            resumed_bytes += end - start + 1
    if resumed_bytes and on_bytes:
        on_bytes(resumed_bytes)  # count already-present bytes toward the overall total

    meta_lock = threading.Lock()
    meta_path = _segment_meta_path(part)

    def persist() -> None:
        tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"size": size, "segment_size": segment_size, "done": sorted(completed)}),
            encoding="utf-8",
        )
        os.replace(tmp, meta_path)

    persist()

    def fetch(index: int, start: int, end: int) -> None:
        headers = {**_auth_headers(token), "Range": f"bytes={start}-{end}"}
        with client.stream(
            "GET", url, headers=headers, timeout=_HTTP_TIMEOUT, follow_redirects=True
        ) as response:
            if response.status_code not in (200, 206):
                raise ModelDownloadError(
                    f"segment {index} of {part.name}: HTTP {response.status_code}"
                )
            with part.open("rb+") as handle:
                handle.seek(start)
                for block in response.iter_bytes():
                    handle.write(block)
                    if on_bytes:
                        on_bytes(len(block))
        with meta_lock:
            completed.add(index)
            persist()

    pending = [(i, s, e) for i, (s, e) in enumerate(segments) if i not in completed]
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, segment_workers)) as pool:
            futures = [pool.submit(fetch, i, s, e) for i, s, e in pending]
            for future in futures:
                future.result()
    return resumed_bytes


def _download_streaming(
    client: httpx.Client,
    url: str,
    part: Path,
    *,
    size: int,
    token: str | None,
    on_bytes: Callable[[int], None] | None,
) -> int:
    resume_from = part.stat().st_size if part.exists() else 0
    if size and resume_from > size:
        resume_from = 0
    headers = _auth_headers(token)
    if resume_from:
        headers = {**headers, "Range": f"bytes={resume_from}-"}
    if resume_from and on_bytes:
        on_bytes(resume_from)
    mode = "ab" if resume_from else "wb"
    with client.stream(
        "GET", url, headers=headers, timeout=_HTTP_TIMEOUT, follow_redirects=True
    ) as response:
        if resume_from and response.status_code == 200:
            # Server ignored the Range — restart cleanly.
            resume_from = 0
            mode = "wb"
        elif response.status_code not in (200, 206):
            raise ModelDownloadError(f"{part.name}: HTTP {response.status_code}")
        with part.open(mode) as handle:
            for block in response.iter_bytes():
                handle.write(block)
                if on_bytes:
                    on_bytes(len(block))
    return resume_from


# ------------------------------------------------------------------ orchestration


def download_model(
    repo: str,
    dest_dir: str | Path,
    *,
    revision: str = "main",
    token: str | None = None,
    token_path: str | Path | None = None,
    file_workers: int = DEFAULT_FILE_WORKERS,
    segment_workers: int = DEFAULT_SEGMENT_WORKERS,
    segment_size: int = DEFAULT_SEGMENT_SIZE,
    console: bool = True,
    client: httpx.Client | None = None,
) -> dict:
    """Download every file of ``repo`` into ``dest_dir`` with parallel files + segments,
    resume and per-file SHA256/size verification. Returns a summary dict. Raises
    ModelDownloadError on any unrecoverable failure (so a caller can gate startup)."""

    from concurrent.futures import ThreadPoolExecutor

    if token is None:
        token = read_hf_token(token_path)
    dest_root = Path(dest_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    client = client or httpx.Client(follow_redirects=True, trust_env=False)
    try:
        files = list_repo_files(repo, revision=revision, token=token, client=client)
        total_bytes = sum(max(0, f.size) for f in files)
        progress = _Progress(total_bytes=total_bytes, total_files=len(files))
        outcomes: list[FileOutcome] = []
        outcomes_lock = threading.Lock()

        def worker(remote: RemoteFile) -> None:
            outcome = download_file(
                client, resolve_url(repo, remote.path, revision=revision),
                dest_root / remote.path, size=remote.size, sha256=remote.sha256, token=token,
                segment_size=segment_size, segment_workers=segment_workers,
                on_bytes=progress.add_bytes,
            )
            if outcome.skipped:
                progress.add_bytes(remote.size)
            progress.file_done()
            with outcomes_lock:
                outcomes.append(outcome)

        reporter = _ConsoleReporter(progress) if console else None
        ctx = reporter if reporter is not None else _nullcontext()
        with ctx, ThreadPoolExecutor(max_workers=max(1, file_workers)) as pool:
            futures = [pool.submit(worker, remote) for remote in files]
            for future in futures:
                future.result()
    finally:
        if owns_client:
            client.close()
    return {
        "repo": repo,
        "revision": revision,
        "dest": str(dest_root),
        "files": len(files),
        "bytes": total_bytes,
        "skipped": sum(1 for o in outcomes if o.skipped),
        "verified": sum(1 for o in outcomes if o.verified),
    }


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Custom multithreaded HF model downloader.")
    parser.add_argument("repo", help="HuggingFace repo id, e.g. unsloth/Qwen3.6-35B-A3B-NVFP4")
    parser.add_argument("dest", help="Destination directory for the checkpoint")
    parser.add_argument("--revision", default="main")
    parser.add_argument("--token-file", default="hf_token.txt")
    parser.add_argument("--file-workers", type=int, default=DEFAULT_FILE_WORKERS)
    parser.add_argument("--segment-workers", type=int, default=DEFAULT_SEGMENT_WORKERS)
    args = parser.parse_args(argv)
    try:
        summary = download_model(
            args.repo, args.dest, revision=args.revision, token_path=args.token_file,
            file_workers=args.file_workers, segment_workers=args.segment_workers, console=True,
        )
    except ModelDownloadError as exc:
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Done: {summary['files']} files, {_human(summary['bytes'])}, "
        f"{summary['verified']} verified ({summary['skipped']} already present)."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
