# collector.py - 웹페이지 수집·텍스트 추출 (docs/SPEC.md 2.1)
# 페이지를 가져와 제목·본문 텍스트만 뽑고, 실패하면 이유를 분류한다. 관련도 판정·저장은 하지 않는다.
# 수집기는 도메인별로 교체·추가할 수 있다(COLLECTORS). 1차는 일반 HTML 페이지만 지원한다.
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import requests

from src import config  # WebBaseLoader보다 먼저 읽어 USER_AGENT 설정값을 적용한다
from langchain_community.document_loaders import WebBaseLoader  # noqa: E402

logger = logging.getLogger(__name__)

# 실패 이유 코드 → 사용자 안내 문구
FAILURE_MESSAGES = {
    "video": "동영상 페이지라 내용을 읽을 수 없습니다",
    "login_required": "로그인이 필요한 페이지라 내용을 읽을 수 없습니다",
    "not_html": "웹페이지가 아닌 파일(PDF·이미지 등)이라 읽을 수 없습니다",
    "http_error": "페이지를 열 수 없습니다(없는 페이지이거나 접근이 막혀 있습니다)",
    "timeout": "페이지 응답이 너무 늦어 읽지 못했습니다",
    "network": "페이지에 연결하지 못했습니다",
    "no_content": "본문을 찾지 못했습니다(자바스크립트로 그리는 페이지 등)",
    "invalid_url": "올바른 웹 주소가 아닙니다",
}
# 1차에서 지원하지 않는 사이트 (도메인 끝부분 일치)
VIDEO_DOMAINS = ("youtube.com", "youtu.be", "tiktok.com", "vimeo.com", "tv.naver.com")
LOGIN_DOMAINS = ("instagram.com", "facebook.com", "x.com", "twitter.com", "threads.net")
NON_HTML_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip", ".mp4", ".mp3", ".doc", ".docx",
                       ".xls", ".xlsx", ".ppt", ".pptx")
REMOVE_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "svg", "button"]
# 알려진 본문 영역 (앞에서부터 먼저 찾는다). 예: 네이버 블로그 스마트에디터 본문
CONTENT_SELECTORS = ["div.se-main-container", "div#postViewArea", "main", "article"]


@dataclass
class CollectResult:
    """수집 결과. ok면 title·text가 있고, 실패면 failure에 이유 코드가 있다. url은 사용자가 넣은 원래 주소."""
    url: str
    ok: bool
    title: str = ""
    text: str = ""
    failure: str | None = None
    truncated: bool = False
    fetched_url: str = ""

    @property
    def failure_message(self) -> str:
        """실패 이유를 사용자 안내 문구로 돌려준다."""
        return FAILURE_MESSAGES.get(self.failure or "", "")


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    """host가 도메인 목록 중 하나이거나 그 하위 도메인인지 판정한다."""
    return any(host == d or host.endswith("." + d) for d in domains)


def rewrite_for_fetch(url: str) -> str:
    """수집이 잘 되는 주소로 바꾼다. 네이버 블로그 PC 주소(iframe 구조)는 모바일 주소로 바꾼다."""
    parsed = urlparse(url)
    if parsed.netloc.lower() in ("blog.naver.com", "www.blog.naver.com"):
        query = parse_qs(parsed.query)
        if "blogId" in query and "logNo" in query:  # blog.naver.com/PostView.naver?blogId=..&logNo=..
            return f"https://m.blog.naver.com/{query['blogId'][0]}/{query['logNo'][0]}"
        return f"https://m.blog.naver.com{parsed.path}"  # blog.naver.com/{blogId}/{logNo}
    return url


