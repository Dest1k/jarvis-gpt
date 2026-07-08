from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import JarvisSettings
from .model_catalog import MODEL_OVERRIDE_KEY, ModelCatalog
from .storage import JarvisStorage, new_id, utc_now

HF_API_ROOT = "https://huggingface.co"
DOWNLOAD_JOBS_KEY = "models.download_jobs"
GB = 1024**3
DEFAULT_DOWNLOAD_WORKERS = 3
SEGMENT_DOWNLOAD_MIN_BYTES = 64 * 1024 * 1024

DOWNLOAD_EXTENSIONS = (
    ".safetensors",
    ".json",
    ".txt",
    ".model",
    ".tiktoken",
)
DOWNLOAD_NAMES = {
    "config.json",
    "generation_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "chat_template.json",
    "preprocessor_config.json",
    "processor_config.json",
    "vocab.json",
    "vocab.txt",
    "merges.txt",
}


@dataclass(frozen=True)
class DownloadedFile:
    relative_path: str
    size: int
    resumed_from: int
    skipped: bool = False


class ModelHubManager:
    def __init__(self, *, settings: JarvisSettings, storage: JarvisStorage) -> None:
        self.settings = settings
        self.storage = storage

    def inventory(self) -> dict[str, Any]:
        catalog = ModelCatalog(self.settings, self.storage).response()
        budget = _gpu_budget()
        models = []
        for model in catalog["models"]:
            models.append({**model, "fit": estimate_local_model(model, budget)})
        return {**catalog, "models": models, "vram": budget, "downloads": self.download_jobs()}

    def search(self, query: str, *, limit: int = 12, context_tokens: int = 8192) -> dict[str, Any]:
        clean_query = query.strip()
        if not clean_query:
            return {
                "query": "",
                "items": [],
                "vram": _gpu_budget(),
                "token_available": bool(self.token()),
            }
        params = urllib.parse.urlencode(
            {
                "search": clean_query,
                "sort": "downloads",
                "direction": "-1",
                "limit": max(1, min(limit, 30)),
                "full": "true",
            }
        )
        data = _request_json(f"{HF_API_ROOT}/api/models?{params}", token=self.token())
        if not isinstance(data, list):
            raise ValueError("Hugging Face search returned an unexpected payload.")
        budget = _gpu_budget()
        items = [
            self._remote_item(item, budget=budget, context_tokens=context_tokens)
            for item in data
            if isinstance(item, dict)
        ]
        return {
            "query": clean_query,
            "items": items,
            "vram": budget,
            "token_available": bool(self.token()),
        }

    def remote_info(self, repo_id: str, *, context_tokens: int = 8192) -> dict[str, Any]:
        data = self._model_info(repo_id)
        return self._remote_item(data, budget=_gpu_budget(), context_tokens=context_tokens)

    def start_download(
        self,
        repo_id: str,
        *,
        revision: str = "main",
        workers: int = DEFAULT_DOWNLOAD_WORKERS,
    ) -> dict[str, Any]:
        info = self._model_info(repo_id)
        files = [
            item
            for item in _siblings(info)
            if _should_download_model_file(str(item.get("rfilename") or ""))
        ]
        if not files:
            raise ValueError("No downloadable model files were found for this repository.")
        job = {
            "id": new_id("modeldl"),
            "repo_id": repo_id,
            "revision": revision or "main",
            "status": "queued",
            "summary": "Queued model download.",
            "target": str(self._target_dir(repo_id)),
            "total_files": len(files),
            "completed_files": 0,
            "total_bytes": sum(_int(item.get("size")) for item in files),
            "downloaded_bytes": 0,
            "current_file": "",
            "error": "",
            "workers": max(1, min(workers, 6)),
            "resumable": True,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        self._save_job(job)
        worker = threading.Thread(
            target=self._download_worker,
            args=(job["id"], repo_id, job["revision"], files, int(job["workers"])),
            daemon=True,
            name=f"jarvis-model-download-{job['id']}",
        )
        worker.start()
        return job

    def download_jobs(self) -> list[dict[str, Any]]:
        jobs = self.storage.get_runtime_value(DOWNLOAD_JOBS_KEY, [])
        if not isinstance(jobs, list):
            return []
        return [item for item in jobs if isinstance(item, dict)][:20]

    def activate_model(self, model_id: str) -> dict[str, Any]:
        path = self._local_model_path(model_id)
        if not path.exists() or not path.is_dir():
            raise ValueError(f"Model is not installed: {model_id}")
        self.storage.set_runtime_value(MODEL_OVERRIDE_KEY, path.name)
        self.storage.record_audit(
            actor="operator",
            action="model.activate",
            target_type="model",
            target_id=path.name,
            summary=f"Model override set to {path.name}",
            after={"model_id": path.name, "path": str(path)},
        )
        return {
            "ok": True,
            "summary": f"Активная модель переключена на {path.name}.",
            "model_id": path.name,
            "path": str(path),
        }

    def clear_model_override(self) -> dict[str, Any]:
        self.storage.set_runtime_value(MODEL_OVERRIDE_KEY, "")
        return {"ok": True, "summary": "Активная модель возвращена к профилю."}

    def delete_model(self, model_id: str) -> dict[str, Any]:
        path = self._local_model_path(model_id)
        active = ModelCatalog(self.settings, self.storage).active_model_dir_name()
        if path.name == active:
            raise ValueError("Active model cannot be deleted. Switch to another model first.")
        if not path.exists():
            raise ValueError(f"Model is not installed: {model_id}")
        size = _folder_size(path)
        shutil.rmtree(path)
        self.storage.record_audit(
            actor="operator",
            action="model.delete",
            target_type="model",
            target_id=path.name,
            summary=f"Deleted local model {path.name}",
            before={"path": str(path), "size_bytes": size},
        )
        return {
            "ok": True,
            "summary": f"Удалена модель {path.name}.",
            "model_id": path.name,
            "freed_bytes": size,
        }

    def token(self) -> str:
        env_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
        if env_token:
            return env_token.strip()
        candidates = [
            self.settings.home / "hf_token.txt",
            self.settings.home / ".jarvis" / "hf_token.txt",
        ]
        for path in candidates:
            try:
                token = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if token:
                return token.splitlines()[0].strip()
        return ""

    def _remote_item(
        self,
        item: dict[str, Any],
        *,
        budget: dict[str, Any],
        context_tokens: int,
    ) -> dict[str, Any]:
        siblings = _siblings(item)
        file_count = len(
            [file for file in siblings if _should_download_model_file(file["rfilename"])]
        )
        size_bytes = sum(
            _int(file.get("size"))
            for file in siblings
            if _should_download_model_file(str(file.get("rfilename") or ""))
        )
        model_id = str(item.get("modelId") or item.get("id") or "")
        config = _dict(item.get("config"))
        safetensors = _dict(item.get("safetensors"))
        return {
            "id": model_id,
            "author": item.get("author"),
            "downloads": _int(item.get("downloads")),
            "likes": _int(item.get("likes")),
            "tags": _list(item.get("tags"))[:18],
            "pipeline_tag": item.get("pipeline_tag") or item.get("pipelineTag"),
            "private": bool(item.get("private")),
            "gated": item.get("gated"),
            "last_modified": item.get("lastModified") or item.get("last_modified"),
            "siblings": siblings[:120],
            "downloadable_files": file_count,
            "size_bytes": size_bytes,
            "config": config,
            "safetensors": safetensors,
            "fit": estimate_remote_model(
                model_id=model_id,
                config=config,
                safetensors=safetensors,
                tags=_list(item.get("tags")),
                size_bytes=size_bytes,
                budget=budget,
                context_tokens=context_tokens,
            ),
        }

    def _model_info(self, repo_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[\w.\-]+/[\w.\-]+", repo_id.strip()):
            raise ValueError("Model id must look like 'owner/name'.")
        url = f"{HF_API_ROOT}/api/models/{urllib.parse.quote(repo_id.strip(), safe='/')}"
        data = _request_json(url, token=self.token())
        if not isinstance(data, dict):
            raise ValueError("Hugging Face returned an unexpected model payload.")
        return data

    def _target_dir(self, repo_id: str) -> Path:
        name = repo_id.strip().replace("/", "__")
        return self.settings.model_root / name

    def _local_model_path(self, model_id: str) -> Path:
        name = model_id.strip().replace("/", "__")
        path = (self.settings.model_root / name).resolve(strict=False)
        root = self.settings.model_root.resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("Model path escapes the model root.") from exc
        return path

    def _download_worker(
        self,
        job_id: str,
        repo_id: str,
        revision: str,
        files: list[dict[str, Any]],
        workers: int,
    ) -> None:
        target = self._target_dir(repo_id)
        token = self.token()
        lock = threading.Lock()
        pending_files = _pending_download_files(target, files)
        completed_files = len(files) - len(pending_files)
        downloaded_total = _existing_downloaded_bytes(target, files)
        running: set[str] = set()
        file_workers = max(1, min(workers, 6))
        segment_workers = file_workers if len(pending_files) == 1 else 1
        try:
            target.mkdir(parents=True, exist_ok=True)
            self._update_job(
                job_id,
                status="running",
                summary=f"Downloading {repo_id}",
                downloaded_bytes=downloaded_total,
                updated_at=utc_now(),
            )

            def fetch(file_info: dict[str, Any]) -> DownloadedFile:
                relative = str(file_info.get("rfilename") or "")
                expected_size = _int(file_info.get("size"))
                with lock:
                    running.add(relative)
                    self._update_job(
                        job_id,
                        status="running",
                        current_file=", ".join(sorted(running)[:3]),
                        updated_at=utc_now(),
                    )
                result = _download_file(
                    repo_id=repo_id,
                    revision=revision,
                    relative_path=relative,
                    target_root=target,
                    token=token,
                    expected_size=expected_size,
                    part_workers=segment_workers,
                )
                with lock:
                    running.discard(relative)
                return result

            with ThreadPoolExecutor(max_workers=file_workers) as pool:
                futures = [pool.submit(fetch, file_info) for file_info in pending_files]
                for future in as_completed(futures):
                    result = future.result()
                    with lock:
                        completed_files += 1
                        downloaded_total = _existing_downloaded_bytes(target, files)
                        resumed_note = (
                            f" resumed from {result.resumed_from} bytes"
                            if result.resumed_from
                            else ""
                        )
                        skipped_note = " skipped existing file" if result.skipped else ""
                        summary = (
                            f"Downloaded {result.relative_path}"
                            f"{resumed_note}{skipped_note}."
                        )
                        self._update_job(
                            job_id,
                            status="running",
                            summary=summary,
                            current_file=", ".join(sorted(running)[:3]),
                            completed_files=completed_files,
                            downloaded_bytes=downloaded_total,
                            updated_at=utc_now(),
                        )
            self._update_job(
                job_id,
                status="done",
                summary=f"Downloaded {repo_id}.",
                current_file="",
                completed_files=len(files),
                downloaded_bytes=_existing_downloaded_bytes(target, files),
                updated_at=utc_now(),
            )
        except Exception as exc:  # noqa: BLE001
            self._update_job(
                job_id,
                status="error",
                summary=f"Download failed: {exc}",
                error=str(exc),
                updated_at=utc_now(),
            )

    def _save_job(self, job: dict[str, Any]) -> None:
        jobs = [job, *self.download_jobs()]
        self.storage.set_runtime_value(DOWNLOAD_JOBS_KEY, jobs[:20])

    def _update_job(self, job_id: str, **patch: Any) -> None:
        jobs = self.download_jobs()
        next_jobs = []
        for job in jobs:
            if job.get("id") == job_id:
                next_jobs.append({**job, **patch})
            else:
                next_jobs.append(job)
        self.storage.set_runtime_value(DOWNLOAD_JOBS_KEY, next_jobs[:20])


def estimate_local_model(model: dict[str, Any], budget: dict[str, Any]) -> dict[str, Any]:
    config_path = Path(str(model.get("path") or "")) / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        config = {}
    return estimate_remote_model(
        model_id=str(model.get("id") or ""),
        config=_dict(config),
        safetensors={},
        tags=[],
        size_bytes=_int(model.get("size_bytes")),
        budget=budget,
        context_tokens=8192,
    )


def estimate_remote_model(
    *,
    model_id: str,
    config: dict[str, Any],
    safetensors: dict[str, Any],
    tags: list[Any],
    size_bytes: int,
    budget: dict[str, Any],
    context_tokens: int,
) -> dict[str, Any]:
    params = _parameter_count(model_id, safetensors, tags)
    quant_bits = _quant_bits(model_id, tags)
    weights = size_bytes or int(params * quant_bits / 8) if params else size_bytes
    layers = _first_int(config, "num_hidden_layers", "n_layer", "num_layers")
    hidden = _first_int(config, "hidden_size", "n_embd", "d_model")
    kv_dtype_bytes = 1
    kv_cache = (
        layers * hidden * 2 * max(512, context_tokens) * kv_dtype_bytes
        if layers and hidden
        else 0
    )
    overhead = max(int(1.5 * GB), int(weights * 0.14)) if weights else int(2 * GB)
    required = int(weights + kv_cache + overhead)
    total = _int(budget.get("total_bytes"))
    free = _int(budget.get("free_bytes"))
    usable = int(total * 0.90) if total else 0
    if total and required <= usable:
        status = "fits"
        label = "заведётся"
    elif total and required <= total:
        status = "tight"
        label = "на грани"
    elif total:
        status = "no"
        label = "не заведётся"
    else:
        status = "unknown"
        label = "нет данных GPU"
    confidence = (
        "high"
        if size_bytes and layers and hidden
        else "medium"
        if size_bytes or params
        else "low"
    )
    warnings = []
    if status == "tight":
        warnings.append(
            "По общей VRAM проходит только без запаса; возможны OOM при длинном контексте."
        )
    if status == "no":
        warnings.append(
            "Оценка выше доступной VRAM; нужен меньший квант, offload или другая модель."
        )
    if free and required > free:
        warnings.append("Прямо сейчас свободной VRAM недостаточно без остановки текущей нагрузки.")
    if confidence != "high":
        warnings.append("Метаданные неполные; оценка приблизительная.")
    return {
        "status": status,
        "label": label,
        "confidence": confidence,
        "required_bytes": required,
        "weights_bytes": int(weights),
        "kv_cache_bytes": int(kv_cache),
        "overhead_bytes": int(overhead),
        "gpu_total_bytes": total,
        "gpu_free_bytes": free,
        "context_tokens": context_tokens,
        "quant_bits": quant_bits,
        "parameters": int(params) if params else None,
        "warnings": warnings,
    }


def _download_file(
    *,
    repo_id: str,
    revision: str,
    relative_path: str,
    target_root: Path,
    token: str,
    expected_size: int,
    part_workers: int = 1,
) -> DownloadedFile:
    if ".." in Path(relative_path).parts:
        raise ValueError(f"Unsafe file path: {relative_path}")
    target = target_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    if expected_size and target.exists() and target.stat().st_size == expected_size:
        return DownloadedFile(
            relative_path=relative_path,
            size=target.stat().st_size,
            resumed_from=0,
            skipped=True,
        )
    if target.exists() and not expected_size:
        return DownloadedFile(
            relative_path=relative_path,
            size=target.stat().st_size,
            resumed_from=0,
            skipped=True,
        )
    segment_dir = _segment_dir(target)
    should_segment = (
        expected_size
        and not tmp.exists()
        and (
            segment_dir.exists()
            or (part_workers > 1 and expected_size >= SEGMENT_DOWNLOAD_MIN_BYTES)
        )
    )
    url = _download_url(repo_id=repo_id, revision=revision, relative_path=relative_path)
    if should_segment and _server_supports_range(url, token=token):
        return _download_file_segmented(
            url=url,
            relative_path=relative_path,
            target=target,
            segment_dir=segment_dir,
            token=token,
            expected_size=expected_size,
            part_workers=part_workers,
        )
    return _download_file_streaming(
        url=url,
        relative_path=relative_path,
        target=target,
        token=token,
        expected_size=expected_size,
    )


def _download_file_streaming(
    *,
    url: str,
    relative_path: str,
    target: Path,
    token: str,
    expected_size: int,
) -> DownloadedFile:
    tmp = target.with_suffix(target.suffix + ".part")
    resume_from = tmp.stat().st_size if tmp.exists() else 0
    if expected_size and resume_from >= expected_size:
        tmp.replace(target)
        return DownloadedFile(
            relative_path=relative_path,
            size=target.stat().st_size,
            resumed_from=resume_from,
            skipped=True,
        )
    headers = _headers(token)
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        status = getattr(response, "status", response.getcode())
        append = resume_from > 0 and status == 206
        if resume_from > 0 and not append:
            resume_from = 0
        mode = "ab" if append else "wb"
        with tmp.open(mode) as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
    if expected_size and tmp.stat().st_size != expected_size:
        raise ValueError(
            f"Downloaded size mismatch for {relative_path}: "
            f"{tmp.stat().st_size} != {expected_size}"
        )
    tmp.replace(target)
    return DownloadedFile(
        relative_path=relative_path,
        size=target.stat().st_size,
        resumed_from=resume_from,
    )


def _download_file_segmented(
    *,
    url: str,
    relative_path: str,
    target: Path,
    segment_dir: Path,
    token: str,
    expected_size: int,
    part_workers: int,
) -> DownloadedFile:
    workers = max(1, min(part_workers, 6))
    segment_dir.mkdir(parents=True, exist_ok=True)
    segment_size = max(1, (expected_size + workers - 1) // workers)
    ranges = []
    for index in range(workers):
        start = index * segment_size
        if start >= expected_size:
            break
        end = min(expected_size - 1, start + segment_size - 1)
        ranges.append((index, start, end))
    resumed_from = sum(
        min((segment_dir / f"{index}.part").stat().st_size, end - start + 1)
        for index, start, end in ranges
        if (segment_dir / f"{index}.part").exists()
    )
    with ThreadPoolExecutor(max_workers=len(ranges) or 1) as pool:
        futures = [
            pool.submit(
                _download_segment,
                url=url,
                segment_path=segment_dir / f"{index}.part",
                token=token,
                start=start,
                end=end,
            )
            for index, start, end in ranges
        ]
        for future in as_completed(futures):
            future.result()
    tmp = target.with_suffix(target.suffix + ".part")
    with tmp.open("wb") as output:
        for index, start, end in ranges:
            segment_path = segment_dir / f"{index}.part"
            expected_segment_size = end - start + 1
            if not segment_path.exists() or segment_path.stat().st_size != expected_segment_size:
                raise ValueError(f"Segment {index} is incomplete for {relative_path}")
            with segment_path.open("rb") as segment:
                shutil.copyfileobj(segment, output, length=1024 * 1024)
    if tmp.stat().st_size != expected_size:
        raise ValueError(
            f"Downloaded size mismatch for {relative_path}: "
            f"{tmp.stat().st_size} != {expected_size}"
        )
    tmp.replace(target)
    shutil.rmtree(segment_dir, ignore_errors=True)
    return DownloadedFile(
        relative_path=relative_path,
        size=target.stat().st_size,
        resumed_from=resumed_from,
    )


def _download_segment(
    *,
    url: str,
    segment_path: Path,
    token: str,
    start: int,
    end: int,
) -> None:
    expected_size = end - start + 1
    resume_from = segment_path.stat().st_size if segment_path.exists() else 0
    if resume_from >= expected_size:
        return
    segment_path.parent.mkdir(parents=True, exist_ok=True)
    headers = _headers(token)
    headers["Range"] = f"bytes={start + resume_from}-{end}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        status = getattr(response, "status", response.getcode())
        if status != 206:
            raise ValueError("Server did not honor ranged model download.")
        with segment_path.open("ab") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
    if segment_path.stat().st_size != expected_size:
        raise ValueError(
            f"Segment size mismatch: {segment_path.stat().st_size} != {expected_size}"
        )


def _server_supports_range(url: str, *, token: str) -> bool:
    headers = _headers(token)
    headers["Range"] = "bytes=0-0"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return getattr(response, "status", response.getcode()) == 206
    except Exception:
        return False


def _download_url(*, repo_id: str, revision: str, relative_path: str) -> str:
    return (
        f"{HF_API_ROOT}/{urllib.parse.quote(repo_id, safe='/')}/resolve/"
        f"{urllib.parse.quote(revision or 'main', safe='')}/"
        f"{urllib.parse.quote(relative_path, safe='/')}"
    )


def _segment_dir(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".segments")


def _request_json(url: str, *, token: str) -> Any:
    request = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise ValueError(f"Hugging Face HTTP {exc.code}: {body}") from exc


def _headers(token: str) -> dict[str, str]:
    headers = {"User-Agent": "jarvis-gpt-model-browser/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _siblings(item: dict[str, Any]) -> list[dict[str, Any]]:
    siblings = item.get("siblings") or []
    if not isinstance(siblings, list):
        return []
    result = []
    for file in siblings:
        if isinstance(file, dict) and file.get("rfilename"):
            result.append(
                {
                    "rfilename": str(file.get("rfilename")),
                    "size": _int(file.get("size")),
                }
            )
    return result


def _should_download_model_file(name: str) -> bool:
    base = Path(name).name
    lowered = base.lower()
    if lowered in DOWNLOAD_NAMES:
        return True
    if lowered.startswith("tokenizer.") or lowered.startswith("vocab."):
        return True
    return any(lowered.endswith(ext) for ext in DOWNLOAD_EXTENSIONS) and not lowered.endswith(
        (".bin", ".pt", ".pth")
    )


def _pending_download_files(target_root: Path, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending = []
    for file in files:
        relative = str(file.get("rfilename") or "")
        expected_size = _int(file.get("size"))
        target = target_root / relative
        if target.exists() and (not expected_size or target.stat().st_size == expected_size):
            continue
        pending.append(file)
    return pending


def _existing_downloaded_bytes(target_root: Path, files: list[dict[str, Any]]) -> int:
    total = 0
    for file in files:
        relative = str(file.get("rfilename") or "")
        target = target_root / relative
        partial = target.with_suffix(target.suffix + ".part")
        if target.exists():
            total += target.stat().st_size
        elif partial.exists():
            total += partial.stat().st_size
        elif _segment_dir(target).exists():
            total += sum(item.stat().st_size for item in _segment_dir(target).glob("*.part"))
    return total


def _gpu_budget() -> dict[str, Any]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used",
        "--format=csv,noheader,nounits",
    ]
    if shutil.which(command[0]) is None:
        return {
            "available": False,
            "total_bytes": 0,
            "free_bytes": 0,
            "error": "nvidia-smi not found",
        }
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=4, check=False)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "total_bytes": 0, "free_bytes": 0, "error": str(exc)}
    if result.returncode != 0:
        return {
            "available": False,
            "total_bytes": 0,
            "free_bytes": 0,
            "error": result.stderr.strip(),
        }
    first = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
    parts = [part.strip() for part in first.split(",")]
    if len(parts) < 3:
        return {
            "available": False,
            "total_bytes": 0,
            "free_bytes": 0,
            "error": "GPU data unavailable",
        }
    total = int(float(parts[1]) * 1024 * 1024)
    used = int(float(parts[2]) * 1024 * 1024)
    return {
        "available": True,
        "name": parts[0],
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": max(0, total - used),
    }


def _parameter_count(model_id: str, safetensors: dict[str, Any], tags: list[Any]) -> int:
    total = _int(safetensors.get("total"))
    if total:
        return total
    text = " ".join([model_id, *[str(item) for item in tags]]).lower()
    match = re.search(r"(\d+(?:\.\d+)?)\s*b\b", text)
    if match:
        return int(float(match.group(1)) * 1_000_000_000)
    match = re.search(r"(\d+(?:\.\d+)?)\s*m\b", text)
    if match:
        return int(float(match.group(1)) * 1_000_000)
    return 0


def _quant_bits(model_id: str, tags: list[Any]) -> float:
    text = " ".join([model_id, *[str(item) for item in tags]]).lower()
    if any(marker in text for marker in ("nvfp4", "fp4", "4bit", "4-bit", "q4", "int4")):
        return 4.5
    if any(marker in text for marker in ("q5", "5bit", "5-bit")):
        return 5.5
    if any(marker in text for marker in ("q8", "8bit", "8-bit", "int8", "fp8")):
        return 8.5
    if "bf16" in text or "bfloat16" in text or "fp16" in text or "float16" in text:
        return 16
    return 16


def _first_int(config: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = _int(config.get(key))
        if value:
            return value
    return 0


def _folder_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
