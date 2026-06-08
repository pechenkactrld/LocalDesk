import os
import platform
import re
import shutil
import subprocess
import json
import codecs
from datetime import datetime
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QShowEvent, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QLabel,
    QMainWindow,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QListWidget,
    QSlider,
    QSizePolicy,
    QSpacerItem,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

def _get_startupinfo():
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return si
    return None

try:
    import psutil
except Exception:
    psutil = None


@dataclass(frozen=True)
class HardwareSnapshot:
    cpu_name: str
    cpu_cores_logical: int
    cpu_cores_physical: int
    cpu_freq_mhz: Optional[int]
    ram_total_gb: float
    ram_available_gb: float
    disk_free_gb: float
    disk_total_gb: float
    gpu_description: str

    def to_pretty_dict(self) -> Dict[str, str]:
        return {
            "CPU": f"{self.cpu_name} | logical {self.cpu_cores_logical}, physical {self.cpu_cores_physical} | "
                    f"{self.cpu_freq_mhz or 0} MHz",
            "RAM": f"{self.ram_available_gb:.2f} GB available / {self.ram_total_gb:.2f} GB total",
            "Disk Free": f"{self.disk_free_gb:.2f} GB free / {self.disk_total_gb:.2f} GB total",
            "GPU": self.gpu_description,
        }


def _safe_float_gb(bytes_val: float) -> float:
    return bytes_val / (1024 ** 3)


def _effective_host_ram_gib(available_gb: float, total_gb: float) -> float:
    available_gb = max(0.0, available_gb)
    total_gb = max(0.0, total_gb)
    if total_gb <= 0:
        return available_gb
    if total_gb < 12:
        from_total = max(0.0, (total_gb - 2.5) * 0.58)
    else:
        from_total = max(0.0, (total_gb - 3.0) * 0.78)
    return max(available_gb, from_total)


def _is_apple_unified_gpu(gpu_description: str) -> bool:
    gl = gpu_description.lower()
    return "apple" in gl and any(
        token in gl
        for token in ("m1", "m2", "m3", "m4", "m5", "chip", "silicon", "metal")
    )


def get_cpu_info() -> Dict[str, object]:
    cpu_name = platform.processor().strip() or platform.uname().processor.strip() or "Unknown CPU"
    logical = int(psutil.cpu_count(logical=True) or 0) if psutil else 0
    physical = int(psutil.cpu_count(logical=False) or 0) if psutil else 0
    cpu_freq_mhz: Optional[int] = None
    if psutil:
        try:
            freq = psutil.cpu_freq()
            if freq and getattr(freq, "current", None):
                cpu_freq_mhz = int(freq.current)
        except Exception:
            cpu_freq_mhz = None
    return {
        "cpu_name": cpu_name,
        "cpu_cores_logical": logical,
        "cpu_cores_physical": physical,
        "cpu_freq_mhz": cpu_freq_mhz,
    }


def get_ram_disk_info() -> Dict[str, float]:
    if not psutil:
        return {
            "ram_total_gb": 0.0,
            "ram_available_gb": 0.0,
            "disk_free_gb": 0.0,
            "disk_total_gb": 0.0,
        }

    vm = psutil.virtual_memory()
    home = os.path.expanduser("~")
    try:
        usage = psutil.disk_usage(home)
    except Exception:
        usage = psutil.disk_usage("/")

    return {
        "ram_total_gb": _safe_float_gb(float(vm.total)),
        "ram_available_gb": _safe_float_gb(float(vm.available)),
        "disk_free_gb": _safe_float_gb(float(usage.free)),
        "disk_total_gb": _safe_float_gb(float(usage.total)),
    }


def query_nvidia_vram_mib() -> List[Tuple[str, float, float]]:
    rows: List[Tuple[str, float, float]] = []
    try:
        if not shutil.which("nvidia-smi"):
            return rows
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            startupinfo=_get_startupinfo(),
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return rows
        for ln in proc.stdout.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            parts = [p.strip() for p in ln.split(",")]
            if len(parts) < 3:
                continue
            name, total_mib, free_mib = parts[0], parts[1], parts[2]
            try:
                rows.append((name, float(total_mib), float(free_mib)))
            except Exception:
                continue
    except Exception:
        return []
    return rows


def nvidia_vram_first_gpu_gib() -> Optional[Tuple[float, float]]:
    rows = query_nvidia_vram_mib()
    if not rows:
        return None
    _, total_mib, free_mib = rows[0]
    return (total_mib / 1024.0, free_mib / 1024.0)


def get_gpu_info() -> str:
    rows = query_nvidia_vram_mib()
    if rows:
        parsed: List[str] = []
        for name, total_mib, free_mib in rows:
            try:
                parsed.append(
                    f"{name} | VRAM free {free_mib/1024:.1f} GB / {total_mib/1024:.1f} GB"
                )
            except Exception:
                parsed.append(name)
        if parsed:
            return "; ".join(parsed)

    try:
        if shutil.which("lspci"):
            proc = subprocess.run(
                ["lspci", "-nnk"],
                capture_output=True,
                startupinfo=_get_startupinfo(),
                text=True,
                timeout=5,
                check=False,
            )
            text = proc.stdout or ""
            lowered = text.lower()
            if "nvidia" in lowered or "amd" in lowered or "intel" in lowered:
                lines = []
                for ln in text.splitlines():
                    ln_low = ln.lower()
                    if ("vga" in ln_low or "3d controller" in ln_low) and (
                        "nvidia" in ln_low or "amd" in ln_low or "ati" in ln_low or "intel" in ln_low
                    ):
                        lines.append(ln.strip())
                if lines:
                    return "; ".join(lines[:2])
    except Exception:
        pass

    if platform.system() == "Darwin":
        try:
            proc = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True,
                startupinfo=_get_startupinfo(),
                text=True,
                timeout=15,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                names: List[str] = []
                for ln in proc.stdout.splitlines():
                    s = ln.strip()
                    if s.startswith("Chipset Model:") or s.startswith("Model:"):
                        val = s.split(":", 1)[-1].strip()
                        if val and val not in names:
                            names.append(val)
                if names:
                    return "; ".join(names[:2])
        except Exception:
            pass

    if platform.system() == "Windows":
        try:
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_VideoController | "
                    "ForEach-Object { $_.Name } | Where-Object { $_ }",
                ],
                capture_output=True,
                startupinfo=_get_startupinfo(),
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                names = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
                if names:
                    return "; ".join(names[:2])
        except Exception:
            pass

    return "GPU not detected (`nvidia-smi`/`lspci` missing or access is restricted)."


@dataclass(frozen=True)
class ModelRecommendation:

    model_id: str
    rationale: str


@dataclass(frozen=True)
class _ModelTier:
    model_id: str
    min_vram_gib: float
    min_disk_gib: float
    min_ram_cpu_gib: float

_MODEL_TIERS: Tuple[_ModelTier, ...] = (
    _ModelTier("llama3.2:1b",  1.5,  1.5,  3.0),
    _ModelTier("llama3.2:3b",  2.5,  2.5,  5.0),
    _ModelTier("qwen2.5:7b",   4.5,  4.5,  8.0),
    _ModelTier("llama3.1:8b",  5.0,  5.0,  9.0),
    _ModelTier("qwen2.5:14b",  9.0,  8.0, 16.0),
    _ModelTier("llama3.1:70b", 40.0, 40.0, 64.0),
)


def _host_ram_floor_gib(tier: _ModelTier) -> float:
    if "70b" in tier.model_id:
        return 32.0
    if "14b" in tier.model_id:
        return 14.0
    if "8b" in tier.model_id or "7b" in tier.model_id:
        return 7.0
    if "3b" in tier.model_id:
        return 4.0
    return 2.5


