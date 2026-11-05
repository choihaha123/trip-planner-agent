# retriever.py - RAG 파이프라인 (docs/SPEC.md 1.4, 2.7)
# 기본 KB 컬렉션과 사용자 수집 컬렉션을 Chroma에 나눠 두고, 청킹·임베딩·저장·검색·해시 비교·삭제·초기화를 맡는다.
# user_id 분리는 LLM이 아니라 이 파일의 where 필터(파이썬 코드)로 보장한다.
import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from langchain_aws import BedrockEmbeddings
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from src import config

logger = logging.getLogger(__name__)


class ParallelBedrockEmbeddings(BedrockEmbeddings):
    """여러 청크를 동시에 임베딩하는 BedrockEmbeddings. 기본 구현은 청크를 하나씩 차례로 호출해 긴 글에서 느리다.
    결과 순서는 입력 순서와 같다."""

    def _iteratively_embed_documents(self, texts: list[str]) -> list[list[float]]:
        """청크마다 임베딩 호출을 스레드로 동시에 보낸다."""
        def embed_one(text: str) -> list[float]:
            vector = self._embedding_func(text)
            return self._normalize_vector(vector) if self.normalize else vector

        if len(texts) <= 1:
            return [embed_one(t) for t in texts]
        with ThreadPoolExecutor(max_workers=min(config.EMBED_MAX_WORKERS, len(texts))) as pool:
            return list(pool.map(embed_one, texts))


@lru_cache(maxsize=1)
def get_embeddings() -> BedrockEmbeddings:
    """설정값의 모델 ID·리전으로 Bedrock 임베딩 모델을 만든다. (OPEN_ISSUES #6: Titan V2, 청크 동시 임베딩)"""
    return ParallelBedrockEmbeddings(model_id=config.EMBEDDING_MODEL_ID, region_name=config.BEDROCK_REGION)


_stores: dict[str, Chroma] = {}
_store_lock = threading.Lock()


def get_store(collection_name: str) -> Chroma:
    """이름에 맞는 Chroma 컬렉션을 연다(한 번만 만들고 재사용). 저장 위치는 config.CHROMA_DIR.
    URL을 동시에 처리할 때 여러 스레드가 처음 연결을 같이 만들면 Chroma 초기화가 실패하므로 잠금으로 한 번에 하나만 만든다."""
    with _store_lock:
        if collection_name not in _stores:
            config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
            _stores[collection_name] = Chroma(
                collection_name=collection_name,
                embedding_function=get_embeddings(),
                persist_directory=str(config.CHROMA_DIR),
            )
        return _stores[collection_name]


def _check_user_id(user_id: str) -> str:
    """사용자 수집 컬렉션에 쓸 user_id를 검사한다. 비었거나 예약값('__'로 시작)이면 ValueError."""
    user_id = (user_id or "").strip()
    if not user_id or user_id.startswith(config.RESERVED_USER_PREFIX):
        raise ValueError(f"사용자 수집 컬렉션에 쓸 수 없는 user_id입니다: '{user_id}'")
    return user_id