def precheck(url: str) -> str | None:
    """가져오기 전에 주소만 보고 실패 이유를 판정한다. 문제가 없으면 None."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    if parsed.scheme not in ("http", "https") or not host:
        return "invalid_url"
    if _host_matches(host, VIDEO_DOMAINS):
        return "video"
    if _host_matches(host, LOGIN_DOMAINS):
        return "login_required"
    if parsed.path.lower().endswith(NON_HTML_EXTENSIONS):
        return "not_html"
    return None


def densest_paragraph_node(soup):
    """문단(<p>) 글자 수가 가장 많이 모인 영역을 고른다. 본문 영역 태그가 없는 사이트용. 문단이 없으면 None."""
    score: dict[int, int] = defaultdict(int)
    nodes = {}
    for p in soup.find_all("p"):
        size = len(p.get_text(strip=True))
        parent = p.parent
        for weight in (1, 2, 3):  # 부모에 많이, 조상으로 갈수록 적게 점수를 준다
            if parent is None:
                break
            score[id(parent)] += size // weight
            nodes[id(parent)] = parent
            parent = parent.parent
    return nodes[max(score, key=score.get)] if score else None


def extract_text(soup) -> tuple[str, str]:
    """BeautifulSoup 문서에서 (제목, 본문 텍스트)를 뽑는다. 메뉴·광고·스크립트 등은 지우고 알려진 본문 영역을 우선한다."""
    title = " ".join(soup.title.get_text().split()) if soup.title else ""
    for tag in soup(REMOVE_TAGS):
        tag.decompose()
    node = next((found for sel in CONTENT_SELECTORS if (found := soup.select_one(sel))), None)
    if node is None:  # 본문 영역 태그가 없으면 문단이 가장 많은 영역, 그것도 너무 짧으면 body 전체
        dense = densest_paragraph_node(soup)
        node = dense if dense is not None and len(dense.get_text(strip=True)) >= config.COLLECT_MIN_CHARS else None
    node = node or soup.body or soup
    lines = [" ".join(line.split()) for line in node.get_text("\n").splitlines()]
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text


def _collect_web(url: str, fetch_url: str) -> CollectResult:
    """일반 HTML 페이지 수집기. WebBaseLoader로 가져오고 bs4로 제목·본문을 뽑는다."""
    loader = WebBaseLoader(fetch_url, raise_for_status=True,
                           requests_kwargs={"timeout": config.COLLECT_TIMEOUT_SECONDS},
                           header_template={"User-Agent": config.COLLECT_USER_AGENT})
    try:
        soup = loader.scrape()
    except requests.exceptions.Timeout:
        return CollectResult(url, False, failure="timeout", fetched_url=fetch_url)
    except requests.exceptions.HTTPError:
        return CollectResult(url, False, failure="http_error", fetched_url=fetch_url)
    except requests.exceptions.RequestException:
        return CollectResult(url, False, failure="network", fetched_url=fetch_url)
    title, text = extract_text(soup)
    if len(text) < config.COLLECT_MIN_CHARS:
        return CollectResult(url, False, title=title, failure="no_content", fetched_url=fetch_url)
    truncated = len(text) > config.COLLECT_MAX_CHARS
    return CollectResult(url, True, title=title or url, text=text[: config.COLLECT_MAX_CHARS],
                         truncated=truncated, fetched_url=fetch_url)


# 도메인별 수집기 (도메인 끝부분, 수집 함수). 맞는 것이 없으면 일반 HTML 수집기를 쓴다.
COLLECTORS: list[tuple[tuple[str, ...], callable]] = []


def collect(url: str) -> CollectResult:
    """URL 하나를 수집한다. 사전 판정 → 주소 변환 → 도메인별 수집기 순서로 처리하고, 결과를 로그에 남긴다."""
    reason = precheck(url)
    if reason:
        result = CollectResult(url, False, failure=reason)
    else:
        fetch_url = rewrite_for_fetch(url)
        host = urlparse(fetch_url).netloc.lower()
        collector = next((fn for domains, fn in COLLECTORS if _host_matches(host, domains)), _collect_web)
        result = collector(url, fetch_url)
    if result.ok:
        logger.info("페이지 수집", extra={"event": "page_collected", "data": {
            "url": url, "chars": len(result.text), "truncated": result.truncated}})
    else:
        logger.info("페이지 수집 실패", extra={"event": "collect_failed", "data": {"url": url, "reason": result.failure}})
    return result