def recommend_ollama_model(
    snapshot: HardwareSnapshot,
    *,
    nvidia_vram: Optional[Tuple[float, float]],
) -> ModelRecommendation:
    disk_free = max(0.0, snapshot.disk_free_gb)
    ram_avail = max(0.0, snapshot.ram_available_gb)
    ram_total = max(0.0, snapshot.ram_total_gb)
    host_ram = _effective_host_ram_gib(ram_avail, ram_total)

    DISK_BUF = 1.5
    VRAM_BUF = 1.0
    RAM_BUF = 1.5

    no_gpu_line = snapshot.gpu_description.startswith("GPU not detected")
    gl = snapshot.gpu_description.lower()

    intel_igpu_only = (
        "intel" in gl
        and ("graphics" in gl or "uhd" in gl or "iris" in gl)
        and "nvidia" not in gl
        and "amd" not in gl
        and "ati" not in gl
    )
    maybe_discrete = (
        not no_gpu_line
        and not intel_igpu_only
        and not _is_apple_unified_gpu(snapshot.gpu_description)
        and ("nvidia" in gl or "amd" in gl or "ati" in gl or "radeon" in gl)
    )
    unknown_discrete = nvidia_vram is None and maybe_discrete
    apple_unified = nvidia_vram is None and _is_apple_unified_gpu(snapshot.gpu_description)

    notes: List[str] = []
    chosen: Optional[_ModelTier] = None

    if nvidia_vram is not None:
        vram_total, vram_free = nvidia_vram
        vram_budget = max(0.0, vram_free - VRAM_BUF)
        notes.append(f"Mode: GPU (NVIDIA), free VRAM ~{vram_free:.1f} GiB.")
        for tier in reversed(_MODEL_TIERS):
            if disk_free < tier.min_disk_gib + DISK_BUF:
                continue
            if vram_budget < tier.min_vram_gib:
                continue
            if host_ram < _host_ram_floor_gib(tier):
                continue
            chosen = tier
            break
    elif apple_unified and ram_total > 0:
        vram_budget = max(0.0, ram_total * 0.68 - VRAM_BUF)
        notes.append(
            "Mode: Apple Silicon (unified memory) — sizing from total RAM "
            f"(~{vram_budget:.1f} GiB budget for the model)."
        )
        for tier in reversed(_MODEL_TIERS):
            if disk_free < tier.min_disk_gib + DISK_BUF:
                continue
            if vram_budget < tier.min_vram_gib:
                continue
            if host_ram < _host_ram_floor_gib(tier):
                continue
            if ram_total > 0 and ram_total < tier.min_ram_cpu_gib + 2.0:
                continue
            chosen = tier
            break
    elif unknown_discrete:
        vram_budget = 7.0
        notes.append(
            "Mode: GPU is likely present, but VRAM is unknown (`nvidia-smi` missing). "
            f"Using conservative estimate (~<= {vram_budget:.0f} GiB class)."
        )
        for tier in reversed(_MODEL_TIERS):
            if tier.min_vram_gib > vram_budget:
                continue
            if disk_free < tier.min_disk_gib + DISK_BUF:
                continue
            if host_ram < _host_ram_floor_gib(tier):
                continue
            chosen = tier
            break
    else:
        notes.append(
            "Mode: CPU / integrated graphics — selecting by RAM "
            f"(~{host_ram:.1f} GiB effective budget)."
        )
        ram_budget = max(0.0, host_ram - RAM_BUF)
        logical = max(1, snapshot.cpu_cores_logical)
        if logical < 6:
            ram_budget = min(ram_budget, 10.0)
            notes.append("Low logical core count detected - limiting model size for responsiveness.")
        elif logical < 10:
            ram_budget = min(ram_budget, 18.0)

        for tier in reversed(_MODEL_TIERS):
            if disk_free < tier.min_disk_gib + DISK_BUF:
                continue
            if ram_budget < tier.min_ram_cpu_gib:
                continue
            if ram_total > 0 and ram_total < tier.min_ram_cpu_gib + 2.0:
                continue
            chosen = tier
            break

    if chosen is None:
        fallback = _MODEL_TIERS[0]
        warn: List[str] = []
        if disk_free < fallback.min_disk_gib + DISK_BUF:
            warn.append("Not enough space on disk")
        if (
            nvidia_vram is None
            and not unknown_discrete
            and not apple_unified
            and host_ram < fallback.min_ram_cpu_gib
        ):
            warn.append("Not enough RAM")
        if nvidia_vram is not None and nvidia_vram[1] < fallback.min_vram_gib + VRAM_BUF:
            warn.append("Not enough VRAM")
        suffix = f" ({'; '.join(warn)})" if warn else ""
        return ModelRecommendation(
            model_id=fallback.model_id,
            rationale=(
                f"Recommended: {fallback.model_id}. "
                f"Hardware did not pass automatic thresholds for larger models{suffix}. "
                "Free up disk/RAM or choose a smaller model manually."
            ),
        )

    rationale = (
        f"Recommended: `{chosen.model_id}`.\n"
        + "\n".join(notes)
        + f"\nAvailable resources: disk ~{disk_free:.1f} GiB, "
        f"RAM ~{ram_avail:.1f} GiB available / {ram_total:.1f} GiB total "
        f"(~{host_ram:.1f} GiB effective for sizing)."
    )
    if nvidia_vram is not None:
        rationale += f" Free VRAM ~{nvidia_vram[1]:.1f} GiB."
    return ModelRecommendation(model_id=chosen.model_id, rationale=rationale)


@dataclass(frozen=True)
class HardwareCheckPayload:
    pretty: Dict[str, str]
    recommendation: ModelRecommendation


class HardwareCheckBridge(QWidget):

    snapshot_ready = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()

    def start(self) -> None:
        def worker() -> None:
            try:
                cpu = get_cpu_info()
                ram_disk = get_ram_disk_info()
                nvram = nvidia_vram_first_gpu_gib()
                gpu = get_gpu_info()

                snapshot = HardwareSnapshot(
                    cpu_name=str(cpu["cpu_name"]),
                    cpu_cores_logical=int(cpu["cpu_cores_logical"]),
                    cpu_cores_physical=int(cpu["cpu_cores_physical"]),
                    cpu_freq_mhz=cpu["cpu_freq_mhz"],
                    ram_total_gb=float(ram_disk["ram_total_gb"]),
                    ram_available_gb=float(ram_disk["ram_available_gb"]),
                    disk_free_gb=float(ram_disk["disk_free_gb"]),
                    disk_total_gb=float(ram_disk["disk_total_gb"]),
                    gpu_description=gpu,
                )

                pretty = snapshot.to_pretty_dict()
                recommendation = recommend_ollama_model(snapshot, nvidia_vram=nvram)
            except Exception as e:
                pretty = {"Error while getting PC info": str(e)}
                recommendation = ModelRecommendation(
                    model_id="llama3.2:1b",
                    rationale=f"Hardware analysis error: {e}. Safe minimum: llama3.2:1b.",
                )

            for k, v in pretty.items():
                print(f"{k}: {v}")
            print(f"Recommended model (ollama): {recommendation.model_id}")
            print(recommendation.rationale)

            self.snapshot_ready.emit(
                HardwareCheckPayload(pretty=pretty, recommendation=recommendation)
            )

        threading.Thread(target=worker, daemon=True).start()


def ollama_binary() -> Optional[str]:
    return shutil.which("ollama")


def ollama_available() -> bool:
    return ollama_binary() is not None


def _get_requests_session(base_url: str) -> requests.Session:
    """Create a requests session with retry logic."""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def list_local_ollama_models_api(base_url: str) -> Tuple[List[str], Optional[str]]:
    """List models using Ollama API (recommended)."""
    try:
        session = _get_requests_session(base_url)
        response = session.get(f"{base_url}/api/tags", timeout=15)
        response.raise_for_status()
        data = response.json()
        
        models: List[str] = []
        if "models" in data and isinstance(data["models"], list):
            for model_info in data["models"]:
                if isinstance(model_info, dict) and "name" in model_info:
                    model_name = model_info["name"].strip()
                    if model_name and model_name not in models:
                        models.append(model_name)
        
        return models, None
    except requests.exceptions.ConnectionError:
        return [], f"Cannot connect to Ollama at {base_url}"
    except requests.exceptions.Timeout:
        return [], f"Connection to {base_url} timed out"
    except requests.exceptions.HTTPError as e:
        return [], f"HTTP error: {e.response.status_code}"
    except json.JSONDecodeError:
        return [], "Invalid JSON response from Ollama API"
    except Exception as e:
        return [], f"Error: {str(e)}"


# def list_local_ollama_models() -> Tuple[List[str], Optional[str]]:
#     bin_path = ollama_binary()
#     if not bin_path:
#         return [], "Command `ollama` can't be found in PATH"
#
#     proc = subprocess.run(
#         [bin_path, "list"],
#         capture_output=True,
#         startupinfo=_get_startupinfo(),
#         text=True,
#         timeout=15,
#         check=False,
#     )
#     if proc.returncode != 0:
#         err = (proc.stderr or proc.stdout or "Can't get models list").strip()
#         return [], err
#
#     lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
#     if not lines:
#         return [], None
#
#     models: List[str] = []
#     for idx, ln in enumerate(lines):
#         if idx == 0 and ln.lower().startswith("name"):
#             continue
#         first = ln.split()[0]
#         if first:
#             models.append(first)
#
#     unique_models: List[str] = []
#     seen = set()
#     for m in models:
#         if m not in seen:
#             unique_models.append(m)
#             seen.add(m)
#     return unique_models, None


def list_local_ollama_models() -> Tuple[List[str], Optional[str]]:
    return [], "Use list_local_ollama_models_api(base_url) instead"


def _extract_percent(text: str) -> Optional[int]:
    matches = re.findall(r"(\d{1,3})%", text)
    if not matches:
        return None
    try:
        value = int(matches[-1])
    except ValueError:
        return None
    return max(0, min(100, value))


