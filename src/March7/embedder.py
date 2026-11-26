"""embedder.py — BGE-M3 文本编码（本地 GPU，CPU 自动回退）。
见 docs/NAMING.md（本模块无内部代号）；模型/维度见《March7-实现规划-v1.md》§3.1（vec0 float[1024]）与 §3.3。

约定（docs/CONVENTIONS.md §2）：
- `sentence-transformers`（连带 torch 全家桶）只允许在**函数/方法内部** import——禁止模块顶层，
  否则任何 `import March7` 都会拖起 torch；
- 模型权重路径**不写死**：默认 `BAAI/bge-m3` 交给 HuggingFace 缓存解析，可被构造参数或
  `MARCH7_EMBEDDER_MODEL` 覆盖（env 读取统一走 `config._env`，不散落 `os.environ`）。

实测事实（2026-10-12 T9 开发会话，RTX 5060 Ti / torch 2.14.0+cu130 / sentence-transformers 6.1.0）：
- 输出 **1024 维**、`normalize_embeddings=True`（单位向量）。理由：`memories_vec` 建表时
  未指定 distance_metric（schema 已冻结），vec0 默认 **L2**；对单位向量而言 L2 排序与余弦排序等价，
  故入库与查询两侧都归一化，检索口径即「余弦」。
- ⚠️ **本机 HF 缓存不能走软链接**：默认缓存会把 snapshot 落成 **0 字节**文件，
  `SentenceTransformer` 随即在 `json.load` 上抛 `JSONDecodeError`（实测踩到）。
  对策 = 首次下载时设 `HF_HUB_DISABLE_SYMLINKS=1`（换机器同样适用，见 docs/PROGRESS.md 2026-10-12 段）。
- §3.3 记「首次加载 ~1.1GB 权重」与实测不符：仓库同时带 `pytorch_model.bin` 与
  `model.safetensors`（各 ≈2.27GB），HF snapshot 会**两个都拉** ⇒ 本地缓存实测 **4.3GB**
  （载入只用其中一份）。显存 2-4GB、首次加载数十秒的说法仍成立（实测 GPU 18.4s / CPU ≈15s）。
"""

from __future__ import annotations

import gc
import logging
import threading
import time
from typing import Any, Callable

from March7 import config

logger = logging.getLogger(__name__)

# 默认模型 + 覆盖入口（env 名只在此处集中声明，读取走 config._env）
DEFAULT_MODEL_NAME = "BAAI/bge-m3"
ENV_MODEL_NAME = "MARCH7_EMBEDDER_MODEL"
ENV_DEVICE = "MARCH7_EMBEDDER_DEVICE"

DEVICE_AUTO = "auto"
DEFAULT_BATCH_SIZE = 32

# 模型对象缓存：权重加载 = 数秒 + ~2GB 内存/显存，同进程内不该重复加载
# （单测里 5 个用例各 new 一个 Embedder，缓存后只加载一次）。
_MODEL_CACHE: dict[tuple[str, str], Any] = {}
_MODEL_CACHE_LOCK = threading.Lock()


def _env(name: str) -> str | None:
    """读环境变量——走 `config` 的包内出口，避免各模块散落 `os.environ`（CONVENTIONS §6）。"""
    return config._env(name)


def _resolve_device(requested: str, cuda_available: bool) -> str:
    """把 device 请求解析为实际设备（**纯函数**：不 import torch，单测/轻量环境可直接跑）。

    - `"auto"`（或空）→ CUDA 可用 `"cuda"`，否则 `"cpu"`；
    - 显式 `"cuda*"` 但 CUDA 不可用 → **回退 `"cpu"` 并告警**（§3.3「GPU 不可用自动回退 CPU」）；
    - 其他值（`cpu` / `mps`…）原样返回。
    """
    req = (requested or DEVICE_AUTO).strip().lower()
    if req in ("", DEVICE_AUTO):
        return "cuda" if cuda_available else "cpu"
    if req.startswith("cuda") and not cuda_available:
        logger.warning("请求 device=%s 但 CUDA 不可用：自动回退 CPU（§3.3）", requested)
        return "cpu"
    return req


