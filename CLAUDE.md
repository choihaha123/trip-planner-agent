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
requirements.txt   설치 패키지 목록


## 참조 MD 파일
- SERVICE.md    프로젝트 기획서. 무엇을 만드는지의 기준
  - 모든 작업은 SERVICE.md 내용을 근거로 한다
  - 작업에 필요한데 SERVICE.md에 없거나 모호한 내용은 추측하지 말고 사용자에게 먼저 확인한다
  - 확인된 내용은 SERVICE.md에 반영해 기획서를 최신 상태로 유지한다
- TASKS.md      단계별 작업 계획서. 프로젝트 진행 순서를 task 단위로 정리한다
  - 요청받은 task 하나만 수행하고, 다음 task로 넘어가지 않는다
  - task를 끝내면 TASKS.md에 완료 표시를 한다
- EVALUATION.md 평가 기준. test_queries.csv 스키마, [setup] 표기, 카테고리 구성, 판정 원칙
  - 평가 데이터·평가 스크립트 작업은 EVALUATION.md를 따른다
- SCENARIOS.md  1차 완료 시나리오. 시연에서 반드시 동작해야 하는 시나리오 목록
  - 기능을 구현할 때 관련 시나리오가 동작하는지를 기준으로 삼는다
- OPEN_ISSUES.md 미결 항목 목록
  - 여기 있는 항목에 해당하는 작업은 추측하지 말고 사용자에게 먼저 확인한다
  - 결정되면 관련 문서(SERVICE.md / EVALUATION.md / SCENARIOS.md)에 반영하고 OPEN_ISSUES.md에서 '결정됨'으로 옮긴다


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