def pull_ollama_model_api(
    base_url: str,
    model_name: str,
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> Tuple[bool, str]:
    try:
        session = _get_requests_session(base_url)
        
        if on_progress:
            on_progress(-1, "Preparing to download...")
        
        response = session.post(
            f"{base_url}/api/pull",
            json={"name": model_name},
            timeout=3600,
            stream=True
        )
        response.raise_for_status()
        
        lines = []
        for line in response.iter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
                if "status" in data:
                    status = data["status"]
                    lines.append(status)
                    if on_progress:
                        pct = _extract_percent(status) if "%" in status else -1
                        on_progress(pct, status)
                if data.get("error"):
                    return False, data["error"]
            except json.JSONDecodeError:
                continue
        
        return True, "Model downloaded successfully" if lines else "Model installed"
        
    except requests.exceptions.ConnectionError:
        return False, f"Cannot connect to Ollama at {base_url}"
    except requests.exceptions.Timeout:
        return False, f"Download timed out while pulling {model_name}"
    except requests.exceptions.HTTPError as e:
        return False, f"HTTP error {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return False, f"Error downloading model: {str(e)}"


# def pull_ollama_model(
#     model_name: str,
#     on_progress: Optional[Callable[[int, str], None]] = None,
# ) -> Tuple[bool, str]:
#     bin_path = ollama_binary()
#     if not bin_path:
#         return False, "Command `ollama` can't be found in PATH"
#     proc = subprocess.Popen(
#         [bin_path, "pull", model_name],
#         stdout=subprocess.PIPE,
#         stderr=subprocess.STDOUT,
#         startupinfo=_get_startupinfo(),
#         text=True,
#     )
#     ansi_escape_re = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
#
#     def _sanitize_pull_text(text: str) -> str:
#         cleaned = ansi_escape_re.sub("", text)
#         cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", cleaned)
#         cleaned = re.sub(r"\[\?[0-9;]*[hl]", "", cleaned)
#         cleaned = re.sub(r"\s{2,}", " ", cleaned)
#         return cleaned.strip()
#
#     lines: List[str] = []
#     if on_progress:
#         on_progress(-1, "Preparing to download...")
#     assert proc.stdout is not None
#     for raw in proc.stdout:
#         line = _sanitize_pull_text(raw)
#         if not line:
#             continue
#         lines.append(line)
#         if on_progress:
#             pct = _extract_percent(line)
#             on_progress(pct if pct is not None else -1, line)
#     code = proc.wait()
#     output = "\n".join(lines).strip()
#     if code != 0:
#         return False, output or "Can't download model"
#     return True, output or "Model installed"


def pull_ollama_model(
    model_name: str,
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> Tuple[bool, str]:
    return False, "Use pull_ollama_model_api(base_url, model_name, on_progress) instead"


def run_ollama_chat(model_name: str, prompt: str) -> Tuple[bool, str]:
    return False, "Use run_ollama_chat_api(base_url, model_name, prompt) instead"


# DEPRECATED: CLI version (kept for reference)
# def run_ollama_chat_cli(model_name: str, prompt: str) -> Tuple[bool, str]:
#     bin_path = ollama_binary()
#     if not bin_path:
#         return False, "Command `ollama` can't be found in PATH"
#     proc = subprocess.run(
#         [bin_path, "run", model_name, prompt],
#         capture_output=True,
#         startupinfo=_get_startupinfo(),
#         text=True,
#         check=False,
#     )
#     if proc.returncode != 0:
#         return False, (proc.stderr or proc.stdout or "Error with ollama run.").strip()
#     answer = (proc.stdout or "").strip()
#     if not answer:
#         answer = "Model gave an empty answer"
#     return True, answer


def run_ollama_chat_api(base_url: str, model_name: str, prompt: str) -> Tuple[bool, str]:
    try:
        session = _get_requests_session(base_url)
        response = session.post(
            f"{base_url}/api/generate",
            json={
                "model": model_name,
                "prompt": prompt,
                "stream": False
            },
            timeout=300
        )
        response.raise_for_status()
        data = response.json()
        
        answer = data.get("response", "").strip()
        if not answer:
            return False, "Model gave an empty answer"
        return True, answer
        
    except requests.exceptions.ConnectionError:
        return False, f"Cannot connect to Ollama at {base_url}"
    except requests.exceptions.Timeout:
        return False, f"Request to {base_url} timed out"
    except requests.exceptions.HTTPError as e:
        return False, f"HTTP error: {e.response.text if hasattr(e.response, 'text') else str(e)}"
    except json.JSONDecodeError:
        return False, "Invalid JSON response from Ollama API"
    except Exception as e:
        return False, f"Error: {str(e)}"


def run_ollama_chat_stream(
    model_name: str,
    prompt: str,
    image_paths: Optional[List[str]] = None,
    on_chunk: Optional[Callable[[str], None]] = None,
    on_process: Optional[Callable[[object], None]] = None,
) -> Tuple[bool, str]:
    return False, "Use run_ollama_chat_stream_api(base_url, model_name, prompt) instead"


def run_ollama_chat_stream_api(
    base_url: str,
    model_name: str,
    prompt: str,
    image_paths: Optional[List[str]] = None,
    on_chunk: Optional[Callable[[str], None]] = None,
    on_process: Optional[Callable[[object], None]] = None,
    temperature: Optional[float] = None,
) -> Tuple[bool, str]:
    try:
        session = _get_requests_session(base_url)
        
        payload = {
            "model": model_name,
            "prompt": prompt,
            "stream": True
        }
        
        if temperature is not None:
            payload["temperature"] = temperature
        
        response = session.post(
            f"{base_url}/api/generate",
            json=payload,
            timeout=3600,
            stream=True
        )
        response.raise_for_status()
        
        chunks: List[str] = []
        
        for line in response.iter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
                chunk = data.get("response", "")
                if chunk:
                    chunks.append(chunk)
                    if on_chunk:
                        on_chunk(chunk)
                
                if data.get("done"):
                    break
                    
            except json.JSONDecodeError:
                continue
        
        answer = "".join(chunks).strip()
        if not answer:
            return False, "Model gave an empty answer"
        return True, answer
        
    except requests.exceptions.ConnectionError:
        return False, f"Cannot connect to Ollama at {base_url}"
    except requests.exceptions.Timeout:
        return False, f"Request to {base_url} timed out"
    except requests.exceptions.HTTPError as e:
        return False, f"HTTP error: {e.response.text if hasattr(e.response, 'text') else str(e)}"
    except json.JSONDecodeError:
        return False, "Invalid JSON response from Ollama API"
    except Exception as e:
        return False, f"Error: {str(e)}"


# DEPRECATED: CLI version (kept for reference)
# def run_ollama_chat_stream_cli(
#     model_name: str,
#     prompt: str,
#     image_paths: Optional[List[str]] = None,
#     on_chunk: Optional[Callable[[str], None]] = None,
#     on_process: Optional[Callable[[object], None]] = None,
# ) -> Tuple[bool, str]:
#     bin_path = ollama_binary()
#     if not bin_path:
#         return False, "Command `ollama` can't be found in PATH"
#
#     ansi_escape_re = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
#
#     def _sanitize_stream_text(text: str) -> str:
#         cleaned = ansi_escape_re.sub("", text)
#         cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", cleaned)
#         cleaned = re.sub(r"\[\?[0-9;]*[hl]", "", cleaned)
#         return cleaned
#
#     try:
#         cmd = [bin_path, "run", model_name, prompt]
#         if image_paths:
#             cmd.extend(image_paths)
#         proc = subprocess.Popen(
#             cmd,
#             stdout=subprocess.PIPE,
#             stderr=subprocess.PIPE,
#             startupinfo=_get_startupinfo(),
#             text=False,
#             bufsize=0,
#         )
#     except Exception as e:
#         return False, f"Error starting ollama process: {e}"
#     if on_process:
#         on_process(proc)
#
#     chunks: List[str] = []
#     decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
#     try:
#         assert proc.stdout is not None
#         while True:
#             piece = proc.stdout.read(256)
#             if not piece:
#                 break
#             decoded = decoder.decode(piece)
#             if not decoded:
#                 continue
#             cleaned = _sanitize_stream_text(decoded)
#             if not cleaned:
#                 continue
#             chunks.append(cleaned)
#             if on_chunk:
#                 on_chunk(cleaned)
#
#         tail = decoder.decode(b"", final=True)
#         if tail:
#             cleaned_tail = _sanitize_stream_text(tail)
#             if cleaned_tail:
#                 chunks.append(cleaned_tail)
#                 if on_chunk:
#                     on_chunk(cleaned_tail)
#
#         code = proc.wait()
#         answer = "".join(chunks).strip()
#         if code != 0:
#             err = ""
#             if proc.stderr is not None:
#                 try:
#                     err = proc.stderr.read().decode("utf-8", errors="ignore").strip()
#                 except Exception:
#                     err = ""
#             return False, answer or err or "Error with ollama run."
#         if not answer:
#             return False, "Model gave an empty answer"
#         return True, answer
#     finally:
#         if on_process:
#             on_process(None)


def _get_settings_path() -> str:
    """Get path to settings file."""
    base = os.path.dirname(__file__) or "."
    return os.path.join(base, "settings.json")


def _load_settings_from_file() -> Dict[str, object]:
    try:
        path = _get_settings_path()
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        pass
    return {}


def _save_settings_to_file(settings: Dict[str, object]) -> None:
    try:
        path = _get_settings_path()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


class ChatInputBox(QTextEdit):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.on_send_requested: Optional[Callable[[], None]] = None
    
    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Return:
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                if self.on_send_requested:
                    self.on_send_requested()
                event.accept()
        else:
            super().keyPressEvent(event)


class ChatBubble(QFrame):
    def __init__(self, role: str, text: str) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("bubble")
        self._apply_style(role)

        layout = QVBoxLayout()
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label.setObjectName("bubbleText")
        layout.addWidget(self.label)
        self.setLayout(layout)

    def _apply_style(self, role: str) -> None:
        if role == "user":
            self.setStyleSheet(
                """
                #bubble { background: rgba(110, 231, 183, 0.14); border: 1px solid rgba(110, 231, 183, 0.35); border-radius: 16px; }
                #bubbleText { color: #EAFBF3; font-size: 14px; }
                """
            )
        elif role == "notice":
            self.setStyleSheet(
                """
                #bubble { background: rgba(255, 255, 255, 0.03); border: 1px dashed rgba(234, 242, 255, 0.22); border-radius: 16px; }
                #bubbleText { color: rgba(234, 242, 255, 0.76); font-size: 13px; }
                """
            )
        else:
            self.setStyleSheet(
                """
                #bubble { background: rgba(59, 130, 246, 0.14); border: 1px solid rgba(59, 130, 246, 0.35); border-radius: 16px; }
                #bubbleText { color: #EAF2FF; font-size: 14px; }
                """
            )

    def set_text(self, text: str) -> None:
        self.label.setText(text)


class MainWindow(QMainWindow):
    models_loaded = pyqtSignal(object, object)
    pull_finished = pyqtSignal(str, bool, str)
    pull_progress = pyqtSignal(int, str)
    chat_finished = pyqtSignal(bool, str)
    chat_stream_chunk = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()

        self.setAcceptDrops(True)
        self._chat_history: List[Dict[str, object]] = []
        self._max_context_messages = 12
        self._pending_attachments: List[str] = []
        self._empty_chat_label: Optional[QLabel] = None
        self._suppress_history = False
        self._stream_bubble: Optional[ChatBubble] = None
        self._stream_text: str = ""
        self._stream_cursor_visible = True
        self._stream_cursor_timer = QTimer(self)
        self._stream_cursor_timer.setInterval(420)
        self._stream_cursor_timer.timeout.connect(self._on_stream_cursor_tick)
        self._active_chat_proc: Optional[object] = None
        self._chat_proc_lock = threading.Lock()
        self._chat_stop_requested = False
        self.setWindowTitle("LocalDesk")
        self.setMinimumSize(980, 700)

        self.hardware_bridge = HardwareCheckBridge()
        self._recommended_model_id: str = ""
        self._side_mode: Optional[str] = "model"
        self._chat_fullscreen_active = False
        
        self._username: str = ""
        self._custom_system_prompts: List[str] = []
        self._load_settings()
        
        self._init_ui()

        self.hardware_bridge.snapshot_ready.connect(self._on_hardware_ready)
        self.models_loaded.connect(self._on_models_loaded)
        self.pull_finished.connect(self._on_pull_finished)
        self.pull_progress.connect(self._on_pull_progress)
        self.chat_finished.connect(self._on_chat_finished)
        self.chat_stream_chunk.connect(self._on_chat_stream_chunk)
        self._set_hardware_loading()
        self.hardware_bridge.start()
        self._refresh_model_list_async()
        self._refresh_saved_chats()

    def _init_ui(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                            stop:0 #0B1020, stop:0.45 #0B1228, stop:1 #070A14);
                color: #EAF2FF;
            }
            QLabel { color: #EAF2FF; }
            QPushButton {
                border-radius: 12px;
                padding: 10px 14px;
                font-weight: 600;
            }
            QPushButton#primary {
                background: rgba(59, 130, 246, 0.18);
                border: 1px solid rgba(59, 130, 246, 0.40);
            }
            QPushButton#primary:hover { background: rgba(59, 130, 246, 0.26); }
            QPushButton#ghost {
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.10);
            }
            QPushButton#ghost:hover { background: rgba(255, 255, 255, 0.07); }
            QTextEdit, QScrollArea {
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 16px;
            }
            QTextEdit {
                padding: 12px;
                color: #EAF2FF;
                background: rgba(255, 255, 255, 0.04);
                selection-background-color: rgba(59, 130, 246, 0.35);
            }
            QScrollArea { background: transparent; border: none; }
            QScrollArea QWidget { background: transparent; }
            QFrame#card {
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 18px;
            }
            QFrame#sideCard {
                background: rgba(255, 255, 255, 0.03);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 18px;
            }
            """
        )

        root = QWidget()
        root_layout = QVBoxLayout()
        root_layout.setContentsMargins(18, 18, 18, 18)
        root_layout.setSpacing(14)
        root.setLayout(root_layout)
        self.setCentralWidget(root)

        header = QFrame()
        header.setObjectName("card")
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(18, 16, 18, 16)
        header_layout.setSpacing(14)
        header.setLayout(header_layout)

        title = QLabel("LocalDesk")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setBold(True)
        title.setFont(title_font)

        left_header = QVBoxLayout()
        left_header.setContentsMargins(0, 0, 0, 0)
        left_header.setSpacing(6)
        left_header.addWidget(title)

        self.btn_nav_chats = QPushButton("Chats")
        self.btn_nav_chats.setObjectName("ghost")
        self.btn_nav_chats.setCheckable(True)
        self.btn_nav_chats.clicked.connect(self._on_nav_chats_clicked)

        self.btn_nav_model = QPushButton("Model")
        self.btn_nav_model.setObjectName("ghost")
        self.btn_nav_model.setCheckable(True)
        self.btn_nav_model.clicked.connect(self._on_nav_model_clicked)
        self.btn_nav_model.setChecked(True)

        self.btn_nav_settings = QPushButton("Settings")
        self.btn_nav_settings.setObjectName("ghost")
        self.btn_nav_settings.setCheckable(True)
        self.btn_nav_settings.clicked.connect(self._on_nav_settings_clicked)

        self.btn_save_chat = QPushButton("Save chat")
        self.btn_save_chat.setObjectName("ghost")
        self.btn_save_chat.clicked.connect(self._save_current_chat)

        header_layout.addLayout(left_header, 1)
        header_layout.addWidget(self.btn_nav_chats, 0)
        header_layout.addWidget(self.btn_nav_model, 0)
        header_layout.addWidget(self.btn_nav_settings, 0)
        header_layout.addWidget(self.btn_save_chat, 0)
        self._header_frame = header
        root_layout.addWidget(header)

        main = QHBoxLayout()
        main.setSpacing(14)

        side = QFrame()
        side.setObjectName("sideCard")
        side.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        side.setMinimumWidth(252)
        side.setMaximumWidth(300)
        side_layout = QVBoxLayout()
        side_layout.setContentsMargins(16, 16, 16, 16)
        side_layout.setSpacing(12)
        side.setLayout(side_layout)

        input_style = """
            QLineEdit#input {
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 12px;
                padding: 10px 12px;
                color: #EAF2FF;
            }
            QLineEdit#input:focus {
                border: 1px solid rgba(59, 130, 246, 0.55);
            }
            """

        self._side_stack = QStackedWidget()
        side_layout.addWidget(self._side_stack, 1)

        model_page = QWidget()
        model_layout = QVBoxLayout()
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.setSpacing(12)
        model_page.setLayout(model_layout)

        self.ollama_host_input = QLineEdit("http://localhost:11434")
        self.ollama_host_input.setObjectName("input")
        self.ollama_host_input.setStyleSheet(input_style)

        self.ollama_model_input = QLineEdit("llama3")
        self.ollama_model_input.setObjectName("input")
        self.ollama_model_input.setStyleSheet(input_style)

        rec_title = QLabel("Model recommendation")
        rec_title.setStyleSheet("font-weight: 700; color: rgba(234, 242, 255, 0.95);")
        model_layout.addWidget(rec_title)

        self.recommendation_body = QLabel("Analysing your PC and selecting model...")
        self.recommendation_body.setStyleSheet("color: rgba(234, 242, 255, 0.72); font-size: 13px;")
        self.recommendation_body.setWordWrap(True)
        model_layout.addWidget(self.recommendation_body)

        self.btn_apply_recommendation = QPushButton('Apply to the "Model" field')
        self.btn_apply_recommendation.setObjectName("primary")
        self.btn_apply_recommendation.setEnabled(False)
        self.btn_apply_recommendation.clicked.connect(self._apply_recommended_model)
        model_layout.addWidget(self.btn_apply_recommendation)

        ollama_title = QLabel("Ollama settings")
        ollama_title.setStyleSheet("font-weight: 700; color: rgba(234, 242, 255, 0.95);")
        model_layout.addWidget(ollama_title)

        host_label = QLabel("Base URL")
        host_label.setStyleSheet("color: rgba(234, 242, 255, 0.70); font-size: 12px;")
        model_layout.addWidget(host_label)
        model_layout.addWidget(self.ollama_host_input)

        model_label = QLabel("Model")
        model_label.setStyleSheet("color: rgba(234, 242, 255, 0.70); font-size: 12px;")
        model_layout.addWidget(model_label)
        model_row = QHBoxLayout()
        model_row.setSpacing(8)
        self.ollama_model_combo = QComboBox()
        self.ollama_model_combo.setStyleSheet(
            """
            QComboBox {
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 12px;
                padding: 8px 10px;
                color: #EAF2FF;
            }
            QComboBox::drop-down { border: none; width: 22px; }
            QComboBox QAbstractItemView {
                background: #0B1228;
                color: #EAF2FF;
                border: 1px solid rgba(255, 255, 255, 0.10);
                selection-background-color: rgba(59, 130, 246, 0.35);
            }
            """
        )
        self.ollama_model_combo.currentTextChanged.connect(self._on_model_combo_changed)
        self.btn_refresh_models = QPushButton("Refresh")
        self.btn_refresh_models.setObjectName("ghost")
        self.btn_refresh_models.clicked.connect(self._refresh_model_list_async)
        model_row.addWidget(self.ollama_model_combo, 1)
        model_row.addWidget(self.btn_refresh_models, 0)
        model_layout.addLayout(model_row)
        model_layout.addWidget(self.ollama_model_input)

        self.btn_pull_model = QPushButton("Download model")
        self.btn_pull_model.setObjectName("primary")
        self.btn_pull_model.clicked.connect(self._pull_selected_model_async)
        model_layout.addWidget(self.btn_pull_model)

        self.pull_progress_label = QLabel("")
        self.pull_progress_label.setStyleSheet("color: rgba(234, 242, 255, 0.72); font-size: 12px;")
        self.pull_progress_label.setWordWrap(True)
        self.pull_progress_label.setVisible(False)
        model_layout.addWidget(self.pull_progress_label)

        self.pull_progress_bar = QProgressBar()
        self.pull_progress_bar.setRange(0, 100)
        self.pull_progress_bar.setValue(0)
        self.pull_progress_bar.setTextVisible(True)
        self.pull_progress_bar.setVisible(False)
        self.pull_progress_bar.setStyleSheet(
            """
            QProgressBar {
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 10px;
                color: #EAF2FF;
                text-align: center;
                padding: 2px;
            }
            QProgressBar::chunk {
                background: rgba(59, 130, 246, 0.55);
                border-radius: 8px;
            }
            """
        )
        model_layout.addWidget(self.pull_progress_bar)

        temp_label = QLabel("Temperature")
        temp_label.setStyleSheet("color: rgba(234, 242, 255, 0.70); font-size: 12px;")
        self.temperature_slider = QSlider(Qt.Orientation.Horizontal)
        self.temperature_slider.setRange(0, 200)  # 0.0 .. 2.0
        self.temperature_slider.setValue(120)
        self.temperature_slider.setStyleSheet(
            """
            QSlider::groove:horizontal {
                height: 6px;
                background: rgba(255, 255, 255, 0.10);
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                width: 18px;
                height: 18px;
                background: rgba(59, 130, 246, 0.55);
                margin-top: -6px;
                border-radius: 9px;
            }
            """
        )
        self.temperature_value_label = QLabel("1.20")
        self.temperature_value_label.setStyleSheet(
            "color: rgba(234, 242, 255, 0.85); font-weight: 700;"
        )
        
        self._temperature_use_default = True

        def on_temp_changed(v: int) -> None:
            self.temperature_value_label.setText(f"{v/100:.2f}")
            self._temperature_use_default = False

        self.temperature_slider.valueChanged.connect(on_temp_changed)
        on_temp_changed(self.temperature_slider.value())

        self.btn_temp_default = QPushButton("Default")
        self.btn_temp_default.setObjectName("ghost")
        self.btn_temp_default.clicked.connect(self._reset_temperature_to_default)
        
        temp_row = QHBoxLayout()
        temp_row.setSpacing(10)
        temp_row.addWidget(self.temperature_slider, 1)
        temp_row.addWidget(self.temperature_value_label, 0)
        temp_row.addWidget(self.btn_temp_default, 0)
        model_layout.addWidget(temp_label)
        model_layout.addLayout(temp_row)

        model_layout.addItem(QSpacerItem(20, 1, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))

        hint = QLabel("Made by pechenka")
        hint.setStyleSheet("color: rgba(234, 242, 255, 0.55); font-size: 12px;")
        hint.setWordWrap(True)
        model_layout.addWidget(hint)

        chats_page = QWidget()
        chats_layout = QVBoxLayout()
        chats_layout.setContentsMargins(0, 0, 0, 0)
        chats_layout.setSpacing(8)
        chats_page.setLayout(chats_layout)
        chats_title = QLabel("Saved chats")
        chats_title.setStyleSheet("font-weight: 700; color: rgba(234, 242, 255, 0.95);")
        chats_layout.addWidget(chats_title)

        self._saved_chats_list = QListWidget()
        chats_layout.addWidget(self._saved_chats_list, 1)

        btns_row = QHBoxLayout()
        self.btn_open_chat = QPushButton("Open")
        self.btn_open_chat.setObjectName("ghost")
        self.btn_open_chat.clicked.connect(self._open_selected_saved_chat)
        self.btn_delete_chat = QPushButton("Delete")
        self.btn_delete_chat.setObjectName("ghost")
        self.btn_delete_chat.clicked.connect(self._delete_selected_saved_chat)
        self.btn_refresh_chats = QPushButton("Refresh")
        self.btn_refresh_chats.setObjectName("ghost")
        self.btn_refresh_chats.clicked.connect(self._refresh_saved_chats)
        btns_row.addWidget(self.btn_open_chat)
        btns_row.addWidget(self.btn_delete_chat)
        btns_row.addStretch(1)
        btns_row.addWidget(self.btn_refresh_chats)
        chats_layout.addLayout(btns_row)

        self._saved_chats_list.itemDoubleClicked.connect(lambda it: self._open_selected_saved_chat())
        chats_layout.addItem(QSpacerItem(20, 1, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))

        settings_page = QWidget()
        settings_layout = QVBoxLayout()
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(12)
        settings_page.setLayout(settings_layout)
        
        settings_title = QLabel("Settings")
        settings_title.setStyleSheet("font-weight: 700; color: rgba(234, 242, 255, 0.95);")
        settings_layout.addWidget(settings_title)
        
        username_label = QLabel("Your name")
        username_label.setStyleSheet("color: rgba(234, 242, 255, 0.70); font-size: 12px;")
        settings_layout.addWidget(username_label)
        self.username_input = QLineEdit()
        self.username_input.setObjectName("input")
        self.username_input.setStyleSheet(input_style)
        self.username_input.setPlaceholderText("e.g., John")
        self.username_input.setText(self._username)
        self.username_input.textChanged.connect(lambda text: setattr(self, '_username', text))
        settings_layout.addWidget(self.username_input)
        
        context_label = QLabel("Chat history (messages to remember)")
        context_label.setStyleSheet("color: rgba(234, 242, 255, 0.70); font-size: 12px;")
        settings_layout.addWidget(context_label)
        
        context_row = QHBoxLayout()
        context_row.setSpacing(10)
        self.context_messages_spin = QSpinBox()
        self.context_messages_spin.setRange(1, 50)
        self.context_messages_spin.setValue(self._max_context_messages)
        self.context_messages_spin.valueChanged.connect(lambda v: setattr(self, '_max_context_messages', v))
        self.context_messages_spin.setStyleSheet(
            """
            QSpinBox {
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 8px;
                padding: 6px 10px;
                color: #EAF2FF;
            }
            """
        )
        context_row.addWidget(self.context_messages_spin, 0)
        context_row.addStretch(1)
        settings_layout.addLayout(context_row)
        
        # System prompts section
        system_label = QLabel("System prompts")
        system_label.setStyleSheet("color: rgba(234, 242, 255, 0.70); font-size: 12px;")
        settings_layout.addWidget(system_label)
        
        system_info = QLabel("Standard prompts (read-only):")
        system_info.setStyleSheet("color: rgba(234, 242, 255, 0.60); font-size: 11px; font-style: italic;")
        settings_layout.addWidget(system_info)
        
        default_prompts_text = """• You are a helpful assistant.
