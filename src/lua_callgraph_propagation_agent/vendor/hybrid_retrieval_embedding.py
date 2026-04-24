#!/usr/bin/env python3
"""
Hybrid retrieval with sentence embeddings for semantic similarity.

Compared to 01_hybrid_retrieval.py:
- symbolic similarity: same
- numeric similarity: same
- semantic similarity: sentence embedding instead of TF-IDF

Recommended first model:
  BAAI/bge-small-en-v1.5

Example:

  python scripts/03_hybrid_retrieval_embedding.py build \
    --input-dir data/raw_features/Lua_547/x86_64 \
    --index-out data/indexes/Lua_547_x86_bge.pkl \
    --embedding-model BAAI/bge-small-en-v1.5

  python scripts/03_hybrid_retrieval_embedding.py search-file \
    --index data/indexes/Lua_547_x86_bge.pkl \
    --query-file data/eval/tmp/masked_x86_query.json \
    --query-func luaL_checktype \
    --topk 10 \
    --save-json data/results/luaL_checktype_bge_top10.json
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

import numpy as np
from tqdm import tqdm
from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import SentenceTransformer

try:
    import faiss  # type: ignore
except ImportError:
    faiss = None


# =========================
# Config
# =========================
DEFAULT_OPCODE_LIST = [
    "COPY",
    "LOAD",
    "STORE",
    "CALL",
    "CALLIND",
    "BRANCH",
    "CBRANCH",
    "RETURN",
    "INT_EQUAL",
    "INT_NOTEQUAL",
    "INT_LESS",
    "INT_LESSEQUAL",
    "INT_ADD",
    "INT_SUB",
    "INT_MULT",
    "INT_AND",
    "INT_OR",
    "INT_XOR",
    "PTRSUB",
    "PTRADD",
    "CAST",
    "MULTIEQUAL",
]

HYBRID_WEIGHTS = {
    "symbolic": 0.25,
    "numeric": 0.35,
    "semantic": 0.40,
}

BASE_WEIGHTS = {
    "semantic": 0.55,
    "numeric": 0.45,
}

SYMBOLIC_BONUS_CAP = 0.18
SYMBOLIC_BONUS_V2_CAP = 0.08

WEAK_STRING_TOKENS = {
    "",
    "a",
    "an",
    "and",
    "error",
    "file",
    "function",
    "index",
    "nil",
    "no",
    "not",
    "number",
    "string",
    "table",
    "the",
    "to",
    "type",
    "value",
}

MAX_STRINGS_IN_TEXT = 20
MAX_CALLEES_IN_TEXT = 10
MAX_CALLERS_IN_TEXT = 10
MAX_COMPARE_TOKENS_IN_TEXT = 60
MAX_OFFSETS_IN_TEXT = 30


# =========================
# Data structures
# =========================
@dataclass
class FunctionRecord:
    function_id: str
    source_json: str
    function_name: str
    metadata: Dict[str, Any]
    raw_features: Dict[str, Any]
    symbolic_tokens: List[str]
    semantic_text: str
    numeric_vector: np.ndarray


@dataclass
class HybridEmbeddingIndex:
    records: List[FunctionRecord]
    semantic_model_name: str
    semantic_matrix: np.ndarray
    numeric_matrix: np.ndarray
    numeric_mean: np.ndarray
    numeric_std: np.ndarray


@dataclass
class HybridDiskIndex:
    records: List[FunctionRecord]
    semantic_model_name: str
    numeric_matrix: np.ndarray
    numeric_mean: np.ndarray
    numeric_std: np.ndarray
    semantic_matrix: Optional[np.ndarray] = None
    faiss_index: Any = None
    index_dir: Optional[str] = None


# =========================
# Utility
# =========================
def safe_list(x: Any) -> List[Any]:
    return x if isinstance(x, list) else []


def safe_dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def flatten_compare(compare_map: Dict[str, Any]) -> List[Tuple[str, int]]:
    items: List[Tuple[str, int]] = []
    for off, vals in safe_dict(compare_map).items():
        for v in safe_list(vals):
            if isinstance(v, int):
                items.append((str(off), v))
    return items


@lru_cache(maxsize=8)
def parse_feature_json_file(json_path: Path) -> List[Dict[str, Any]]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    raise ValueError(f"Expected list in {json_path}, got {type(data).__name__}")


def fit_zscore_stats(matrix: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    mean = matrix.mean(axis=0)
    std = matrix.std(axis=0)
    std[std == 0] = 1.0
    return mean, std


def apply_zscore(matrix: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    return (matrix - mean) / std


def l2_normalize_rows(matrix: np.ndarray) -> np.ndarray:
    if matrix.size == 0:
        return matrix
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


@lru_cache(maxsize=4)
@lru_cache(maxsize=4)
def load_embedding_model(model_name: str) -> SentenceTransformer:
    print(f"[INFO] loading embedding model: {model_name}")
    model = SentenceTransformer(model_name, local_files_only=True)
    return model

def collapse_by_function_name(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    collapsed = []
    for r in results:
        fn = r["function_name"]
        if fn in seen:
            continue
        seen.add(fn)
        collapsed.append(r)
    return collapsed

# =========================
# Feature transforms
# =========================
def build_symbolic_tokens(feature: Dict[str, Any]) -> List[str]:
    tokens: List[str] = []

    for s in safe_list(feature.get("strings")):
        if isinstance(s, str) and s.strip():
            tokens.append(f"str:{s.strip().lower()}")

    for c in safe_list(feature.get("callees")):
        if isinstance(c, str) and c.strip():
            tokens.append(f"callee:{c.strip().lower()}")

    for c in safe_list(feature.get("callers")):
        if isinstance(c, str) and c.strip():
            tokens.append(f"caller:{c.strip().lower()}")

    for off, val in flatten_compare(feature.get("compare", {})):
        tokens.append(f"cmp:{off}={val}")

    for off in safe_list(feature.get("struct_offsets")):
        if isinstance(off, int):
            tokens.append(f"off:{off}")

    return sorted(set(tokens))


def build_semantic_text(feature: Dict[str, Any]) -> str:
    parts: List[str] = []

    bb_count = feature.get("basic_block_count", 0)
    pcode_count = feature.get("pcode_instruction_count", 0)
    parts.append(f"basic blocks {bb_count}")
    parts.append(f"pcode instructions {pcode_count}")

    ratio = safe_dict(feature.get("pcode_opcode_ratio"))
    if ratio:
        ratio_items = sorted(ratio.items(), key=lambda x: x[0])
        ratio_text = " ".join(
            [f"{k.lower()} {v:.4f}" for k, v in ratio_items if isinstance(v, (int, float))]
        )
        parts.append(f"opcode ratio {ratio_text}")

    offsets = [x for x in safe_list(feature.get("struct_offsets")) if isinstance(x, int)]
    if offsets:
        offsets = sorted(set(offsets))[:MAX_OFFSETS_IN_TEXT]
        parts.append("offsets " + " ".join(map(str, offsets)))

    rw = safe_dict(feature.get("read_write"))
    rw_tokens: List[str] = []
    for off, counts in rw.items():
        counts = safe_dict(counts)
        r = counts.get("read", 0)
        w = counts.get("write", 0)
        rw_tokens.append(f"offset {off} read {r} write {w}")
    if rw_tokens:
        parts.append("read write " + " ; ".join(rw_tokens[:50]))

    cmp_tokens: List[str] = []
    for off, val in flatten_compare(feature.get("compare", {})):
        cmp_tokens.append(f"offset {off} compare {val}")
    if cmp_tokens:
        parts.append("compare " + " ; ".join(cmp_tokens[:MAX_COMPARE_TOKENS_IN_TEXT]))

    strings = [s.strip().lower() for s in safe_list(feature.get("strings")) if isinstance(s, str) and s.strip()]
    if strings:
        parts.append("strings " + " ".join(strings[:MAX_STRINGS_IN_TEXT]))

    callees = [s.strip().lower() for s in safe_list(feature.get("callees")) if isinstance(s, str) and s.strip()]
    callers = [s.strip().lower() for s in safe_list(feature.get("callers")) if isinstance(s, str) and s.strip()]

    if callees:
        parts.append("calls " + " ".join(callees[:MAX_CALLEES_IN_TEXT]))
    if callers:
        parts.append("called by " + " ".join(callers[:MAX_CALLERS_IN_TEXT]))

    lua_version = feature.get("lua_version", "")
    architecture = feature.get("architecture", "")
    if lua_version:
        parts.append(f"lua version {lua_version}")
    if architecture:
        parts.append(f"architecture {architecture}")

    return "\n".join(parts)


def build_numeric_vector(
    feature: Dict[str, Any],
    opcode_list: Optional[List[str]] = None,
) -> np.ndarray:
    opcode_list = opcode_list or DEFAULT_OPCODE_LIST
    vec: List[float] = []

    ratio = safe_dict(feature.get("pcode_opcode_ratio"))
    for op in opcode_list:
        vec.append(float(ratio.get(op, 0.0)))

    bb_count = math.log1p(float(feature.get("basic_block_count", 0)))
    pcode_count = math.log1p(float(feature.get("pcode_instruction_count", 0)))

    offsets = [
        x for x in safe_list(feature.get("struct_offsets"))
        if isinstance(x, int) and 0 <= x <= 1_000_000
    ]

    offset_count = math.log1p(float(len(offsets)))
    raw_offset_max = float(max(offsets)) if offsets else 0.0
    raw_offset_mean = float(sum(offsets) / len(offsets)) if offsets else 0.0

    offset_max = math.log1p(raw_offset_max)
    offset_mean = math.log1p(raw_offset_mean)

    read_write = safe_dict(feature.get("read_write"))
    read_total = 0.0
    write_total = 0.0
    for _, counts in read_write.items():
        counts = safe_dict(counts)
        read_total += float(counts.get("read", 0))
        write_total += float(counts.get("write", 0))

    read_total = math.log1p(read_total)
    write_total = math.log1p(write_total)

    compare_count = math.log1p(float(len(flatten_compare(feature.get("compare", {})))))
    callee_count = math.log1p(float(len(safe_list(feature.get("callees")))))
    caller_count = math.log1p(float(len(safe_list(feature.get("callers")))))
    string_count = math.log1p(float(len(safe_list(feature.get("strings")))))

    vec.extend([
        bb_count,
        pcode_count,
        offset_count,
        offset_max,
        offset_mean,
        read_total,
        write_total,
        compare_count,
        callee_count,
        caller_count,
        string_count,
    ])

    return np.array(vec, dtype=np.float32)


def make_function_id(json_path: Path, feature: Dict[str, Any], input_root: Path) -> str:
    rel = json_path.relative_to(input_root).as_posix()
    fn_name = str(feature.get("function_name", "unknown_function"))
    entry = str(feature.get("entry_point", "unknown_entry"))
    return f"{rel}::{fn_name}@{entry}"


# =========================
# Loading records
# =========================
@lru_cache(maxsize=8)
def parse_feature_json_file(json_path: Path) -> List[Dict[str, Any]]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    raise ValueError(f"Expected list in {json_path}, got {type(data).__name__}")


def load_jsonl_records(jsonl_path: Path) -> List[Dict[str, Any]]:
    records = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception as e:
                print(f"[WARN] failed to parse {jsonl_path}:{line_no}: {e}")
    return records


def build_function_record_from_row(row: Dict[str, Any], source_path: Path) -> FunctionRecord:
    feature = row.get("raw_features", row)
    function_name = str(
        row.get("canonical_function_name")
        or row.get("function_name")
        or feature.get("function_name", "unknown_function")
    )
    function_id = str(
        row.get("function_id")
        or f"{source_path.name}::{function_name}@{feature.get('entry_point', 'unknown_entry')}"
    )

    metadata = {
        "lua_version": row.get("lua_version", feature.get("lua_version")),
        "architecture": row.get("architecture", feature.get("architecture")),
        "source_json": row.get("source_json", str(source_path)),
        "binary_id": row.get("binary_id"),
        "opt_level": row.get("opt_level"),
    }

    return FunctionRecord(
        function_id=function_id,
        source_json=row.get("source_json", str(source_path)),
        function_name=function_name,
        metadata=metadata,
        raw_features=feature,
        symbolic_tokens=build_symbolic_tokens(feature),
        semantic_text=build_semantic_text(feature),
        numeric_vector=build_numeric_vector(feature),
    )


def load_records(input_dir: Path) -> List[FunctionRecord]:
    records: List[FunctionRecord] = []

    json_files = sorted(input_dir.rglob("*.json"))
    jsonl_files = sorted(input_dir.rglob("*.jsonl"))

    # 1) 기존 raw json 지원
    for json_path in json_files:
        try:
            funcs = parse_feature_json_file(json_path)
        except Exception as e:
            print(f"[WARN] failed to parse {json_path}: {e}")
            continue

        for feature in funcs:
            try:
                function_id = make_function_id(json_path, feature, input_dir)
                function_name = str(feature.get("function_name", "unknown_function"))
                metadata = {
                    "lua_version": feature.get("lua_version"),
                    "architecture": feature.get("architecture"),
                    "source_json": str(json_path),
                }

                symbolic_tokens = build_symbolic_tokens(feature)
                semantic_text = build_semantic_text(feature)
                numeric_vector = build_numeric_vector(feature)

                records.append(
                    FunctionRecord(
                        function_id=function_id,
                        source_json=str(json_path),
                        function_name=function_name,
                        metadata=metadata,
                        raw_features=feature,
                        symbolic_tokens=symbolic_tokens,
                        semantic_text=semantic_text,
                        numeric_vector=numeric_vector,
                    )
                )
            except Exception as e:
                print(f"[WARN] failed to load record from {json_path}: {e}")
                continue

    # 2) filtered/dedup jsonl 지원
    for jsonl_path in jsonl_files:
        row_records = load_jsonl_records(jsonl_path)

        for row in row_records:
            try:
                records.append(build_function_record_from_row(row, jsonl_path))
            except Exception as e:
                print(f"[WARN] failed to load record from {jsonl_path}: {e}")
                continue

    return records


# =========================
# Index build
# =========================
def build_index(input_dir: Path, embedding_model_name: str, batch_size: int = 128) -> HybridEmbeddingIndex:
    records = load_records(input_dir)
    if not records:
        raise RuntimeError(f"No records found under {input_dir}")

    print("[INFO] building numeric matrix...")
    numeric_matrix_raw = np.vstack([r.numeric_vector for r in tqdm(records, desc="numeric vec", unit="func")])

    print("[INFO] fitting numeric normalization...")
    numeric_mean, numeric_std = fit_zscore_stats(numeric_matrix_raw)
    numeric_matrix = apply_zscore(numeric_matrix_raw, numeric_mean, numeric_std)
    numeric_matrix = l2_normalize_rows(numeric_matrix)

    print("[INFO] building semantic texts...")
    semantic_texts = [r.semantic_text for r in tqdm(records, desc="semantic text", unit="func")]

    model = load_embedding_model(embedding_model_name)

    print("[INFO] encoding semantic embeddings...")
    semantic_matrix = model.encode(
        semantic_texts,
        batch_size=batch_size,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).astype(np.float32)

    print("[INFO] index build complete")

    return HybridEmbeddingIndex(
        records=records,
        semantic_model_name=embedding_model_name,
        semantic_matrix=semantic_matrix,
        numeric_matrix=numeric_matrix.astype(np.float32),
        numeric_mean=numeric_mean.astype(np.float32),
        numeric_std=numeric_std.astype(np.float32),
    )


def serialize_record(record: FunctionRecord, row_id: int) -> Dict[str, Any]:
    return {
        "row_id": row_id,
        "function_id": record.function_id,
        "function_name": record.function_name,
        "source_json": record.source_json,
        "metadata": record.metadata,
        "symbolic_tokens": record.symbolic_tokens,
        "semantic_text": record.semantic_text,
        "raw_features": record.raw_features,
    }


def save_records_jsonl(records: List[FunctionRecord], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row_id, record in enumerate(records):
            f.write(json.dumps(serialize_record(record, row_id), ensure_ascii=False) + "\n")


def load_records_jsonl_as_function_records(path: Path) -> List[FunctionRecord]:
    return [build_function_record_from_row(row, path) for row in load_jsonl_records(path)]


def save_directory_index(index: HybridEmbeddingIndex, index_dir: Path) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)

    records_path = index_dir / "records.jsonl"
    numeric_path = index_dir / "numeric.npz"
    meta_path = index_dir / "meta.json"

    save_records_jsonl(index.records, records_path)
    np.savez_compressed(
        numeric_path,
        numeric_matrix=index.numeric_matrix.astype(np.float32),
        numeric_mean=index.numeric_mean.astype(np.float32),
        numeric_std=index.numeric_std.astype(np.float32),
    )

    semantic_backend = "numpy"
    if faiss is not None:
        semantic_path = index_dir / "semantic.faiss"
        faiss_index = faiss.IndexFlatIP(index.semantic_matrix.shape[1])
        faiss_index.add(index.semantic_matrix.astype(np.float32))
        faiss.write_index(faiss_index, str(semantic_path))
        semantic_backend = "faiss"
    else:
        semantic_path = index_dir / "semantic.npy"
        np.save(semantic_path, index.semantic_matrix.astype(np.float32))

    meta = {
        "index_format": "hybrid_embedding_directory_v1",
        "semantic_backend": semantic_backend,
        "semantic_model_name": index.semantic_model_name,
        "num_records": len(index.records),
        "semantic_dim": int(index.semantic_matrix.shape[1]),
        "numeric_dim": int(index.numeric_matrix.shape[1]),
        "hybrid_weights": HYBRID_WEIGHTS,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)


def load_directory_index(index_dir: Path) -> HybridDiskIndex:
    meta_path = index_dir / "meta.json"
    records_path = index_dir / "records.jsonl"
    numeric_path = index_dir / "numeric.npz"

    if not meta_path.exists():
        raise FileNotFoundError(f"meta.json not found under {index_dir}")
    if not records_path.exists():
        raise FileNotFoundError(f"records.jsonl not found under {index_dir}")
    if not numeric_path.exists():
        raise FileNotFoundError(f"numeric.npz not found under {index_dir}")

    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    numeric_data = np.load(numeric_path)
    records = load_records_jsonl_as_function_records(records_path)

    semantic_matrix: Optional[np.ndarray] = None
    faiss_index = None

    semantic_faiss_path = index_dir / "semantic.faiss"
    semantic_npy_path = index_dir / "semantic.npy"

    if semantic_faiss_path.exists() and faiss is not None:
        faiss_index = faiss.read_index(str(semantic_faiss_path))
    elif semantic_npy_path.exists():
        semantic_matrix = np.load(semantic_npy_path).astype(np.float32)
    elif semantic_faiss_path.exists():
        raise RuntimeError(
            f"FAISS index exists at {semantic_faiss_path}, but faiss is not installed. "
            "Install faiss-cpu/faiss-gpu or rebuild the index without FAISS."
        )
    else:
        raise FileNotFoundError(f"No semantic index found under {index_dir}")

    return HybridDiskIndex(
        records=records,
        semantic_model_name=str(meta["semantic_model_name"]),
        numeric_matrix=numeric_data["numeric_matrix"].astype(np.float32),
        numeric_mean=numeric_data["numeric_mean"].astype(np.float32),
        numeric_std=numeric_data["numeric_std"].astype(np.float32),
        semantic_matrix=semantic_matrix,
        faiss_index=faiss_index,
        index_dir=str(index_dir),
    )


def load_index(index_path: Path) -> Union[HybridEmbeddingIndex, HybridDiskIndex]:
    if index_path.is_dir():
        return load_directory_index(index_path)

    with open(index_path, "rb") as f:
        return pickle.load(f)


# =========================
# Search scoring
# =========================
def symbolic_jaccard(tokens_a: Iterable[str], tokens_b: Iterable[str]) -> float:
    a = set(tokens_a)
    b = set(tokens_b)
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def tokens_with_prefix(tokens: Iterable[str], prefix: str) -> Set[str]:
    return {t[len(prefix):] for t in tokens if isinstance(t, str) and t.startswith(prefix)}


def strong_string_tokens(tokens: Iterable[str]) -> Set[str]:
    values = set()
    for value in tokens_with_prefix(tokens, "str:"):
        normalized = " ".join(value.lower().split())
        if len(normalized) < 4:
            continue
        if normalized in WEAK_STRING_TOKENS:
            continue
        values.add(normalized)
    return values


def threshold_bonus(count: int, rules: List[Tuple[int, float]]) -> float:
    return sum(bonus for threshold, bonus in rules if count >= threshold)


def compute_symbolic_bonus(
    query_tokens: Iterable[str],
    candidate_tokens: Iterable[str],
    base_score: float,
) -> float:
    q = set(query_tokens)
    c = set(candidate_tokens)

    offset_overlap = len(tokens_with_prefix(q, "off:") & tokens_with_prefix(c, "off:"))
    compare_overlap = len(tokens_with_prefix(q, "cmp:") & tokens_with_prefix(c, "cmp:"))
    string_overlap = len(strong_string_tokens(q) & strong_string_tokens(c))
    callee_overlap = len(tokens_with_prefix(q, "callee:") & tokens_with_prefix(c, "callee:"))

    bonus = 0.0
    bonus += threshold_bonus(offset_overlap, [(2, 0.02), (3, 0.03), (5, 0.03)])
    bonus += threshold_bonus(compare_overlap, [(1, 0.02), (2, 0.03), (3, 0.02)])
    bonus += threshold_bonus(string_overlap, [(1, 0.02), (2, 0.02)])
    bonus += threshold_bonus(callee_overlap, [(1, 0.01), (2, 0.01)])

    if base_score >= 0.40:
        if string_overlap >= 2:
            bonus += 0.15
        elif string_overlap >= 1:
            bonus += 0.10

    if base_score >= 0.45:
        if 1 <= compare_overlap <= 2:
            bonus += 0.06
        if 2 <= offset_overlap <= 4:
            bonus += 0.05

    return min(bonus, SYMBOLIC_BONUS_CAP)


def compute_symbolic_bonus_v2(
    query_tokens: Iterable[str],
    candidate_tokens: Iterable[str],
    base_score: float,
    best_base_score: float,
) -> float:
    q = set(query_tokens)
    c = set(candidate_tokens)

    offset_overlap = len(tokens_with_prefix(q, "off:") & tokens_with_prefix(c, "off:"))
    compare_overlap = len(tokens_with_prefix(q, "cmp:") & tokens_with_prefix(c, "cmp:"))
    string_overlap = len(strong_string_tokens(q) & strong_string_tokens(c))
    callee_overlap = len(tokens_with_prefix(q, "callee:") & tokens_with_prefix(c, "callee:"))

    bonus = 0.0
    bonus += threshold_bonus(offset_overlap, [(2, 0.010), (3, 0.015), (5, 0.015)])
    bonus += threshold_bonus(compare_overlap, [(1, 0.010), (2, 0.015), (3, 0.015)])
    bonus += threshold_bonus(string_overlap, [(1, 0.015), (2, 0.015)])
    bonus += threshold_bonus(callee_overlap, [(1, 0.005), (2, 0.005)])

    if base_score >= 0.45:
        if string_overlap >= 2:
            bonus += 0.070
        elif string_overlap >= 1:
            bonus += 0.040

    if base_score >= 0.50:
        if 1 <= compare_overlap <= 2:
            bonus += 0.030
        if 2 <= offset_overlap <= 4:
            bonus += 0.020

    if base_score < best_base_score - 0.03:
        bonus *= 0.30
    if base_score < 0.60:
        bonus = min(bonus, 0.03)

    return min(bonus, SYMBOLIC_BONUS_V2_CAP)


def compute_semantic_scores_and_candidates(
    index: Union[HybridEmbeddingIndex, HybridDiskIndex],
    query_sem: np.ndarray,
    topk: int,
    candidate_pool: Optional[int] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    if isinstance(index, HybridDiskIndex) and index.faiss_index is not None:
        pool = candidate_pool or max(topk * 20, 200)
        pool = min(pool, len(index.records))
        scores, ids = index.faiss_index.search(query_sem.astype(np.float32), pool)
        semantic_scores = np.full(len(index.records), -1.0, dtype=np.float32)
        candidate_ids = ids[0]
        for idx, score in zip(candidate_ids, scores[0]):
            if idx >= 0:
                semantic_scores[int(idx)] = float(score)
        candidate_ids = np.array([int(x) for x in candidate_ids if x >= 0], dtype=np.int32)
        return semantic_scores, candidate_ids

    semantic_matrix = index.semantic_matrix
    if semantic_matrix is None:
        raise RuntimeError("semantic_matrix is not available for this index")

    semantic_scores = cosine_similarity(query_sem, semantic_matrix)[0].astype(np.float32)

    # candidate_pool 적용: 상위 N개만 symbolic 루프 대상으로 제한
    if candidate_pool is not None and candidate_pool < len(index.records):
        top_ids = np.argpartition(semantic_scores, -candidate_pool)[-candidate_pool:]
        candidate_ids = top_ids[np.argsort(semantic_scores[top_ids])[::-1]].astype(np.int32)
    else:
        candidate_ids = np.argsort(semantic_scores)[::-1].astype(np.int32)

    return semantic_scores, candidate_ids


def _score_candidates(
    index: Union[HybridEmbeddingIndex, HybridDiskIndex],
    query_record: FunctionRecord,
    query_sem: np.ndarray,
    topk: int,
    candidate_pool: Optional[int],
    scoring_mode: str,
    exclude_same_id: bool,
    hybrid_weights: Optional[Dict[str, float]],
) -> List[Dict[str, Any]]:
    """Core scoring logic shared by search_index and search_index_with_embedding."""
    weights = hybrid_weights or HYBRID_WEIGHTS

    semantic_scores, candidate_ids = compute_semantic_scores_and_candidates(
        index=index,
        query_sem=query_sem,
        topk=topk,
        candidate_pool=candidate_pool,
    )

    query_num = query_record.numeric_vector.reshape(1, -1).astype(np.float32)
    query_num = apply_zscore(query_num, index.numeric_mean, index.numeric_std)
    query_num = l2_normalize_rows(query_num)
    numeric_scores = np.full(len(index.records), 0.0, dtype=np.float32)
    if len(candidate_ids) > 0:
        numeric_scores_candidate = cosine_similarity(query_num, index.numeric_matrix[candidate_ids])[0]
        numeric_scores[candidate_ids] = numeric_scores_candidate.astype(np.float32)

    symbolic_scores = np.full(len(index.records), 0.0, dtype=np.float32)
    base_scores = np.full(len(index.records), 0.0, dtype=np.float32)

    if scoring_mode in {"bonus", "bonus_v2"}:
        base_scores = (
            BASE_WEIGHTS["semantic"] * semantic_scores
            + BASE_WEIGHTS["numeric"] * numeric_scores
        ).astype(np.float32)
        best_base_score = float(np.max(base_scores[candidate_ids])) if len(candidate_ids) > 0 else 0.0
        for i in candidate_ids.tolist():
            if scoring_mode == "bonus_v2":
                symbolic_scores[i] = compute_symbolic_bonus_v2(
                    query_record.symbolic_tokens,
                    index.records[i].symbolic_tokens,
                    float(base_scores[i]),
                    best_base_score,
                )
            else:
                symbolic_scores[i] = compute_symbolic_bonus(
                    query_record.symbolic_tokens,
                    index.records[i].symbolic_tokens,
                    float(base_scores[i]),
                )
        total_scores = base_scores + symbolic_scores
    else:
        for i in candidate_ids.tolist():
            symbolic_scores[i] = symbolic_jaccard(query_record.symbolic_tokens, index.records[i].symbolic_tokens)
        total_scores = (
            weights["symbolic"] * symbolic_scores
            + weights["numeric"] * numeric_scores
            + weights["semantic"] * semantic_scores
        ).astype(np.float32)
        base_scores = (
            weights["numeric"] * numeric_scores
            + weights["semantic"] * semantic_scores
        ).astype(np.float32)

    results: List[Dict[str, Any]] = []
    for i in candidate_ids.tolist():
        rec = index.records[i]
        if exclude_same_id and rec.function_id == query_record.function_id:
            continue
        results.append(
            {
                "function_id": rec.function_id,
                "function_name": rec.function_name,
                "source_json": rec.source_json,
                "score_total": float(total_scores[i]),
                "score_breakdown": {
                    "base": float(base_scores[i]),
                    "symbolic": float(symbolic_scores[i]),
                    "numeric": float(numeric_scores[i]),
                    "semantic": float(semantic_scores[i]),
                    "scoring_mode": scoring_mode,
                },
                "metadata": rec.metadata,
            }
        )

    results.sort(key=lambda x: x["score_total"], reverse=True)
    return results[:topk]


def search_index(
    index: Union[HybridEmbeddingIndex, HybridDiskIndex],
    query_record: FunctionRecord,
    topk: int = 5,
    exclude_same_id: bool = True,
    hybrid_weights: Optional[Dict[str, float]] = None,
    candidate_pool: Optional[int] = None,
    scoring_mode: str = "jaccard",
) -> List[Dict[str, Any]]:
    """Search using a single query record (encodes semantic text internally)."""
    if scoring_mode not in {"jaccard", "bonus", "bonus_v2"}:
        raise ValueError(f"Unsupported scoring_mode: {scoring_mode}")
    model = load_embedding_model(index.semantic_model_name)
    query_sem = model.encode(
        [query_record.semantic_text],
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).astype(np.float32)
    return _score_candidates(
        index=index,
        query_record=query_record,
        query_sem=query_sem,
        topk=topk,
        candidate_pool=candidate_pool,
        scoring_mode=scoring_mode,
        exclude_same_id=exclude_same_id,
        hybrid_weights=hybrid_weights,
    )


def search_index_with_embedding(
    index: Union[HybridEmbeddingIndex, HybridDiskIndex],
    query_record: FunctionRecord,
    query_embedding: np.ndarray,
    topk: int = 5,
    exclude_same_id: bool = True,
    hybrid_weights: Optional[Dict[str, float]] = None,
    candidate_pool: Optional[int] = None,
    scoring_mode: str = "jaccard",
) -> List[Dict[str, Any]]:
    """Search using a pre-computed query embedding (skips model.encode)."""
    if scoring_mode not in {"jaccard", "bonus", "bonus_v2"}:
        raise ValueError(f"Unsupported scoring_mode: {scoring_mode}")
    query_sem = query_embedding.reshape(1, -1).astype(np.float32)
    return _score_candidates(
        index=index,
        query_record=query_record,
        query_sem=query_sem,
        topk=topk,
        candidate_pool=candidate_pool,
        scoring_mode=scoring_mode,
        exclude_same_id=exclude_same_id,
        hybrid_weights=hybrid_weights,
    )


# =========================
# Query helpers
# =========================
def find_record_by_id(index: HybridEmbeddingIndex, function_id: str) -> FunctionRecord:
    for r in index.records:
        if r.function_id == function_id:
            return r
    raise KeyError(f"function_id not found: {function_id}")


def build_query_record_from_file(json_file: Path, query_func: str) -> FunctionRecord:
    funcs = parse_feature_json_file(json_file)
    target = None
    for f in funcs:
        if str(f.get("function_name", "")) == query_func:
            target = f
            break

    if target is None:
        raise KeyError(f"function_name '{query_func}' not found in {json_file}")

    return FunctionRecord(
        function_id=f"{json_file.name}::{target.get('function_name')}@{target.get('entry_point')}",
        source_json=str(json_file),
        function_name=str(target.get("function_name", "unknown_function")),
        metadata={
            "lua_version": target.get("lua_version"),
            "architecture": target.get("architecture"),
            "source_json": str(json_file),
        },
        raw_features=target,
        symbolic_tokens=build_symbolic_tokens(target),
        semantic_text=build_semantic_text(target),
        numeric_vector=build_numeric_vector(target),
    )


def print_results(query_title: str, results: List[Dict[str, Any]]) -> None:
    print("=" * 100)
    print(query_title)
    print("=" * 100)
    for rank, r in enumerate(results, 1):
        print(f"#{rank} total={r['score_total']:.4f}")
        print(f"  id      : {r['function_id']}")
        print(f"  file    : {r['source_json']}")
        print(
            "  scores  : "
            f"sym={r['score_breakdown']['symbolic']:.4f}, "
            f"num={r['score_breakdown']['numeric']:.4f}, "
            f"sem={r['score_breakdown']['semantic']:.4f}"
        )
        print(f"  meta    : {r['metadata']}")
        print("-" * 100)


# =========================
# CLI commands
# =========================
def cmd_build(args: argparse.Namespace) -> None:
    input_dir = Path(args.input_dir)
    index_out = Path(args.index_out)

    print(f"[INFO] building index from {input_dir}")
    index = build_index(
        input_dir=input_dir,
        embedding_model_name=args.embedding_model,
        batch_size=args.batch_size,
    )

    if index_out.suffix == ".pkl":
        index_out.parent.mkdir(parents=True, exist_ok=True)
        with open(index_out, "wb") as f:
            pickle.dump(index, f)
        print(f"[OK] saved legacy pickle index: {index_out}")
    else:
        save_directory_index(index, index_out)
        print(f"[OK] saved directory index: {index_out}")

    print(f"[INFO] total records: {len(index.records)}")
    print(f"[INFO] semantic model: {index.semantic_model_name}")


def cmd_search(args: argparse.Namespace) -> None:
    index_path = Path(args.index)
    index = load_index(index_path)

    query = find_record_by_id(index, args.query_id)
    results = search_index(
        index,
        query,
        topk=args.topk,
        candidate_pool=args.candidate_pool,
        scoring_mode=args.scoring_mode,
    )

    if args.save_json:
        out = Path(args.save_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"[OK] saved results: {out}")

    print_results(f"[QUERY] {query.function_id}", results)


def cmd_search_file(args: argparse.Namespace) -> None:
    index_path = Path(args.index)
    index = load_index(index_path)

    query = build_query_record_from_file(Path(args.query_file), args.query_func)
    results = search_index(
        index,
        query,
        topk=args.topk,
        exclude_same_id=False,
        candidate_pool=args.candidate_pool,
        scoring_mode=args.scoring_mode,
    )

    if args.save_json:
        out = Path(args.save_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"[OK] saved results: {out}")

    print_results(f"[QUERY FILE] {args.query_file}\n[QUERY FUNC] {args.query_func}", results)
    ###================
    unique_results = collapse_by_function_name(results)

    print("=" * 80)
    print("[UNIQUE FAMILY TOPK]")
    print("=" * 80)
    for rank, r in enumerate(unique_results[:args.topk], 1):
        print(f"#{rank} total={r['score_total']:.4f}")
        print(f"  fn      : {r['function_name']}")
        print(f"  id      : {r['function_id']}")
        print(f"  file    : {r['source_json']}")
        print(
            "  scores  : "
            f"sym={r['score_breakdown']['symbolic']:.4f}, "
            f"num={r['score_breakdown']['numeric']:.4f}, "
            f"sem={r['score_breakdown']['semantic']:.4f}"
        )
        print("-" * 80)

def cmd_list_functions(args: argparse.Namespace) -> None:
    index_path = Path(args.index)
    index = load_index(index_path)

    limit = args.limit if args.limit is not None else len(index.records)

    print(f"[INFO] total functions in index: {len(index.records)}")
    print(f"[INFO] semantic model: {index.semantic_model_name}")
    print("=" * 100)
    for i, rec in enumerate(index.records[:limit], 1):
        print(f"{i:6d} | {rec.function_id}")
    print("=" * 100)


# =========================
# CLI
# =========================
def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Hybrid retrieval with semantic embeddings")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="Build retrieval index")
    p_build.add_argument("--input-dir", required=True, help="Root directory of extracted feature JSONs")
    p_build.add_argument(
        "--index-out",
        required=True,
        help="Output path. Use .pkl for legacy pickle, or a directory path for split index files",
    )
    p_build.add_argument(
        "--embedding-model",
        default="BAAI/bge-small-en-v1.5",
        help="SentenceTransformer model name",
    )
    p_build.add_argument("--batch-size", type=int, default=128, help="Embedding batch size")
    p_build.set_defaults(func=cmd_build)

    p_search = sub.add_parser("search", help="Search by function_id already in index")
    p_search.add_argument("--index", required=True, help="Index path (.pkl or directory)")
    p_search.add_argument("--query-id", required=True, help="Exact function_id")
    p_search.add_argument("--topk", type=int, default=5)
    p_search.add_argument("--candidate-pool", type=int, default=None, help="Semantic candidate pool size")
    p_search.add_argument("--scoring-mode", choices=["jaccard", "bonus", "bonus_v2"], default="jaccard")
    p_search.add_argument("--save-json", help="Optional path to save search results as JSON")
    p_search.set_defaults(func=cmd_search)

    p_search_file = sub.add_parser("search-file", help="Search using a raw feature JSON file + function name")
    p_search_file.add_argument("--index", required=True, help="Index path (.pkl or directory)")
    p_search_file.add_argument("--query-file", required=True, help="Feature JSON file path")
    p_search_file.add_argument("--query-func", required=True, help="function_name inside that JSON")
    p_search_file.add_argument("--topk", type=int, default=5)
    p_search_file.add_argument("--candidate-pool", type=int, default=None, help="Semantic candidate pool size")
    p_search_file.add_argument("--scoring-mode", choices=["jaccard", "bonus", "bonus_v2"], default="jaccard")
    p_search_file.add_argument("--save-json", help="Optional path to save search results as JSON")
    p_search_file.set_defaults(func=cmd_search_file)

    p_list = sub.add_parser("list-functions", help="List function_ids stored in the index")
    p_list.add_argument("--index", required=True, help="Index path (.pkl or directory)")
    p_list.add_argument("--limit", type=int, default=50, help="How many function ids to print")
    p_list.set_defaults(func=cmd_list_functions)

    return p


def main() -> None:
    parser = build_argparser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()


# python lua_function_embedding/scripts/03_hybrid_retrieval_embedding.py build \
#   --input-dir lua_function_embedding/data/filtered_functions \
#   --index-out lua_function_embedding/data/indexes/lua547_x86_bge \
#   --embedding-model BAAI/bge-small-en-v1.5