def _wants_cuda(requested: str) -> bool:
    """该请求是否需要探测 CUDA（显式 `cpu` 时不必探测，避免无谓初始化 CUDA 上下文）。"""
    req = (requested or DEVICE_AUTO).strip().lower()
    return req in ("", DEVICE_AUTO) or req.startswith("cuda")


def _cuda_available() -> bool:
    """torch 可导入且 CUDA 设备可用；探测失败一律按「不可用」处理 = 走 CPU（不抛错）。

    注意：这里**不是**静默吞异常——失败会记 WARNING（CONVENTIONS §3：处理 + 语义化降级）。
    """
    try:
        import torch
    except ImportError:
        logger.warning("torch 不可导入：Embedder 按 CPU 处理（sentence-transformers 同样不可用）")
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception as exc:  # noqa: BLE001 —— 驱动/设备探测异常不该让整个进程挂掉，降级为 CPU
        logger.warning("CUDA 可用性探测失败（按不可用处理）：%s", exc)
        return False


def _embedding_dim(model: Any) -> Any:
    """取模型输出维度。

    sentence-transformers 6.x 把 `get_sentence_embedding_dimension()` 改名为
    `get_embedding_dimension()`（旧名仍可调用但会发 FutureWarning，实测 2026-10-12 复现）；
    pyproject 允许 `>=3.0`，故这里做双向兼容。
    """
    getter = getattr(model, "get_embedding_dimension", None) or getattr(
        model, "get_sentence_embedding_dimension", None
    )
    if getter is None:  # 理论上不会发生；返回 None 交由调用方按「未知维度」处理
        return None
    return getter()


def _build_model(model_name: str, device: str) -> Any:
    """真正加载 SentenceTransformer（**重量级 import 只出现在这里**）。"""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise ImportError(
            "sentence-transformers 未安装：Embedder 不可用（完整环境先 `uv sync`；"
            "轻量基线环境请按 CONVENTIONS §4 用 pytest.importorskip 守卫）"
        ) from exc

    logger.info(
        "加载编码模型 model=%s device=%s（首次运行会自动下载权重，约 4.3GB）", model_name, device
    )
    started = time.perf_counter()
    model = SentenceTransformer(model_name, device=device)
    if device.startswith("cuda"):
        try:
            model.half()  # FP16：§3.3 显存 2-4GB（RTX 5060 Ti 16G 富余）
        except Exception as exc:  # noqa: BLE001 —— 半精度不可用时降级 FP32，必须留痕
            logger.warning("FP16 转换失败，改用 FP32（显存占用上升）：%s", exc)
    logger.info(
        "编码模型就绪：dim=%s device=%s 耗时=%.1fs",
        _embedding_dim(model),
        device,
        time.perf_counter() - started,
    )
    return model


def _evict_model(model_name: str, device: str) -> None:
    """把 `(model, device)` 的缓存条目踢掉并尽量释放显存（回退 CPU 时用）。"""
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE.pop((model_name, device), None)
    gc.collect()
    try:
        import torch  # 延迟导入（CONVENTIONS §2：重量级依赖只在函数内）

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # pragma: no cover - 释放失败不影响主流程
        logger.debug("回退 CPU 时释放显存失败（忽略）", exc_info=True)


def _probe_encode(model: Any) -> None:
    """GPU 探针：真跑一次最小编码，验证**内核在这个设备上确实可用**。

    为什么不能只信 `torch.cuda.is_available()`（2026-10-31 本机实测）：
    torch 2.14.0+cu130 + RTX 5060 Ti（sm_120）上 `torch.tanh` 的 CUDA 内核缺失——
    `is_available()` 返回 True、`matmul` 也正常，但 BGE-M3 的 forward 走到 tanh 就抛
    `CUDA error: no kernel image is available`。只查可用性 ⇒ 得到一条"看起来在 GPU 上跑"
    的路径，实际**每次 encode 都抛错**；而钩子层恒 exit 0 ⇒ 表现为"每轮静默不写库"。

    探针成本：一次 2 字的 forward（毫秒级），远小于它挡住的故障成本。
    """
    model.encode(["探针"], show_progress_bar=False, normalize_embeddings=True)


