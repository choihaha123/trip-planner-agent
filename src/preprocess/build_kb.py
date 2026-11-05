# build_kb.py - 기본 KB 적재 스크립트 (docs/SPEC.md 1.4)
# data/kb/*.md 를 읽어 청킹·임베딩 후 기본 KB 컬렉션에 저장한다. CLI 실행과 별개로, 설치 후 한 번 실행한다.
# 실행: python -m src.preprocess.build_kb
# 문서 형식: 머리말(title / source_url / collected_at) + '---' 줄 + 본문
import sys
from pathlib import Path

from src import config
from src.retriever import replace_kb

REQUIRED_FIELDS = ("title", "source_url", "collected_at")


def parse_kb_file(path: Path) -> dict:
    """KB 문서 파일 하나를 읽어 머리말 필드와 본문(text)을 담은 dict로 돌려준다. 필드가 빠지면 ValueError."""
    header, sep, body = path.read_text(encoding="utf-8").partition("\n---\n")
    if not sep:
        raise ValueError(f"{path.name}: 머리말과 본문을 나누는 '---' 줄이 없습니다.")
    page = {}
    for line in header.splitlines():
        key, _, value = line.partition(":")
        if key.strip() in REQUIRED_FIELDS:
            page[key.strip()] = value.strip()
    missing = [f for f in REQUIRED_FIELDS if not page.get(f)]
    if missing:
        raise ValueError(f"{path.name}: 머리말 필드가 없습니다: {', '.join(missing)}")
    page["text"] = body.strip()
    return page


def main() -> int:
    """data/kb 의 모든 문서를 읽어 기본 KB 컬렉션을 새로 만든다."""
    config.setup_logging()
    files = sorted(config.KB_DATA_DIR.glob("*.md"))
    if not files:
        print(f"오류: {config.KB_DATA_DIR} 에 문서(.md)가 없습니다.")
        return 1
    try:
        pages = [parse_kb_file(f) for f in files]
    except ValueError as e:
        print(f"오류: {e}")
        return 1
    chunks = replace_kb(pages)
    print(f"기본 KB 적재 완료: 문서 {len(pages)}개, 청크 {chunks}개 → {config.CHROMA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
