# OPEN_ISSUES.md — 미결 항목

> 아직 결정하지 않은 항목 목록이다. 이 항목에 해당하는 작업을 할 때는 추측하지 말고 사용자에게 먼저 확인한다.
> 결정되면 해당 내용을 관련 문서([SERVICE.md](SERVICE.md) · [SPEC.md](SPEC.md) · [EVALUATION.md](EVALUATION.md) · [SCENARIOS.md](SCENARIOS.md))에 반영하고, 여기서는 '결정됨'으로 옮긴다.

## 미결

| # | 항목 | 내용 | 결정 시점 |
|---|---|---|---|
| 8 | (선택) MCP 서버 설계 | MCP 라이브러리(후보: fastmcp, 신규 승인 필요), 노출 범위, 실행 방식 | TASKS Step 14 시작 전 |

## 결정됨
- (2026-11-05) #9 웹페이지 설계: 백엔드 FastAPI(신규 승인, 0.162.0) + uvicorn(기존 설치). 화면은 빌드 도구 없는 HTML/CSS/JS를 FastAPI가 제공, 클로드·ChatGPT식(왼쪽 대화 목록 + 채팅 + 링크·기억 패널). 로그인은 이메일(중복 불가)·비밀번호, 비밀번호는 표준 `hashlib.scrypt`, 세션은 무작위 토큰을 SQLite에 저장하고 HttpOnly 쿠키로 전달(인증 라이브러리 없음). user_id는 이메일이 아닌 내부 ID(`u_` + 무작위). 답변 대기 중 단계 표시(SSE), 토큰 스트리밍은 나중. 동시 접속은 처음엔 에이전트 호출을 하나씩 처리(잠금). 1차 화면: 회원가입·로그인·로그아웃, 대화 목록(제목 자동·삭제)·새 대화, 마크다운 답변, 링크 패널(추가·목록·삭제·처리 결과), 기억 패널. 파일: `src/web/`(`__init__.py`·`app.py`·`auth.py`·`threads.py`·`static/`), `tests/test_web.py`, 데이터 `storage/web.sqlite`. → SERVICE.md 2.10 · TASKS.md Step 15 · CLAUDE.md · requirements.txt
- (2026-11-05) 추후 기능 순서 변경: 장기 기억 → **웹페이지(Step 15) → MCP 서버(Step 14)**. 두 기능은 같은 에이전트 코어를 감싸는 별개 출입구라 의존 관계가 없고, 웹을 먼저 하며 정리한 코어(대화 목록·구조화 데이터·동시 접속)를 MCP가 재사용한다. → SERVICE.md 1.5 · TASKS.md Phase 6
- (2026-11-05) #7 장기 기억 설계: 저장은 LangGraph `SqliteStore`(이미 설치된 `langgraph-checkpoint-sqlite`, 신규 라이브러리 없음) `storage/memory.sqlite`, 공간 `("users", user_id)`. 항목은 이름·호칭, 여행 선호, 답변 스타일(설정값이라 나중에 바꿀 수 있음, 출발지·동행은 넣지 않음). 매 턴 자동 추출: 파이썬 규칙으로 후보 메시지를 거른 뒤 판정 모델이 사용자가 직접 말한 정보만 항목별로 뽑고 파이썬이 검사·저장, 저장하면 답변 끝에 알림. 확인·삭제는 CLI 명령(`/memory`, `/forget-memory <항목>`), `--reset`은 저장 링크와 기억을 함께 지운다. 새 파일 `src/memory.py`·`tests/test_memory.py` 승인. 웹 확장 시 로그인 계정 ID를 user_id로 쓰면 그대로 이어진다. → SERVICE.md 1.5, 2.9 · TASKS.md Step 13 · CLAUDE.md
- (2026-11-05) Step 12 평가 실패 수정: ① 출처 URL 없는 일반 답변의 가격은 이번 턴 검색 결과에서 같은 금액을 찾아 '(출처: 제목 URL)'을 자동으로 붙이고, 없으면 '출처 없음, 사용자 확인 필요' ② 일정 요청 표현이 있는 턴은 첫 모델 호출에서 `check_trip_info`를 강제 호출(`tool_choice`). → SERVICE.md 2.2, 2.8 · SPEC.md 2.2, 2.8
- (2026-11-05) 검색 순서를 파이썬이 고정한다: 통합 도구 `search_travel_info`가 RAG(저장 자료+KB) 먼저 → Haiku가 질문·결과 관련성을 한 번에 판정해 관련 없는 결과 제외 → 관련 결과가 없으면 자동 웹 검색. 부족하면 LLM이 `web_search`로 보충(그 턴에 RAG를 안 했으면 웹 검색 도구가 RAG 먼저 수행). RAG 후보는 4 → 8개. `search_rag` 도구는 대체됨. → SERVICE.md 2.5 · SPEC.md 1.4, 2.1, 2.5 · EVALUATION.md 4 · evaluation/test_queries.csv
- (2026-11-05) #1 평가 기준: 전체 80%(16/20) 이상 + guardrail 3건 전부 통과. URL 세트는 실제 URL(관련 2·무관 1·수집 실패 2) + 인젝션은 `evaluation/fixtures/inject.html`을 로컬 웹서버로 제공. #2 판정 방식: 도구 호출·문자열 포함/미포함은 코드로, 문자열로 어려운 항목(`~` 표시)만 Haiku 4.5가 예/아니오 채점. 평가 스크립트는 `evaluation/run_eval.py`, 결과는 `evaluation/results/`. → EVALUATION.md 5, 6 · CLAUDE.md
- (2026-11-05) 링크 사용 방식은 현행 유지(관련도 통과해 저장된 링크만 답변에 사용)하되, 참고하지 못한 링크는 이유(관련도 낮음·지시문 의심·수집 실패·판정 실패·개수 초과)와 함께 답변 끝에 프로그램이 안내한다. 한 메시지 최대 URL은 3개 → 5개. Step 10의 '거절 URL 가리기'는 제거. → SERVICE.md 2.1, 2.8 · SPEC.md 1.5, 2.1, 2.8 · SCENARIOS.md 2 · EVALUATION.md 3 · TASKS.md Step 9, 10
- (2026-11-05) Step 10 가드레일: 시스템 프롬프트에 [가드레일] 절 추가, 거절·판정 실패·초과 URL은 모델 입력에서 주소를 지워 가림, 일반 답변의 가격은 이번 턴 도구 결과 금액(오차 1%)과 대조해 없는 것만 '출처 없음, 사용자 확인 필요' 표시. 인젝션 점검은 임시 폴더 HTML + 로컬 웹서버로 했다(평가용 URL 세트는 #1). → SERVICE.md 2.8 · SPEC.md 1.5, 2.1, 2.8 · TASKS.md Step 10 (URL 주소 가리기는 이후 2026-11-05 링크 사용 방식 결정으로 제거, 참고하지 못한 링크는 답변 끝 안내로 대체)
- (2026-11-05) 관련도 판정은 Haiku 4.5(본문 앞 3,000자, 한도 초과 시 다른 리전·global Haiku)로 점수·지시문 의심을 받고, 지시문은 파이썬 정규식으로 본문 전체에서도 검사해 둘 중 하나라도 의심하면 거절한다. → SERVICE.md 2.1 · SPEC.md 2.1
- (2026-11-05) #3 URL 전처리 한계: 한 메시지 최대 3개(초과분은 안내 없이 건너뜀 → 이후 5개·안내로 변경), 수집 타임아웃 15초, 본문 5만 자 초과 시 앞부분만 저장, 200자 미만은 '본문 없음' 실패. 네이버 블로그 PC 주소는 모바일 주소로 바꿔 수집. 속도는 URL 동시 처리·임베딩 병렬화·판정 경량화(Haiku 4.5, 본문 앞 3,000자)로 확보하고, 처리 순서(저장 후 답변)는 유지한다(백그라운드 저장 안 함). → SERVICE.md 2.1 · SPEC.md 1.5, 2.1 · TASKS.md Step 8, 9
- (2026-11-05) 출처 없는 가격은 숨기지 않고 가격 옆에 '(출처 없음, 사용자 확인 필요)'를 붙여 알린다. 일정표에서는 자유 텍스트 가격과 출처 없는 입장료 칸 모두 파이썬이 표시를 붙인다(규칙 통일). 예산 '계산'의 추정치 규칙(큰 항목만)은 그대로. → SERVICE.md 2.2, 2.4, 2.6 · SPEC.md 2.2 · SCENARIOS.md 3 · EVALUATION.md 3
- (2026-11-05) 장기 기억(thread 간 사용자 정보 기억), MCP 서버, 웹페이지를 '추후 추가 기능'으로 두고 1차 완료·제출 후 시간 여유가 있을 때만 이 순서로 진행한다. 세부 설계는 미결 #7~#9. → SERVICE.md 1.3~1.5 · TASKS.md Phase 6
- (2026-11-05) 일정표는 도구 `render_itinerary`가 구조화 데이터를 검증·렌더링하고, 그 결과를 LLM이 다시 쓰지 않고 그대로 최종 답변으로 보여준다. 예산은 일정표 도구 안에서 계산해 일차별 비용·예산 요약을 함께 넣고, 예산 초과면 조정안이 있어야 렌더링된다. → SERVICE.md 2.2 · SPEC.md 2.2
- (2026-11-05) 정보 확인은 같은 대화라도 목적지가 바뀌면 처음부터 다시 하고, 사용자 답이 오기 전에는 가정으로 넘어가지 않는다. 시나리오 3은 '요청 → 인원·출발지 되묻기 → 답(또는 알아서) → 일정표' 흐름으로 바꾼다. → SERVICE.md 2.2 · SPEC.md 2.2 · SCENARIOS.md 3
- (2026-11-05) 정보 부족 처리는 도구 `check_trip_info`(`src/tools.py`)로 구현한다(미들웨어 강제 아님). 한 대화에서 한 번 물어본 항목은 다시 묻지 않고, 답이 없으면 가정으로 진행한다(파이썬이 대화 기록의 이전 결과로 판정). → SERVICE.md 2.2 · SPEC.md 2.2
- (2026-11-05) `resolve_dates` 입력은 구조화 인자(`weeks_from_now`+`weekday` / `days_from_today` / `date` 중 하나, + `nights`), 단위 테스트는 `tests/test_dates.py`(unittest). → SPEC.md 2.4 · CLAUDE.md
- (2026-11-05) 상대 날짜 변환도 LLM이 아니라 날짜 계산 도구 `resolve_dates`(파이썬)가 한다. 한 주는 월~일, '다음 주'는 다음 주 월요일~일요일, 주말은 그 주 토~일. 여행 기간(출발일 + 박 수 → 귀국일·일차별 날짜)도 같은 도구가 계산. 구현은 TASKS Step 7. → SERVICE.md 2.4 · SPEC.md 1.2, 2.4 · TASKS.md Step 7
- (2026-11-05) 예산 계산 세부: 판정은 최대값 기준(≤예산 안 / ≤예산×1.1 근접 / 초과), 비용 단위 5개(1인 1회·1인 1일·객실 1박·일행 1회·일행 1일), 일자별 합계는 일자 배분 + '여행 전체' 칸, 출처 없는 추정치는 큰 항목만(파이썬 판정). 단위 테스트는 `tests/test_budget.py`(unittest). → SERVICE.md 2.3 · SPEC.md 2.3 · CLAUDE.md
- (2026-11-05) 실행 추적 저장 위치는 `logs/trace.jsonl` 별도 파일, 입력·출력 본문은 앞 200자만 기록. → SPEC.md 1.5
- (2026-11-05) #5 날씨·환율 API 호출용 HTTP 클라이언트는 `httpx`(이미 설치됨, requirements.txt에 명시). 환율 캐시 유지 시간은 1시간. → SPEC.md 1.5, 1.6, 2.4 · requirements.txt
- (2026-11-04) 기본 모델이 토큰 한도(Throttling)에 걸리면 대체 모델로 자동 전환한다. 같은 Sonnet 4.5의 다른 리전/global 프로필 → Sonnet 4.6 → Haiku 4.5 순, `.env`로 변경 가능. → SPEC.md 1.5
- (2026-11-04) #4 기본 KB 대상 도시는 오사카·도쿄·교토. 원본은 `data/kb/*.md`(머리말 title/source_url/collected_at + 본문), 적재는 별도 스크립트 `src/preprocess/build_kb.py`로 한 번 실행하고 CLI는 읽기만 한다. 적재된 벡터DB는 git에 올리지 않는다. → SERVICE.md 3 · SPEC.md 1.2, 1.4 · CLAUDE.md 폴더 구조
- (2026-11-04) #6 임베딩 모델은 Bedrock Titan V2(`amazon.titan-embed-text-v2:0`, `langchain-aws`의 `BedrockEmbeddings`, ref 코드와 동일). `.env`의 `EMBEDDING_MODEL_ID`로 교체 가능. Chroma 저장 경로는 `storage/chroma`(`.env`의 `CHROMA_DIR`, 이미 git 제외). → SPEC.md 1.5, 1.6
- (2026-11-04) 대화 기록 DB는 `storage/checkpoints.sqlite`, thread 소유자는 체크포인트 메타데이터의 `user_id`로 저장한다. → SPEC.md 1.3, 1.5
- (2026-11-04) LLM·도구 호출을 기록하는 실행 추적 미들웨어를 `src/tracing.py`로 두고 TASKS Step 5에서 구현한다. 저장 위치는 Step 5 직전에 정한다. → CLAUDE.md 폴더 구조 · SPEC.md 1.2, 1.5 · TASKS.md Step 5
- (2026-11-04) Bedrock 모델은 `us.anthropic.claude-sonnet-4-5-20250929-v1:0`(ref 코드와 동일), 리전 `us-east-1`. `.env`로 교체 가능. → SPEC.md 1.5
- (2026-11-04) 시스템 로그는 `logs/app.jsonl`에 JSON Lines 형식으로 남긴다. → SPEC.md 1.5, 2.1
- (2026-11-04) 평가 파일 형식: `evaluation/test_queries.csv`, 실습 안내 4-4 스키마(7개 컬럼, 4개 카테고리)를 따른다. → EVALUATION.md 1
- (2026-11-04) 상태가 필요한 케이스는 `note`의 `[setup]` 표기로 전제 조건을 적는다. → EVALUATION.md 2
- (2026-11-04) 카테고리 구성(20건)과 판정 원칙(실시간 값은 도구 호출·특성으로, 계산은 정확값으로). → EVALUATION.md 3, 4
- (2026-11-04) 예산 계산의 가격 근거: 사용자 입력 → 웹 검색 → 'LLM 추정치' 표시 허용(예산 큰 항목만, 입장료 제외), 금액은 범위로, 예산 판정은 ±10% 근접 기준. → SERVICE.md 2.3, 2.6 · SCENARIOS.md 3 · EVALUATION.md 4
- (2026-11-04) URL 출처가 없는 LLM 일반지식 답변(수집 실패, 날씨·환율 대체, 예산 추정치)은 '출처 없음, 사용자 확인 필요'로 표시한다. → SERVICE.md 2.1, 2.3, 2.4, 2.6 · SCENARIOS.md 4 · EVALUATION.md 4
- (2026-11-04) 제출 규약의 폴더 구조는 '최소 구성'이다. 추가 파일로 `main.py`(CLI 진입점·초기화 명령)와 `src/config.py`(설정값·.env·로그 설정)를 둔다. → CLAUDE.md 폴더 구조 · SERVICE.md 2.7 · SPEC.md 1.2, 1.3, 1.5, 2.7
- (2026-11-04) 웹페이지 수집은 `src/collector.py`, URL 전처리 미들웨어(URL 추출·관련도 판정·저장 흐름)는 `src/middleware.py`, RAG는 `src/retriever.py`로 분리한다. → CLAUDE.md 폴더 구조 · SPEC.md 1.2, 2.1
- (2026-11-04) 가드레일: 무관 질문은 정중히 거절+여행 유도, 시스템 프롬프트는 비공개+기능 안내, 수집 본문은 데이터로만 취급하고 지시문 의심 시 저장 거절(로그만), 사용자 격리는 코드(user_id 필터)로 보장. → SERVICE.md 2.1, 2.8 · SPEC.md 2.1, 2.8
- (2026-11-04) 정보 부족 요청: 빠진 항목(목적지·기간·인원·출발지·예산)은 모두 한 번에 되묻고, 사용자가 정보를 주지 않으면 가정해서 답하며 '가정한 조건'을 표시한다. 기본값은 인원 1명·인천 출발, 예산은 가정하지 않는다. → SERVICE.md 2.2 · SPEC.md 1.5, 2.2
- (2026-11-04) 웹 검색은 필수 기능이며 provider는 Tavily(`langchain-tavily`, 도입 승인). 검색 실패 시 LLM 일반지식 + '출처 없음, 사용자 확인 필요' 표시. → SERVICE.md 2.5 · SPEC.md 1.6, 2.5 · OPEN_ISSUES #5에서 웹 검색 제외
- (2026-11-04) 환율 provider는 open.er-api.com(키 없음, 약 160개 통화) 단독 사용. 기준 통화는 KRW 기본·요청 시 변경. HTTPS·통화 코드만 전송·응답 검증·캐시·출처 표기를 지킨다. 조회 실패 시 2.4 대체 규칙을 따른다. → SERVICE.md 2.4 · SPEC.md 1.5, 2.4
- (2026-11-04) 예산 계산은 예산 전용 도구(`calculate_budget`)로 한다. LLM은 재료만 구조화해 넘기고 계산·판정은 파이썬. 예산 단위: 2명 이상에서 1인/총액이 불분명하면 되묻고(답 없으면 총액 가정), 숙박은 2인 1실 기본, 결과는 총액과 1인당을 함께 표시. → SERVICE.md 2.2, 2.3 · SPEC.md 1.2, 1.5, 2.3 · EVALUATION.md 4
- (2026-11-04) 벡터DB는 기본 KB 컬렉션과 사용자 수집 컬렉션으로 분리한다. 기본 KB의 user_id는 `"__kb__"`, `"__"`로 시작하는 user_id는 예약값으로 사용 금지, 초기화·삭제는 사용자 컬렉션만 대상. 관련도 점수 필드명은 `relevance_score`. → SERVICE.md 2.7 · SPEC.md 1.3, 1.4, 2.7
- (2026-11-04) 대화 기록은 SQLite 체크포인터(`langgraph-checkpoint-sqlite`, 도입 승인)로 영구 저장. 실행 시 새 thread_id 자동 생성, `--thread`로 이어가기, `/new`로 새 대화. thread는 만든 user_id만 열 수 있다. → SERVICE.md 2.8 · SPEC.md 1.2, 1.3, 1.5, 1.6, 2.8
- (2026-11-04) 매 턴 현재 날짜·요일·시간대(기본 Asia/Seoul)를 `agent.py`가 시스템 프롬프트에 넣는다. LLM은 상대 날짜를 절대 날짜로 바꾸기만 하고, 날짜 계산·예보 범위 판정은 도구 안 파이썬이 한다. → SERVICE.md 2.4 · SPEC.md 1.2, 1.5, 2.4 (상대 날짜 변환 부분은 2026-11-05 결정으로 날짜 계산 도구로 변경)
- (2026-11-04) 벡터DB는 Chroma(`langchain-chroma`), 청킹은 `langchain-text-splitters`, 웹페이지 수집은 `langchain-community` `WebBaseLoader` + `beautifulsoup4`로 정하고 도입 승인. 하이브리드 검색(BM25)은 1차 기획에 넣지 않는다. → SPEC.md 1.6 · requirements.txt