• Answer directly.
• Do NOT mention memory or chat history too intrusively.
• Answer in the user's language."""
        default_prompts_label = QLabel(default_prompts_text)
        default_prompts_label.setStyleSheet("color: rgba(234, 242, 255, 0.65); font-size: 11px; margin-left: 10px;")
        default_prompts_label.setWordWrap(True)
        settings_layout.addWidget(default_prompts_label)
        
        custom_info = QLabel("Custom prompts (add your own):")
        custom_info.setStyleSheet("color: rgba(234, 242, 255, 0.70); font-size: 12px; margin-top: 8px;")
        settings_layout.addWidget(custom_info)
        
        self.custom_prompts_list = QListWidget()
        self.custom_prompts_list.setMaximumHeight(120)
        self.custom_prompts_list.setStyleSheet(
            """
            QListWidget {
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 8px;
                color: #EAF2FF;
            }
            QListWidget::item { padding: 4px; }
            """
        )
        self._refresh_custom_prompts_list()
        settings_layout.addWidget(self.custom_prompts_list)
        
        prompts_buttons = QHBoxLayout()
        prompts_buttons.setSpacing(6)
        
        self.btn_add_prompt = QPushButton("Add prompt")
        self.btn_add_prompt.setObjectName("ghost")
        self.btn_add_prompt.clicked.connect(self._on_add_custom_prompt)
        
        self.btn_remove_prompt = QPushButton("Remove")
        self.btn_remove_prompt.setObjectName("ghost")
        self.btn_remove_prompt.clicked.connect(self._on_remove_custom_prompt)
        
        prompts_buttons.addWidget(self.btn_add_prompt)
        prompts_buttons.addWidget(self.btn_remove_prompt)
        prompts_buttons.addStretch(1)
        settings_layout.addLayout(prompts_buttons)
        
        self.btn_save_settings = QPushButton("Save settings")
        self.btn_save_settings.setObjectName("primary")
        self.btn_save_settings.clicked.connect(self._on_save_settings_clicked)
        settings_layout.addWidget(self.btn_save_settings)
        
        settings_layout.addItem(QSpacerItem(20, 1, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))

        self._side_stack.addWidget(model_page)
        self._side_stack.addWidget(chats_page)
        self._side_stack.addWidget(settings_page)

        self._side_panel = side

        chat_card = QFrame()
        chat_card.setObjectName("card")
        chat_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        chat_layout = QVBoxLayout()
        chat_layout.setContentsMargins(16, 16, 16, 16)
        chat_layout.setSpacing(12)
        chat_card.setLayout(chat_layout)

        chat_head = QHBoxLayout()
        chat_head.setSpacing(12)
        chat_title = QLabel("Chat")
        chat_title.setStyleSheet("font-weight: 700;")
        chat_head.addWidget(chat_title, 0, Qt.AlignmentFlag.AlignVCenter)
        chat_head.addStretch(1)
        self.btn_chat_fullscreen = QPushButton("Fullscreen chat")
        self.btn_chat_fullscreen.setObjectName("ghost")
        self.btn_chat_fullscreen.clicked.connect(self._toggle_chat_full_window)
        chat_head.addWidget(self.btn_chat_fullscreen, 0, Qt.AlignmentFlag.AlignVCenter)
        chat_layout.addLayout(chat_head)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.chat_container = QWidget()
        self.chat_vbox = QVBoxLayout()
        self.chat_vbox.setContentsMargins(0, 0, 0, 0)
        self.chat_vbox.setSpacing(10)
        self._chat_bottom_spacer = QSpacerItem(
            20, 1, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding
        )
        self._empty_chat_label = QLabel("Start a conversation by typing a message below.")
        self._empty_chat_label.setWordWrap(True)
        self._empty_chat_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._empty_chat_label.setStyleSheet(
            "color: rgba(234, 242, 255, 0.55); font-size: 13px; padding: 10px;"
        )
        self.chat_vbox.addWidget(self._empty_chat_label)
        self.chat_vbox.addItem(self._chat_bottom_spacer)
        self.chat_container.setLayout(self.chat_vbox)
        self.scroll_area.setWidget(self.chat_container)
        self.scroll_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        chat_layout.addWidget(self.scroll_area, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        bottom.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.input_box = ChatInputBox()
        self.input_box.on_send_requested = self._on_send
        self.input_box.setPlaceholderText("Ask anything...")
        self.input_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.input_box.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.input_box.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.input_box.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)

        self._btn_send = QPushButton("Send")
        self._btn_send.setObjectName("primary")
        self._btn_send.clicked.connect(self._on_send)
        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setObjectName("ghost")
        self._btn_stop.clicked.connect(self._on_stop_generation)
        self._btn_stop.setEnabled(False)

        self._btn_upload = QPushButton("Upload a file")
        self._btn_upload.setObjectName("ghost")
        self._btn_upload.clicked.connect(self._on_upload_clicked)

        self._btn_clear = QPushButton("Clear chat")
        self._btn_clear.setObjectName("ghost")
        self._btn_clear.clicked.connect(self._on_clear)

        self._buttons_col_gap = 10
        buttons_col = QVBoxLayout()
        buttons_col.setSpacing(self._buttons_col_gap)
        buttons_col.setContentsMargins(0, 0, 0, 0)
        buttons_col.setAlignment(Qt.AlignmentFlag.AlignTop)
        buttons_col.addWidget(self._btn_send)
        buttons_col.addWidget(self._btn_stop)
        buttons_col.addWidget(self._btn_upload)
        buttons_col.addWidget(self._btn_clear)

        bottom.addWidget(self.input_box, 1, Qt.AlignmentFlag.AlignTop)
        bottom.addLayout(buttons_col, 0)

        chat_layout.addLayout(bottom)

        main.addWidget(side, 0)
        main.addWidget(chat_card, 1)
        main.setStretch(0, 0)
        main.setStretch(1, 1)
        root_layout.addLayout(main, 1)

        self._apply_side_mode()
        self._fit_prompt_to_buttons()

    def _load_settings(self) -> None:
        """Load settings from file."""
        settings = _load_settings_from_file()
        self._username = settings.get("username", "")
        self._max_context_messages = int(settings.get("max_context_messages", 12))
        self._custom_system_prompts = settings.get("custom_system_prompts", [])
        if not isinstance(self._custom_system_prompts, list):
            self._custom_system_prompts = []

    def _save_settings(self) -> None:
        """Save settings to file."""
        settings = {
            "username": self._username,
            "max_context_messages": self._max_context_messages,
            "custom_system_prompts": self._custom_system_prompts,
        }
        _save_settings_to_file(settings)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._fit_prompt_to_buttons)

    def _fit_prompt_to_buttons(self) -> None:
        gap = getattr(self, "_buttons_col_gap", 10)
        btns = [self._btn_send, self._btn_stop]
        if hasattr(self, "_btn_upload"):
            btns.append(self._btn_upload)
        btns.append(self._btn_clear)
        heights = []
        for b in btns:
            h = b.sizeHint().height()
            if getattr(b, "height", lambda: 0)() > 0:
                h = b.height()
            heights.append(h)
        total = sum(heights) + gap * max(0, len(heights) - 1)
        self.input_box.setFixedHeight(max(total, 64))

    def _set_hardware_loading(self) -> None:
        self.recommendation_body.setText("Analysing your PC and selecting model")
        self.btn_apply_recommendation.setEnabled(False)

    def _on_hardware_ready(self, payload: object) -> None:
        if not isinstance(payload, HardwareCheckPayload):
            return
        self._recommended_model_id = payload.recommendation.model_id
        self.ollama_model_input.setText(payload.recommendation.model_id)
        self.recommendation_body.setText(
            f"{payload.recommendation.model_id}\n\n{payload.recommendation.rationale}"
        )
        self.btn_apply_recommendation.setEnabled(bool(self._recommended_model_id))
        self._select_model_in_combo(payload.recommendation.model_id)

    def _on_nav_model_clicked(self) -> None:
        if self.btn_nav_model.isChecked():
            self.btn_nav_chats.setChecked(False)
            self._side_mode = "model"
        else:
            self._side_mode = None
        self._apply_side_mode()

    def _on_nav_chats_clicked(self) -> None:
        if self.btn_nav_chats.isChecked():
            self.btn_nav_model.setChecked(False)
            self.btn_nav_settings.setChecked(False)
            self._side_mode = "chats"
        else:
            self._side_mode = None
        self._apply_side_mode()

    def _on_nav_settings_clicked(self) -> None:
        if self.btn_nav_settings.isChecked():
            self.btn_nav_model.setChecked(False)
            self.btn_nav_chats.setChecked(False)
            self._side_mode = "settings"
        else:
            self._side_mode = None
        self._apply_side_mode()

    def _apply_side_mode(self) -> None:
        if self._side_mode == "model":
            self._side_panel.setVisible(True)
            self._side_stack.setCurrentIndex(0)
            self.btn_nav_model.setChecked(True)
            self.btn_nav_chats.setChecked(False)
            self.btn_nav_settings.setChecked(False)
        elif self._side_mode == "chats":
            self._side_panel.setVisible(True)
            self._side_stack.setCurrentIndex(1)
            self.btn_nav_model.setChecked(False)
            self.btn_nav_chats.setChecked(True)
            self.btn_nav_settings.setChecked(False)
        elif self._side_mode == "settings":
            self._side_panel.setVisible(True)
            self._side_stack.setCurrentIndex(2)
            self.btn_nav_model.setChecked(False)
            self.btn_nav_chats.setChecked(False)
            self.btn_nav_settings.setChecked(True)
        else:
            self._side_panel.setVisible(False)
            self.btn_nav_model.setChecked(False)
            self.btn_nav_chats.setChecked(False)
            self.btn_nav_settings.setChecked(False)

    def _apply_recommended_model(self) -> None:
        if self._recommended_model_id:
            self.ollama_model_input.setText(self._recommended_model_id)
            self._select_model_in_combo(self._recommended_model_id)

    def _on_model_combo_changed(self, model_name: str) -> None:
        model_name = model_name.strip()
        if model_name:
            self.ollama_model_input.setText(model_name)

    def _reset_temperature_to_default(self) -> None:
        """Reset temperature to default (120 = 1.20) and mark as using default."""
        self.temperature_slider.blockSignals(True)
        self.temperature_slider.setValue(120)
        self.temperature_slider.blockSignals(False)
        self.temperature_value_label.setText("1.20")
        self._temperature_use_default = True
        self._set_status_message("Temperature reset to default (1.20)")

    def _refresh_custom_prompts_list(self) -> None:
        """Update custom prompts list display."""
        self.custom_prompts_list.clear()
        for prompt in self._custom_system_prompts:
            self.custom_prompts_list.addItem(prompt[:60] + "..." if len(prompt) > 60 else prompt)

    def _on_add_custom_prompt(self) -> None:
        """Add a new custom system prompt."""
        text, ok = QInputDialog.getMultiLineText(self, "Add Custom Prompt", "Enter your custom system prompt:")
        if ok and text.strip():
            prompt = text.strip()
            if prompt not in self._custom_system_prompts:
                self._custom_system_prompts.append(prompt)
                self._refresh_custom_prompts_list()
                self._set_status_message("Custom prompt added")
            else:
                self._set_status_message("This prompt already exists")

    def _on_remove_custom_prompt(self) -> None:
        """Remove selected custom prompt."""
        current_row = self.custom_prompts_list.currentRow()
        if current_row >= 0 and current_row < len(self._custom_system_prompts):
            del self._custom_system_prompts[current_row]
            self._refresh_custom_prompts_list()
            self._set_status_message("Custom prompt removed")

    def _on_save_settings_clicked(self) -> None:
        """Save all settings to file."""
        self._save_settings()
        self._set_status_message("Settings saved successfully")

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            for u in urls:
                path = u.toLocalFile()
                if path and self._is_allowed_file(path):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            for u in event.mimeData().urls():
                path = u.toLocalFile()
                if path and self._is_allowed_file(path):
                    self._handle_uploaded_file(path)
        event.acceptProposedAction()

    def _is_allowed_file(self, path: str) -> bool:
        ext = os.path.splitext(path)[1].lower()
        return ext in (".txt", ".py", ".png", ".jpg", ".jpeg")

    def _on_upload_clicked(self) -> None:
        filters = "Text/Code/Images (*.txt *.py *.png *.jpg *.jpeg)"
        files, _ = QFileDialog.getOpenFileNames(self, "Select files", os.path.expanduser("~"), filters)
        for f in files:
            if f:
                if not self._is_allowed_file(f):
                    self._set_status_message(f"File `{os.path.basename(f)}` has not a valid type")
                    continue
                self._attach_file_to_next_message(f)

    def _attach_file_to_next_message(self, path: str) -> None:
        if not os.path.isfile(path):
            self._set_status_message(f"File `{os.path.basename(path)}` not found")
            return
        if not self._is_allowed_file(path):
            self._set_status_message(f"File `{os.path.basename(path)}` is not supported.")
            return
        if path not in self._pending_attachments:
            self._pending_attachments.append(path)
        names = ", ".join(os.path.basename(p) for p in self._pending_attachments[-4:])
        more = "" if len(self._pending_attachments) <= 4 else f" (+{len(self._pending_attachments) - 4} more)"
        self._set_status_message(f"Attached to next message: {names}{more}")
        self._append_notice(f"Attached file to next message: `{os.path.basename(path)}`")

    def _handle_uploaded_file(self, path: str) -> None:
        self._attach_file_to_next_message(path)

    def _append_custom_widget(self, widget: QWidget) -> None:
        container = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(widget, 0)
        container.setLayout(layout)
        insert_at = self.chat_vbox.count()
        if insert_at == self.chat_vbox.count():
            self.chat_vbox.addWidget(container)
        else:
            self.chat_vbox.insertWidget(insert_at, container)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _select_model_in_combo(self, model_name: str) -> None:
        if not hasattr(self, "ollama_model_combo"):
            return
        idx = self.ollama_model_combo.findText(model_name)
        if idx >= 0:
            self.ollama_model_combo.setCurrentIndex(idx)

    def _ocr_image(self, path: str) -> Tuple[bool, str]:
        try:
            from PIL import Image
            import pytesseract

            try:
                img = Image.open(path)
                text = pytesseract.image_to_string(img)
                return True, text.strip() or "(empty OCR result)"
            except Exception as e:
                return False, f"pytesseract error: {e}"
        except Exception:
            if shutil.which("tesseract"):
                try:
                    proc = subprocess.run(["tesseract", path, "stdout"], capture_output=True, text=True, check=False)
                    if proc.returncode != 0:
                        return False, (proc.stderr or proc.stdout or "tesseract error").strip()
                    out = (proc.stdout or "").strip()
                    return True, out or "(empty OCR result)"
                except Exception as e:
                    return False, f"tesseract CLI error: {e}"
            return False, "Text OCR backend (pytesseract/tesseract) not found. Install pytesseract and system tesseract CLI."

    def _refresh_model_list_async(self) -> None:
        self.btn_refresh_models.setEnabled(False)
        self.btn_refresh_models.setText("Refreshing...")

        def worker() -> None:
            host_url = self.ollama_host_input.text().strip()
            if not host_url:
                host_url = "http://localhost:11434"
            models, error = list_local_ollama_models_api(host_url)
            self.models_loaded.emit(models, error)

        threading.Thread(target=worker, daemon=True).start()

    def _on_models_loaded(self, models_obj: object, error_obj: object) -> None:
        self.btn_refresh_models.setEnabled(True)
        self.btn_refresh_models.setText("Refresh")

        models = models_obj if isinstance(models_obj, list) else []
        error = str(error_obj) if isinstance(error_obj, str) else None

        current_text = self.ollama_model_input.text().strip()
        self.ollama_model_combo.blockSignals(True)
        self.ollama_model_combo.clear()
        self.ollama_model_combo.addItems(models)
        self.ollama_model_combo.blockSignals(False)

        if current_text:
            self._select_model_in_combo(current_text)

        if error:
            self._set_status_message(f"Can't get model list: {error}")
        elif not models:
            self._set_status_message("Ollama models not found. Type a model name and click 'Download model' or check models you installed.")

    def _pull_selected_model_async(self) -> None:
        model = self.ollama_model_input.text().strip()
        if not model:
            self._set_status_message("Select a model to install")
            return

        host_url = self.ollama_host_input.text().strip()
        if not host_url:
            host_url = "http://localhost:11434"

        existing = [self.ollama_model_combo.itemText(i) for i in range(self.ollama_model_combo.count())]
        if model in existing:
            self._set_status_message(f"`{model}` already installed")
            return

        self.btn_pull_model.setEnabled(False)
        self.btn_pull_model.setText("Downloading...")
        self.pull_progress_label.setVisible(True)
        self.pull_progress_label.setText(" downloading")
        self.pull_progress_bar.setVisible(True)
        self.pull_progress_bar.setRange(0, 0)
        self._set_status_message(f"Downloading model {model}...")

        def worker() -> None:
            def on_progress(percent: int, line: str) -> None:
                self.pull_progress.emit(percent, line)

            ok, msg = pull_ollama_model_api(host_url, model, on_progress=on_progress)
            self.pull_finished.emit(model, ok, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_pull_progress(self, percent: int, line: str) -> None:
        if percent >= 0:
            if self.pull_progress_bar.minimum() == 0 and self.pull_progress_bar.maximum() == 0:
                self.pull_progress_bar.setRange(0, 100)
            self.pull_progress_bar.setValue(percent)
        cleaned = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", line)
        cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", cleaned)
        cleaned = re.sub(r"\[\?[0-9;]*[hl]", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        self.pull_progress_label.setText(cleaned[:220])

    def _on_pull_finished(self, model_name: str, ok: bool, message: str) -> None:
        self.btn_pull_model.setEnabled(True)
        self.btn_pull_model.setText("Download model")
        if ok:
            self.pull_progress_bar.setRange(0, 100)
            self.pull_progress_bar.setValue(100)
            self.pull_progress_label.setText("Download completed")
        else:
            self.pull_progress_label.setText("Error while downloading")
        if ok:
            self._set_status_message(f"Model `{model_name}` has been installed")
            self._refresh_model_list_async()
            self.ollama_model_input.setText(model_name)
        else:
            self._set_status_message(f"Error while downloading: `{model_name}`:\n{message}")

    def _toggle_chat_full_window(self) -> None:
        self._chat_fullscreen_active = not self._chat_fullscreen_active
        show_chrome = not self._chat_fullscreen_active
        self._header_frame.setVisible(show_chrome)
        if show_chrome:
            self._apply_side_mode()
        else:
            self._side_panel.setVisible(False)
        self.btn_chat_fullscreen.setText(
            "Show panel" if self._chat_fullscreen_active else "Fullscreen chat"
        )

    def _hide_empty_chat_placeholder(self) -> None:
        if self._empty_chat_label is not None and self._empty_chat_label.isVisible():
            self._empty_chat_label.setVisible(False)

    def _show_empty_chat_placeholder(self) -> None:
        if self._empty_chat_label is not None:
            self._empty_chat_label.setVisible(True)

    def _append_message(self, role: str, text: str, *, record_history: bool = True) -> None:
        self._hide_empty_chat_placeholder()
        bubble = ChatBubble(role, text)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        if role == "user":
            row.addSpacerItem(QSpacerItem(20, 1, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))
            row.addWidget(bubble, alignment=Qt.AlignmentFlag.AlignRight)
        else:
            row.addWidget(bubble, alignment=Qt.AlignmentFlag.AlignLeft)
            row.addSpacerItem(QSpacerItem(20, 1, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))

        container = QWidget()
        container.setLayout(row)
        container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        insert_at = self.chat_vbox.count()
        if self.chat_vbox.count() > 0:
            last_item = self.chat_vbox.itemAt(self.chat_vbox.count() - 1)
            if last_item and last_item.spacerItem() is not None and last_item.widget() is None:
                insert_at = self.chat_vbox.count() - 1

        if insert_at == self.chat_vbox.count():
            self.chat_vbox.addWidget(container)
        else:
            self.chat_vbox.insertWidget(insert_at, container)

        QTimer.singleShot(0, self._scroll_to_bottom)
        if record_history and not getattr(self, "_suppress_history", False):
            try:
                self._chat_history.append({"role": role, "text": text, "ts": datetime.utcnow().isoformat()})
            except Exception:
                pass

    def _append_notice(self, text: str) -> None:
        self._hide_empty_chat_placeholder()
        self._append_message("notice", text, record_history=False)

    def _start_streaming_assistant_message(self) -> None:
        bubble = ChatBubble("assistant", "...")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(bubble, alignment=Qt.AlignmentFlag.AlignLeft)
        row.addSpacerItem(QSpacerItem(20, 1, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum))

        container = QWidget()
        container.setLayout(row)
        container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        insert_at = self.chat_vbox.count()
        if self.chat_vbox.count() > 0:
            last_item = self.chat_vbox.itemAt(self.chat_vbox.count() - 1)
            if last_item and last_item.spacerItem() is not None and last_item.widget() is None:
                insert_at = self.chat_vbox.count() - 1

        if insert_at == self.chat_vbox.count():
            self.chat_vbox.addWidget(container)
        else:
            self.chat_vbox.insertWidget(insert_at, container)

        self._stream_bubble = bubble
        self._stream_text = ""
        self._stream_cursor_visible = True
        self._stream_cursor_timer.start()
        self._render_streaming_bubble()
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _on_chat_stream_chunk(self, chunk: str) -> None:
        self._stream_text += chunk
        self._render_streaming_bubble()
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _render_streaming_bubble(self) -> None:
        if self._stream_bubble is None:
            return
        cursor = "▌" if self._stream_cursor_visible else ""
        base = self._stream_text or ""
        shown = f"{base}{cursor}" if base else (cursor or "...")
        self._stream_bubble.set_text(shown)

    def _on_stream_cursor_tick(self) -> None:
        if self._stream_bubble is None:
            return
        self._stream_cursor_visible = not self._stream_cursor_visible
        self._render_streaming_bubble()

    def _set_active_chat_proc(self, proc: object) -> None:
        with self._chat_proc_lock:
            self._active_chat_proc = proc if proc is not None else None

    def _on_stop_generation(self) -> None:
        self._chat_stop_requested = True
        self._btn_stop.setEnabled(False)
        with self._chat_proc_lock:
            proc = self._active_chat_proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        self._set_status_message("Generation stopped, keeping partial answer")
        self._append_notice("Generation stopped.")

    def _scroll_to_bottom(self) -> None:
        bar = self.scroll_area.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _set_status_message(self, msg: str, timeout: int = 6000) -> None:
        try:
            self.statusBar().showMessage(msg, timeout)
        except Exception:
            pass
        try:
            print(msg)
        except Exception:
            pass

    def _clear_chat_widgets(self, *, show_placeholder: bool = True) -> None:
        self._stream_cursor_timer.stop()
        self._stream_bubble = None
        self._stream_text = ""
        self._stream_cursor_visible = False

        while True:
            item = self.chat_vbox.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is not None:
                w.setParent(None)
            child_layout = item.layout()
            if child_layout is not None:
                while child_layout.count() > 0:
                    child = child_layout.takeAt(0)
                    cw = child.widget()
                    if cw is not None:
                        cw.setParent(None)

        self._chat_bottom_spacer = QSpacerItem(
            20, 1, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding
        )
        if show_placeholder:
            self._show_empty_chat_placeholder()
            if self._empty_chat_label is not None:
                self.chat_vbox.addWidget(self._empty_chat_label)
        self.chat_vbox.addItem(self._chat_bottom_spacer)

    def _build_prompt_with_context(self, fallback_user_text: str) -> str:
        messages: List[Tuple[str, str]] = []
        try:
            for msg in self._chat_history:
                if not isinstance(msg, dict):
                    continue
                role_raw = msg.get("role")
                text_raw = msg.get("text")
                role = str(role_raw).strip().lower() if role_raw is not None else ""
                text = str(text_raw).strip() if text_raw is not None else ""
                if role not in ("user", "assistant") or not text:
                    continue
                messages.append((role, text))
        except Exception:
            messages = []

        if not messages:
            return fallback_user_text

        recent = messages[-max(1, int(self._max_context_messages)) :]
        lines: List[str] = [
            "System: You are a helpful assistant.",
            "System: Answer directly.",
            "System: Do NOT mention memory or chat history too intrusively, unless the user specifically asks for it.",
            "System: Answer the question in the language the user is communicating with you in.",
        ]
        
        for custom_prompt in self._custom_system_prompts:
            lines.append(f"System: {custom_prompt}")
        
        lines.append("")
        
        for role, text in recent:
            username = self._username if self._username else "User"
            speaker = username if role == "user" else "Assistant"
            lines.append(f"{speaker}: {text}")
        lines.append("")
        lines.append("Assistant:")
        return "\n".join(lines)

    def _on_send(self) -> None:
        text = self.input_box.toPlainText().strip()
        if not text:
            return
        model = self.ollama_model_input.text().strip()
        if not model:
            self._set_status_message("First, select a model")
            return
        
        host_url = self.ollama_host_input.text().strip()
        if not host_url:
            host_url = "http://localhost:11434"
        
        existing = [self.ollama_model_combo.itemText(i) for i in range(self.ollama_model_combo.count())]
        if existing:
            if model not in existing:
                self._set_status_message(
                    f"`{model}` not found in list of installed models, generation paused. Click 'Download model' or select another model"
                )
                return
        else:
            self._set_status_message("No local models found. Install or select a model. Generation paused.")
            return

        self.input_box.clear()
        attachments = list(self._pending_attachments)
        self._pending_attachments = []
        if attachments:
            attach_names = ", ".join(os.path.basename(p) for p in attachments)
            self._append_message("user", f"{text}\n\n[attachments: {attach_names}]")
        else:
            self._append_message("user", text)
        self._start_streaming_assistant_message()
        self._btn_send.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_send.setText("Generating")
        self._chat_stop_requested = False

        base_prompt = self._build_prompt_with_context(text)
        model_l = model.lower()
        is_llava = "llava" in model_l
        image_exts = (".png", ".jpg", ".jpeg")
        text_exts = (".txt", ".py")
        
        temperature: Optional[float] = None
        if not self._temperature_use_default:
            temperature = self.temperature_slider.value() / 100.0

        def worker() -> None:
            def on_chunk(chunk: str) -> None:
                self.chat_stream_chunk.emit(chunk)

            prompt = base_prompt
            run_image_paths: List[str] = []
            if attachments:
                parts: List[str] = [prompt, "", "Attached files context:"]
                for path in attachments:
                    name = os.path.basename(path)
                    ext = os.path.splitext(name)[1].lower()
                    if ext in text_exts:
                        try:
                            with open(path, "r", encoding="utf-8") as fh:
                                content = fh.read()
                            trimmed = content if len(content) <= 6000 else content[:6000] + "\n...[truncated]"
                            parts.append(f"\n[FILE: {name}]\n{trimmed}")
                        except Exception as e:
                            parts.append(f"\n[FILE: {name}] read error: {e}")
                    elif ext in image_exts:
                        if is_llava:
                            run_image_paths.append(path)
                            parts.append(f"\n[IMAGE: {name}] passed directly to vision model.")
                        else:
                            parts.append(
                                f"\n[IMAGE: {name}] attached, but this model is not a vision model "
                                "(no OCR is performed)."
                            )
                    else:
                        parts.append(f"\n[FILE: {name}] unsupported type.")
                parts.append("\nUse this context in your answer.")
                prompt = "\n".join(parts)

            ok, answer = run_ollama_chat_stream_api(
                host_url,
                model,
                prompt,
                image_paths=run_image_paths,
                on_chunk=on_chunk,
                on_process=self._set_active_chat_proc,
                temperature=temperature,
            )
            self.chat_finished.emit(ok, answer)

        threading.Thread(target=worker, daemon=True).start()

    def _save_current_chat(self) -> None:
        if not self._chat_history:
            self._set_status_message("Chat is empty, there is nothing to save")
            return
        base = os.path.dirname(__file__)
        chats_dir = os.path.join(base, "saved_chats")
        try:
            os.makedirs(chats_dir, exist_ok=True)
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            name = f"chat_{ts}.json"
            path = os.path.join(chats_dir, name)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"messages": self._chat_history}, fh, ensure_ascii=False, indent=2)
            self._set_status_message(f"Saved chat as {name}")
            self._refresh_saved_chats()
        except Exception as e:
            self._set_status_message(f"Error with saving chat {e}")

    def _refresh_saved_chats(self) -> None:
        base = os.path.dirname(__file__)
        chats_dir = os.path.join(base, "saved_chats")
        os.makedirs(chats_dir, exist_ok=True)
        files = [f for f in os.listdir(chats_dir) if f.endswith(".json")]
        files.sort(reverse=True)
        if hasattr(self, "_saved_chats_list"):
            self._saved_chats_list.clear()
            for f in files:
                self._saved_chats_list.addItem(f)

    def _open_selected_saved_chat(self) -> None:
        if not hasattr(self, "_saved_chats_list"):
            return
        item = self._saved_chats_list.currentItem()
        if not item:
            return
        name = item.text()
        base = os.path.dirname(__file__)
        path = os.path.join(base, "saved_chats", name)
        self._load_chat_file(path)

    def _delete_selected_saved_chat(self) -> None:
        if not hasattr(self, "_saved_chats_list"):
            return
        item = self._saved_chats_list.currentItem()
        if not item:
            return
        name = item.text()
        base = os.path.dirname(__file__)
        path = os.path.join(base, "saved_chats", name)
        try:
            os.remove(path)
            self._set_status_message(f"File {name} deleted.")
            self._refresh_saved_chats()
        except Exception as e:
            self._set_status_message(f"Failed to delete {name}: {e}")

    def _load_chat_file(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as e:
            self._set_status_message(f"Failed to load chat: {e}")
            return
        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            self._set_status_message("Invalid chat file format.")
            return
        self._suppress_history = True
        self._clear_chat_widgets(show_placeholder=False)
        for msg in messages:
            role = msg.get("role") if isinstance(msg, dict) else "assistant"
            text = msg.get("text") if isinstance(msg, dict) else str(msg)
            self._append_message(role, text)
        self._suppress_history = False

        self._chat_history = messages

    def _on_chat_finished(self, ok: bool, text: str) -> None:
        self._btn_send.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._btn_send.setText("Send")
        self._stream_cursor_timer.stop()
        self._stream_cursor_visible = False
        stop_requested = self._chat_stop_requested
        self._chat_stop_requested = False
        if stop_requested:
            final_text = self._stream_text.strip() or "Generation stopped."
        elif ok:
            final_text = text.strip() or self._stream_text.strip() or "Model gave an empty answer"
        else:
            final_text = f"Request failed:\n{text}"

        if self._stream_bubble is not None:
            self._stream_bubble.set_text(final_text)
            if ok and (not stop_requested):
                try:
                    self._chat_history.append(
                        {"role": "assistant", "text": final_text, "ts": datetime.utcnow().isoformat()}
                    )
                except Exception:
                    pass
            else:
                self._append_notice(final_text)
        else:
            if ok and (not stop_requested):
                self._append_message("assistant", final_text)
            else:
                self._append_notice(final_text)
        self._stream_bubble = None
        self._stream_text = ""

    def _on_clear(self) -> None:
        self._clear_chat_widgets(show_placeholder=True)
        self._chat_history = []
        self._pending_attachments = []


def main() -> None:
    if psutil is None:
        print("Module `psutil` is not installed")

    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon('icon_macos_1024.png'))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()