def content_hash(text: str) -> str:
    """본문 전체 텍스트의 SHA-256 해시를 만든다. 같은 URL 재입력 시 내용 변경 판단에 쓴다."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    """설정 시간대 기준 현재 시각을 ISO 8601 문자열로 만든다."""
    return datetime.now(ZoneInfo(config.TIMEZONE)).isoformat(timespec="seconds")


def split_page(text: str, metadata: dict) -> list[Document]:
    """페이지 본문을 청크로 나누고 모든 청크에 같은 메타데이터를 붙인다."""
    splitter = RecursiveCharacterTextSplitter(chunk_size=config.CHUNK_SIZE, chunk_overlap=config.CHUNK_OVERLAP)
    return [Document(page_content=chunk, metadata=dict(metadata)) for chunk in splitter.split_text(text)]


def _ids_where(store: Chroma, where: dict) -> list[str]:
    """where 조건에 맞는 청크 id 목록을 돌려준다."""
    return store.get(where=where, include=[])["ids"]


def _user_source_where(user_id: str, source_url: str) -> dict:
    """사용자 수집 컬렉션에서 user_id + source_url 로 한 페이지를 찾는 where 조건."""
    return {"$and": [{"user_id": user_id}, {"source_url": source_url}]}


def add_kb_page(text: str, source_url: str, title: str, collected_at: str) -> int:
    """기본 KB 컬렉션에 문서 하나를 저장한다. 같은 source_url이 있으면 지우고 다시 넣는다. 저장한 청크 수를 돌려준다."""
    store = get_store(config.KB_COLLECTION)
    old_ids = _ids_where(store, {"source_url": source_url})
    if old_ids:
        store.delete(ids=old_ids)
    metadata = {
        "user_id": config.KB_USER_ID,
        "source_url": source_url,
        "title": title,
        "content_hash": content_hash(text),
        "collected_at": collected_at,
    }
    docs = split_page(text, metadata)
    if docs:
        store.add_documents(docs)
    logger.info("기본 KB 저장", extra={"event": "kb_saved", "data": {"source_url": source_url, "chunks": len(docs)}})
    return len(docs)


def replace_kb(pages: list[dict]) -> int:
    """기본 KB 컬렉션을 비우고 pages(text·source_url·title·collected_at)로 새로 채운다. 저장한 청크 수를 돌려준다.
    KB 적재 스크립트(src/preprocess/build_kb.py) 전용이다. 사용자 초기화(reset_user)는 이 함수를 쓰지 않는다."""
    store = get_store(config.KB_COLLECTION)
    old_ids = store.get(include=[])["ids"]
    if old_ids:
        store.delete(ids=old_ids)
    total = sum(add_kb_page(p["text"], p["source_url"], p["title"], p["collected_at"]) for p in pages)
    logger.info("기본 KB 재적재", extra={"event": "kb_rebuilt", "data": {"pages": len(pages), "chunks": total}})
    return total


def stored_content_hash(user_id: str, source_url: str) -> str | None:
    """사용자 수집 컬렉션에 저장된 페이지의 content_hash를 돌려준다. 저장된 적이 없으면 None."""
    user_id = _check_user_id(user_id)
    found = get_store(config.USER_COLLECTION).get(where=_user_source_where(user_id, source_url), include=["metadatas"],
                                                  limit=1)
    return found["metadatas"][0].get("content_hash") if found["ids"] else None


def save_user_page(user_id: str, text: str, source_url: str, title: str, relevance_score: float) -> str:
    """사용자 수집 컬렉션에 페이지 하나를 저장한다. (SPEC 1.4)
    같은 user_id·source_url이 있으면 content_hash를 비교해 같으면 건너뛰고, 다르면 기존 청크를 지우고 덮어쓴다.
    결과로 'saved' / 'updated' / 'unchanged' 중 하나를 돌려준다."""
    user_id = _check_user_id(user_id)
    store = get_store(config.USER_COLLECTION)
    new_hash = content_hash(text)

    existing = store.get(where=_user_source_where(user_id, source_url), include=["metadatas"])
    status = "saved"
    if existing["ids"]:
        if existing["metadatas"][0].get("content_hash") == new_hash:
            logger.info("같은 내용이라 저장 건너뜀", extra={"event": "page_unchanged",
                                                          "data": {"user_id": user_id, "source_url": source_url}})
            return "unchanged"
        store.delete(ids=existing["ids"])
        status = "updated"

    metadata = {
        "user_id": user_id,
        "source_url": source_url,
        "title": title,
        "content_hash": new_hash,
        "collected_at": now_iso(),
        "relevance_score": float(relevance_score),
    }
    docs = split_page(text, metadata)
    if docs:
        store.add_documents(docs)
    logger.info("사용자 페이지 저장", extra={"event": f"page_{status}", "data": {
        "user_id": user_id, "source_url": source_url, "chunks": len(docs)}})
    return status


def search(query: str, user_id: str, k: int | None = None) -> list[Document]:
    """기본 KB 전체 + 사용자 수집 컬렉션 중 현재 user_id 청크만 검색해 가까운 순서로 k개 돌려준다. (SPEC 1.4)"""
    k = k or config.RAG_TOP_K
    user_id = _check_user_id(user_id)
    kb_hits = get_store(config.KB_COLLECTION).similarity_search_with_score(query, k=k)
    user_hits = get_store(config.USER_COLLECTION).similarity_search_with_score(
        query, k=k, filter={"user_id": user_id})
    # 두 컬렉션 모두 같은 임베딩·거리 기준이므로 거리(작을수록 가까움)로 합쳐 정렬한다.
    hits = sorted(kb_hits + user_hits, key=lambda pair: pair[1])
    return [doc for doc, _ in hits[:k]]


def delete_user_source(user_id: str, source_url: str) -> int:
    """사용자 수집 컬렉션에서 user_id의 source_url 한 페이지 청크를 지운다. 지운 청크 수를 돌려준다."""
    user_id = _check_user_id(user_id)
    store = get_store(config.USER_COLLECTION)
    ids = _ids_where(store, _user_source_where(user_id, source_url))
    if ids:
        store.delete(ids=ids)
    logger.info("사용자 페이지 삭제", extra={"event": "page_deleted", "data": {
        "user_id": user_id, "source_url": source_url, "chunks": len(ids)}})
    return len(ids)


def list_user_pages(user_id: str) -> list[dict]:
    """user_id가 저장한 링크(페이지) 목록을 저장한 순서대로 돌려준다. 항목: source_url, title, collected_at. (SERVICE 2.1)
    청크 단위로 저장돼 있으므로 source_url 하나당 한 항목으로 묶는다. 다른 사용자의 자료는 조회하지 않는다."""
    user_id = _check_user_id(user_id)
    found = get_store(config.USER_COLLECTION).get(where={"user_id": user_id}, include=["metadatas"])
    pages = {}
    for m in found["metadatas"]:
        m = m or {}
        pages.setdefault(m.get("source_url", ""), {"source_url": m.get("source_url", ""), "title": m.get("title", ""),
                                                   "collected_at": m.get("collected_at", "")})
    return sorted(pages.values(), key=lambda p: (p["collected_at"], p["source_url"]))


def reset_user(user_id: str) -> tuple[int, int]:
    """user_id의 수집 데이터를 모두 지운다. 사용자 수집 컬렉션만 대상이라 기본 KB는 지워지지 않는다. (SPEC 2.7)
    (지운 링크(페이지) 수, 지운 청크 수)를 돌려준다. 사용자 안내에는 링크 수를 쓴다."""
    user_id = _check_user_id(user_id)
    store = get_store(config.USER_COLLECTION)
    found = store.get(where={"user_id": user_id}, include=["metadatas"])
    ids = found["ids"]
    pages = len({(m or {}).get("source_url") for m in found["metadatas"]})
    if ids:
        store.delete(ids=ids)
    logger.info("사용자 데이터 초기화", extra={"event": "user_reset", "data": {
        "user_id": user_id, "pages": pages, "chunks": len(ids)}})
    return pages, len(ids)
