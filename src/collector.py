# collector.py - 웹페이지 수집·텍스트 추출 (docs/SPEC.md 2.1)
# 페이지를 가져와 제목·본문 텍스트만 뽑고, 실패하면 이유를 분류한다. 관련도 판정·저장은 하지 않는다.
# 수집기는 도메인별로 교체·추가할 수 있다(COLLECTORS). 1차는 일반 HTML 페이지만 지원한다.
# 사용자가 준 주소로 서버가 요청을 보내므로, 내부망·로컬 주소는 리다이렉트 홉마다 막고(SSRF 방지) 전체 시간·크기를 직접 제한한다.
import ipaddress
import logging
import re
import socket
import time
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src import config

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
    "blocked_address": "내부망·로컬 주소라 안전을 위해 열지 않았습니다",
    "too_large": "페이지가 너무 커서 읽지 않았습니다",
}
REDIRECT_CODES = (301, 302, 303, 307, 308)
# 본문이 아닌 파일로 보는 응답 형식 (Content-Type 앞부분)
NON_HTML_TYPES = ("image/", "video/", "audio/", "application/pdf", "application/zip", "application/octet-stream")
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


def address_failure(url: str) -> str | None:
    """요청 직전에 주소의 호스트를 IP로 풀어 공개 주소인지 본다. 내부망·루프백·링크로컬(클라우드 메타데이터 169.254.x.x)·
    예약 주소면 'blocked_address'. 설정 허용 목록(COLLECT_ALLOWED_HOSTS)의 host·host:port는 통과. 문제가 없으면 None."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return "invalid_url"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if host in config.COLLECT_ALLOWED_HOSTS or f"{host}:{port}" in config.COLLECT_ALLOWED_HOSTS:
        return None
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return "network"
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:  # 하나라도 공개 주소가 아니면 막는다
            return "blocked_address"
    return None


def fetch_html(fetch_url: str) -> tuple[str, str | None, str]:
    """페이지를 가져와 (HTML 글, 실패 코드, 마지막 주소)를 돌려준다. 리다이렉트는 직접 따라가며 홉마다 주소를 검사하고,
    전체 시간(COLLECT_TIMEOUT_SECONDS)·크기(COLLECT_MAX_BYTES)를 넘으면 끊는다. 본문은 도착한 만큼씩(read1) 받아,
    조금씩 흘려보내는 서버도 '전체 한도 + 한 번 읽기 대기(COLLECT_READ_TIMEOUT_SECONDS)' 안에 끝난다. (R2)"""
    deadline = time.monotonic() + config.COLLECT_TIMEOUT_SECONDS
    url = fetch_url
    with requests.Session() as session:
        session.headers["User-Agent"] = config.COLLECT_USER_AGENT
        for _ in range(config.COLLECT_MAX_REDIRECTS + 1):
            failure = address_failure(url)
            if failure:
                return "", failure, url
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "", "timeout", url
            try:
                resp = session.get(url, stream=True, allow_redirects=False,
                                   timeout=(remaining, min(remaining, config.COLLECT_READ_TIMEOUT_SECONDS)))
            except requests.exceptions.Timeout:
                return "", "timeout", url
            except requests.exceptions.RequestException:
                return "", "network", url
            with resp:
                if resp.status_code in REDIRECT_CODES and resp.headers.get("Location"):
                    url = urljoin(url, resp.headers["Location"])
                    continue
                if resp.status_code >= 400:
                    return "", "http_error", url
                if resp.headers.get("Content-Type", "").lower().startswith(NON_HTML_TYPES):
                    return "", "not_html", url
                data = bytearray()
                try:
                    while piece := resp.raw.read1(65_536, decode_content=True):  # 도착한 만큼 (압축은 풀어서 센다)
                        data += piece
                        if len(data) > config.COLLECT_MAX_BYTES:
                            return "", "too_large", url
                        if time.monotonic() > deadline:
                            return "", "timeout", url
                except Exception as e:  # 읽기 중 시간 초과·연결 끊김 (urllib3 예외)
                    return "", ("timeout" if "timed out" in str(e).lower() else "network"), url
                resp._content = bytes(data)  # 받은 본문으로 인코딩을 추정한다(WebBaseLoader와 같은 apparent_encoding)
                resp.encoding = resp.apparent_encoding
                return resp.text, None, url
    return "", "http_error", url  # 리다이렉트가 너무 많다


def _collect_web(url: str, fetch_url: str) -> CollectResult:
    """일반 HTML 페이지 수집기. fetch_html로 가져오고 bs4로 제목·본문을 뽑는다."""
    html, failure, fetched = fetch_html(fetch_url)
    if failure:
        return CollectResult(url, False, failure=failure, fetched_url=fetched)
    title, text = extract_text(BeautifulSoup(html, "html.parser"))
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