def _load_model(model_name: str, device: str) -> Any:
    """按 `(model_name, device)` 缓存加载（加锁：并发构造不得重复加载权重）。"""
    key = (model_name, device)
    with _MODEL_CACHE_LOCK:
        model = _MODEL_CACHE.get(key)
        if model is None:
            model = _build_model(model_name, device)
            _MODEL_CACHE[key] = model
    return model


class Embedder:
    """BGE-M3 编码器：1024 维，多语言（中文主场），FP16 GPU，GPU 不可用回退 CPU。

    §3.3：显存 2-4GB（RTX 5060 Ti 16G 富余）。
    降级路径（§8）：BGE-M3 不可用时降级 `bge-small-zh-v1.5`（512 维）——**维度与建表维度联动，
    属 ADR 级变更**；因此本类在维度不符时**直接抛错**，不静默降级，否则 `memories_vec` 的
    `float[1024]` 会静默错位（写入成功、召回全错，最难查的一类 bug）。
    """

    EMBEDDING_DIM = 1024  # BGE-M3 输出维度（memories_vec 的 float[1024] 与此一致）

    def __init__(self, model_name: str | None = None, device: str | None = None) -> None:
        """加载模型。

        Args:
            model_name: 模型名。`None` → `MARCH7_EMBEDDER_MODEL` → 默认 `BAAI/bge-m3`
                （不写死权重路径；换模型 = 改配置，不动代码）。
            device: `"cuda"` / `"cpu"` / `"auto"` / `None`（`None` = `MARCH7_EMBEDDER_DEVICE`
                → `auto`：GPU 优先，不可用自动回退 CPU）。

        Raises:
            ImportError: 环境缺 sentence-transformers / torch。
            RuntimeError: 模型输出维度 != `EMBEDDING_DIM`（换维度模型须先走 ADR）。
        """
        requested = device or _env(ENV_DEVICE) or DEVICE_AUTO
        self.model_name = model_name or _env(ENV_MODEL_NAME) or DEFAULT_MODEL_NAME
        # 显式 cpu 时不探测 CUDA（避免无谓初始化 CUDA 上下文）
        cuda_available = _cuda_available() if _wants_cuda(requested) else False
        self.device = _resolve_device(requested, cuda_available)
        self._model = _load_model(self.model_name, self.device)

        # GPU 探针（2026-10-31 实测补）：`is_available()` 只说明"驱动看得见卡"，
        # 不说明"这个 torch 构建在这个算力上有内核"。探针失败 ⇒ 响亮记 ERROR 后回退 CPU，
        # 而不是让每一次真实编码都抛错（那在钩子里会变成静默不写库）。
        if self.device.startswith("cuda"):
            try:
                _probe_encode(self._model)
            except Exception as exc:  # noqa: BLE001 —— 任何后端异常都按"GPU 不可用"处理
                logger.error(
                    "GPU 编码探针失败，回退 CPU（device=%s）：%s。"
                    "注意：torch.cuda.is_available() 为真并**不能**证明内核可用。",
                    self.device,
                    exc,
                )
                _evict_model(self.model_name, self.device)
                self.device = "cpu"
                self._model = _load_model(self.model_name, "cpu")

        dim = _embedding_dim(self._model)
        if isinstance(dim, int) and dim > 0 and dim != self.EMBEDDING_DIM:
            raise RuntimeError(
                f"模型 {self.model_name} 输出 {dim} 维，与 memories_vec 的 "
                f"float[{self.EMBEDDING_DIM}] 不一致：换维度模型属 ADR 级变更"
                "（建表维度须联动），请先走 ADR 再改（CONVENTIONS §2 依赖边界）。"
            )
        logger.debug(
            "Embedder 就绪：model=%s device=%s dim=%s", self.model_name, self.device, dim
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量编码：返回与输入**等长**的 1024 维向量列表（元素为 Python `float`，非 numpy）。

        - `[]` 输入直接返回 `[]`（不触模型）；
        - `normalize_embeddings=True`：输出单位向量，使 vec0 的 L2 距离与余弦同序（见模块 docstring）；
        - 返回纯 Python list（`.tolist()`），下游 `store._serialize_embedding` 再转 float32 字节串。

        Raises:
            TypeError: `texts` 不是 `list`。
            RuntimeError: 输出维度与 `EMBEDDING_DIM` 不符（模型被换错）。
        """
        if not isinstance(texts, list):
            raise TypeError(f"texts 必须是 list[str]，收到 {type(texts).__name__}")
        if not texts:
            return []

        vectors = self._model.encode(
            texts,
            batch_size=DEFAULT_BATCH_SIZE,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        rows: list[list[float]] = vectors.tolist()

        for row in rows:
            if len(row) != self.EMBEDDING_DIM:
                raise RuntimeError(
                    f"编码输出 {len(row)} 维，与 EMBEDDING_DIM={self.EMBEDDING_DIM} 不符"
                    f"（model={self.model_name}）：拒绝写入 vec0，避免索引静默错位。"
                )
        return rows

    def embed_one(self, text: str) -> list[float]:
        """单条编码（召回 query 用）。

        刻意走 `embed([text])[0]` 同一条路径：入库与查询必须用**同一编码器 + 同一批处理口径**，
        否则归一化/池化差异会造成极小的漂移，让「单条编 vs 批量编」结果不一致。
        """
        return self.embed([text])[0]


class LazyEmbedder:
    """按需加载的编码器代理（`ADR-0036`）：**构造时不碰权重，首次真正编码才加载**。

    为什么需要（2026-11-06 真机复算）：Stop 钩子每轮是宿主拉起的**独立进程**，
    `server._build_engine()` 会无条件 `Embedder()`，而 `Embedder.__init__`
    直接 `_load_model()` 加载 BGE-M3（本机 CPU 回退实测 14.4s 起）。
    但 11-01~11-06 的 124 轮 `stop_ingested` 里 **111 轮 `written=0`**——
    Judge 判定「这轮没值得记的」时 `process_transcript` 根本不会调 `embed_one`，
    那 14.4s 是纯白付，正好对上钩子日志 `elapsed_ms` 的最小值 **14377ms**。
    把加载推迟到第一次编码 ⇒ 这些轮次只剩一次 LLM 调用。

    ⚠️ **语义变化（必须一起读）**：模型损坏 / 维度不符这类「响亮失败」从
    **进程启动时**推迟到**首次真正编码时**。对 Stop 写入通道，没有候选本就不该碰模型；
    对 `March7_recall`、以及有候选要入库的 `March7_write`，仍会在首次编码时立刻抛错，
    不会被静默吞掉。取舍与替代方案见 `ADR-0036`。

    **组合而非继承**：这是鸭子类型（与 `tests` 里的 `_FakeEmbedder` 同例），
    不假装是 `Embedder` 的子类——继承一个必须跳过 `super().__init__()` 的基类
    会留下 `self._model` 这类未初始化字段，是最难查的那类坑。
    """

    def __init__(self, factory: Callable[[], Embedder] | None = None) -> None:
        self._factory = factory or Embedder
        self._delegate: Embedder | None = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        """本进程是否**已经真的加载过权重**（只读观测位，供体检/日志用）。"""
        return self._delegate is not None

    def _ensure(self) -> Embedder:
        # 双检锁：同一进程里 Judge（入库）与 Retriever（召回）可能同时首次要编码。
        if self._delegate is None:
            with self._lock:
                if self._delegate is None:
                    self._delegate = self._factory()
        return self._delegate

    @property
    def model_name(self) -> str:
        return self._ensure().model_name

    @property
    def device(self) -> str:
        return self._ensure().device

    def embed(self, texts: list[str]) -> list[list[float]]:
        # 空输入不触模型：与 `Embedder.embed([])` 同义，且省掉整次加载。
        if not isinstance(texts, list):
            raise TypeError(f"texts 必须是 list[str]，收到 {type(texts).__name__}")
        if not texts:
            return []
        return self._ensure().embed(texts)

    def embed_one(self, text: str) -> list[float]:
        """与 `Embedder.embed_one` 同一口径：走 `embed([text])[0]`，不另开路径。"""
        return self.embed([text])[0]
