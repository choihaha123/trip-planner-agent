# 프로젝트 규칙

## 기술 스택
- Python, LangChain, LangGraph
- 모델은 Amazon Bedrock (ChatBedrockConverse)
- Agent 생성은 langchain.agents 의 create_agent 를 쓴다

## 폴더 구조 (제출 규약. 바꾸지 않는다)
아래는 '최소한 있어야 하는' 구성이다. 이름·위치를 바꾸거나 빼지 않는다.
src/agent.py       메인 에이전트 그래프
src/tools.py       도메인 도구
src/retriever.py   RAG 파이프라인
data/              사용한 문서와 데이터
evaluation/        test_queries.csv 와 평가 리포트

허용된 추가 파일 (이 외의 파일은 사용자 승인 후 추가하고 여기에 적는다)
main.py            CLI 진입점
src/config.py      설정값·.env 읽기·로그 설정
src/collector.py   웹페이지 수집·텍스트 추출
src/middleware.py  URL 전처리 미들웨어 (URL 추출·관련도 판정·저장 흐름)
src/tracing.py     실행 추적 미들웨어 (LLM·도구 호출 기록, TASKS Step 5)
src/memory.py      장기 기억 (user_id별 기억 저장소·자동 추출·미들웨어, TASKS Step 13)
src/web/__init__.py         웹 패키지 표시 (TASKS Step 15)
src/web/app.py              FastAPI 앱·API 라우트·진행 상황(SSE)
src/web/auth.py             회원가입·로그인·세션 (사용자 DB storage/web.sqlite)
src/web/threads.py          사용자별 대화 목록 (제목·삭제)
src/web/static/             화면 (index.html, app.js, style.css)
src/preprocess/build_kb.py  기본 KB 적재 스크립트 (data/kb → 벡터DB, TASKS Step 4)
tests/test_budget.py        예산 계산 단위 테스트 (unittest, TASKS Step 6)
tests/test_dates.py         날짜 계산 단위 테스트 (unittest, TASKS Step 7)
tests/test_memory.py        장기 기억 단위 테스트 (unittest, TASKS Step 13)
tests/test_web.py           웹 API 단위 테스트 (unittest + FastAPI TestClient, TASKS Step 15)
tests/test_resilience.py    UX 평가 후속 단위 테스트: 장애 내성·링크 안내·저장 투명성·CLI 안내·저장 자료 관리·응답 속도 (unittest, TASKS Phase 5-2 T1~T6, 응답 속도 개선)
evaluation/run_eval.py      평가 스크립트 (test_queries.csv 실행·판정, TASKS Step 11)
evaluation/fixtures/        평가용 테스트 페이지 (inject.html: 간접 프롬프트 인젝션, 로컬 웹서버로 제공)
evaluation/results/         평가 실행 결과 (케이스별 답변·도구·판정, Step 12 리포트 근거)
requirements.txt   설치 패키지 목록
.env.example       .env 작성 예시 (비밀 값 없음, TASKS Step 1)


## 참조 MD 파일
참조 문서는 모두 `docs/` 폴더에 있다. (CLAUDE.md, README.md만 루트에 둔다)
- docs/SERVICE.md 프로젝트 기획서. 무엇을 만드는지의 기준
  - 모든 작업은 SERVICE.md 내용을 근거로 한다
  - 작업에 필요한데 SERVICE.md에 없거나 모호한 내용은 추측하지 말고 사용자에게 먼저 확인한다
  - 확인된 내용은 SERVICE.md에 반영해 기획서를 최신 상태로 유지한다
- docs/SPEC.md 구현 명세. SERVICE.md의 기능을 어떻게 구현하는지의 기준
  - 1장은 공통 구조(파일 역할, 실행 구조, 데이터 모델, 설정값, 외부 구성요소), 2장은 기능별 구현이며 2장 번호는 SERVICE.md 2장과 같다
  - 코드를 작성할 때는 해당 기능의 SERVICE.md 절과 SPEC.md 절을 함께 본다
  - 기능이 바뀌면 SERVICE.md, 구현 방식이 바뀌면 SPEC.md에 반영한다. 두 문서가 어긋나면 SERVICE.md를 따르고 사용자에게 확인한다
- docs/TASKS.md 단계별 작업 계획서. 프로젝트 진행 순서를 task 단위로 정리한다
  - 요청받은 task 하나만 수행하고, 다음 task로 넘어가지 않는다
  - task를 끝내면 TASKS.md에 완료 표시를 한다
- docs/EVALUATION.md 평가 기준. test_queries.csv 스키마, [setup] 표기, 카테고리 구성, 판정 원칙
  - 평가 데이터·평가 스크립트 작업은 EVALUATION.md를 따른다
- docs/SCENARIOS.md 1차 완료 시나리오. 시연에서 반드시 동작해야 하는 시나리오 목록
  - 기능을 구현할 때 관련 시나리오가 동작하는지를 기준으로 삼는다
- docs/OPEN_ISSUES.md 미결 항목 목록
  - 여기 있는 항목에 해당하는 작업은 추측하지 말고 사용자에게 먼저 확인한다
  - 결정되면 관련 문서(SERVICE.md / SPEC.md / EVALUATION.md / SCENARIOS.md)에 반영하고 OPEN_ISSUES.md에서 '결정됨'으로 옮긴다


## 코드 규칙
- 파일 하나에 한 가지 역할만 둔다
- 함수와 도구에는 한국어 docstring 을 쓴다
- API 키와 같은 비밀 값은 .env 에서 읽고 코드에 적지 않는다
- 임계값·한계 수치·provider 선택처럼 조정 가능한 값은 코드에 하드코딩하지 않고 .env 또는 설정 상수로 둔다


## 작업 규칙
- 한 번에 TASKS.md의 작업 하나만 한다.
- 계산, 규칙 판정은 LLM이 아니라 파이썬 코드로 하는 원칙으로 진행한다. 필요시 LLM 판정부분을 덧붙여 추가한다.
- 괜찮은 레퍼런스 코드를 깃허브에서 찾아서 레퍼런스로 삼거나 (있을 경우) 현재 ref 폴더 내 코드를 참조한다. 절대 사용자의 허락없이 신규 프레임워크 또는 라이브러리를 들이지 않는다.


## 하지 말 것
- 요청하지 않은 파일을 새로 만들지 않는다
- 기존 파일을 통째로 다시 쓰지 않는다. 바뀐 부분만 고친다
- 사용자가 요청하지 않는 한 git add·commit·push·브랜치 생성 등 git/GitHub 작업을 하지 않는다








