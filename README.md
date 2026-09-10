<p align="center">
  <img src="assets/logo.png" alt="M7 Terminal" width="120"/>
</p>

<h1 align="center">M7 Terminal</h1>

<p align="center">
  토스증권 Open API 기반 포트폴리오 리포트 · 대시보드
</p>

---

토스증권 Open API로 포트폴리오를 읽어 **Notion 일일 리포트**를 만들고, **로컬 대시보드**로 자산 현황을 보여주는 개인용 도구입니다.

국내(KRX)와 미국 주식을 단일 REST API로 조회하며, 보유 수량·평균 매수가·평가손익은 물론 **수수료·세금 차감 후 손익**과 **전일 대비 손익**까지 서버가 계산해 내려줍니다.

```
토스 계좌 ─┐
           ├─→ 통합 포트폴리오 ─┬─→ Notion 리포트 (AI 분석 + 뉴스)
config.yaml ┘  (KRW 환산)       ├─→ Firestore 스냅샷
 (타 증권사)                     └─→ 대시보드 (자산 추이 · 배분 · 경보)
```

---

## 빠른 시작

```bash
# 1. 의존성
pip install -r requirements.txt

# 2. 자격증명 — .env 의 PASTE_... 를 실제 값으로 교체
#    (토스증권 WTS > 설정 > Open API 에서 발급 + 허용 IP 등록)

# 3. 연결 확인 (읽기 전용)
python scripts/smoke_test.py

# 4. 설정 후 리포트 실행
cp config.example.yaml config.yaml   # 보유 종목 · Notion 정보 입력
python main.py

# 5. 대시보드
uvicorn src.dashboard.api:app --host 127.0.0.1 --port 8000
```

Python 3.10 이상 (3.12에서 개발·검증).

---

## 설정

### 1. 토스 Open API 발급

토스증권 WTS → **설정 → Open API**

1. `client_id` / `client_secret` 발급
2. 같은 화면 하단 **허용 IP 관리**에 API를 호출할 공인 IP 등록

> ⚠️ **허용 IP는 필수입니다.** 등록되지 않은 IP의 호출은 `403 edge-blocked`로 전량 차단됩니다. 가정용 회선처럼 IP가 바뀌는 환경에서는 어느 날 갑자기 전체가 실패하므로, 그 경우 재등록이 필요합니다. `smoke_test.py`는 403을 만나면 이 안내를 명시적으로 출력합니다.

### 2. `.env`

자격증명은 `config.yaml`이 아니라 `.env`에 둡니다 (gitignore 대상). 리포지토리에 이미 `.env`가 플레이스홀더로 생성되어 있으니 값만 교체하면 됩니다.

```
TOSS_CLIENT_ID=tsck_live_xxxxxxxxxxxx
TOSS_CLIENT_SECRET=tssk_live_xxxxxxxxxxxx
GOOGLE_AI_API_KEY=xxxxxxxxxxxx        # 선택. 없으면 AI 분석만 생략
                                      # 발급: https://aistudio.google.com/apikey
NOTION_TOKEN=ntn_xxxxxxxxxxxx         # 일일 리포트용
NOTION_DATABASE_ID=2f1a8b3c4d5e...    # DB URL 의 ?v= 앞 32자
GOOGLE_APPLICATION_CREDENTIALS=/path/to/firebase-service-account.json  # Firestore 저장소
```

교체를 잊으면 앱이 *"`.env`의 TOSS_CLIENT_ID가 아직 플레이스홀더입니다"* 라고 알려줍니다.

### Firebase (Firestore) 설정

스냅샷·시그널·주문 등 영속 데이터는 Firestore에 저장됩니다. **일봉 캐시만 예외**로 로컬 SQLite(`bars.db`)에 둡니다 — 16년치 OHLC는 불변 참조 데이터고 다른 기기가 읽지 않는데, 유니버스 한 번 적재에 5만 건 쓰기라 Firestore 무료 티어 하루 할당량을 통째로 먹습니다. 경로는 `M7_BAR_CACHE_PATH`로 바꿀 수 있습니다.

1. https://console.firebase.google.com 에서 프로젝트 생성 (또는 기존 GCP 프로젝트 사용)
2. **Firestore Database** 활성화 (Native mode)
3. 프로젝트 설정(톱니바퀴) → **서비스 계정** → **새 비공개 키 생성** → JSON 다운로드
4. 다운로드한 JSON을 `secrets/` 아래(gitignore 대상)에 두고, 그 절대 경로를 위 `.env`의
   `GOOGLE_APPLICATION_CREDENTIALS`에 지정

이 파일은 은행 비밀번호와 동급으로 취급하세요 — 유출되면 Firebase 콘솔에서 해당 서비스
계정 키를 즉시 삭제하고 새로 발급하면 됩니다.

### Notion 토큰 받기

Claude 등의 **MCP 커넥터는 쓸 수 없습니다.** OAuth 방식이라 재사용 가능한
토큰을 내주지 않기 때문에, Internal Integration 을 따로 만들어야 합니다.

1. https://www.notion.so/profile/integrations → **New integration** (Type: Internal)
   → **Internal Integration Secret** 복사 (`ntn_` 또는 예전 계정은 `secret_` 로 시작)
2. ⚠️ **리포트를 넣을 데이터베이스 페이지에서 `⋯` → Connections → 해당 integration 연결.**
   이 단계를 빼먹으면 토큰이 맞아도 API 가 404 를 반환합니다.
3. URL 의 32자 hex 문자열이 ID 입니다.
   ```
   https://app.notion.com/p/워크스페이스/제목-1a30084c658a4e78944080096889dbfb?pvs=28
                                            └──────────── 이 부분 ────────────┘
   ```

**데이터베이스와 일반 페이지 둘 다 지원합니다.**

| 대상 | 리포트 생성 방식 | 요구사항 |
|---|---|---|
| 데이터베이스 (URL 에 `?v=` 있음) | 새 행으로 추가 | `Report`(제목) · `Date`(날짜) 속성 필요 |
| 일반 페이지 (`/p/` 링크) | 하위 페이지로 추가 | 없음 |

`python scripts/smoke_test.py` 가 토큰·연결·대상 종류를 한 번에 점검해 줍니다.

> **왜 파일이 아니라 환경변수인가**: Phase 2부터 이 키는 조회가 아니라 **주문 실행 권한**을 가집니다. 채팅·이슈·스크린샷에 노출하지 마세요. 노출됐다면 WTS에서 즉시 재발급하면 됩니다 — 재발급은 유출된 토큰도 그 즉시 무효화합니다.

### 3. `config.yaml`

```bash
cp config.example.yaml config.yaml
```

**토스 계좌 밖 보유분**과 Notion 정보만 채우면 됩니다.

```yaml
portfolio:
  manual:
    - symbol: "000660"        # 6자리 코드 (.KS/.KQ 접미사는 자동 제거)
      qty: 10
      avg_price: 180000

    - symbol: "AAPL"
      qty: 5
      avg_price: 180.0
      avg_exchange_rate: 1300.0   # 선택 — 아래 설명 참고

    # 토스가 취급하지 않는 자산 (예금·금 등)
    # - name: "금 현물"
    #   qty: 1
    #   avg_price: 5000000
    #   price: 5400000
    #   currency: "KRW"
```

기존 v1 설정(`portfolio.stocks`, `005930.KS` 표기)도 그대로 읽힙니다 — 자동 변환되며 경고만 출력됩니다.

---

## 수기 입력은 어디까지 남는가

**토스 계좌 보유분은 완전 자동**입니다. 타 증권사 보유분만 `config.yaml`에 남고, 그것도 **`(수량, 평단)` 두 값**뿐입니다 — 종목명·현재가·통화는 토스 시세 API가 채웁니다.

예외가 하나 있습니다: **매수 시점 환율**. 토스도 이 값은 제공하지 않습니다(`averagePurchasePrice`는 원화폐 기준). 해외 종목의 환차손익까지 수익률에 반영하려면 `avg_exchange_rate`를 직접 입력해야 합니다.

|  | 매입액 환산 | 수익률에 포함되는 것 |
|---|---|---|
| `avg_exchange_rate` 입력 | 매수 시점 환율 | 주가 손익 **+ 환차손익** |
| 미입력 | 현재 환율 | 주가 손익만 |

미입력 종목이 있으면 리포트와 대시보드에 **"환차손익 미반영"** 배지가 붙습니다. 수치를 실제보다 넓게 해석하지 않도록 하기 위한 표시입니다.

---

## 사용법

### 연결 점검

```bash
python scripts/smoke_test.py
```

`/accounts` → `/holdings` → `/prices` → `/exchange-rate` → `/market-calendar` → Notion → Gemini 를 순서대로 점검합니다. 토스 호출은 GET만 사용하므로 주문이 나갈 수 없습니다.

Gemini 단계는 토큰 한 개짜리 질문을 실제로 던져 봅니다. 키가 틀렸거나 모델명이 폐기되면 리포트 안에 사과 한 줄로만 남아 놓치기 쉬운데, 여기서는 소리 내어 실패합니다.

**연속 2회 실행**해 보세요. 2회차에 토큰이 재발급되지 않아야 정상입니다.

> 토스는 **client당 유효 토큰이 1개**이고 재발급 시 이전 토큰을 즉시 무효화합니다. 그래서 토큰 캐시(`.toss_token.json`)는 성능 최적화가 아니라 정합성 요구사항이며, 파일 락으로 보호됩니다. 같은 자격증명으로 두 프로세스를 동시에 돌리면 서로를 로그아웃시킵니다.

### 일일 리포트

```bash
python main.py          # 또는 run_report.bat
```

Notion 페이지를 만들고 Firestore에 스냅샷 1건을 적재합니다. 순서상 **스냅샷 저장이 Notion·Gemini 호출보다 먼저**라, 외부 서비스가 죽어도 자산 이력은 남습니다.

실패 시 **0이 아닌 exit code**를 반환합니다 (`2` = 토스/설정 오류, `3` = 그 외). 스케줄러에서 실패를 감지할 수 있습니다.

### 자동 실행 (Windows 작업 스케줄러)

```powershell
schtasks /Query /TN "M7 Terminal Daily Report" /V /FO LIST   # 상태 확인
schtasks /Run   /TN "M7 Terminal Daily Report"               # 지금 한 번 실행
schtasks /Delete /TN "M7 Terminal Daily Report" /F           # 등록 해제
```

> 이미 `StockLens-AI Daily Report`라는 이름으로 등록해 두셨다면 위 명령은 그 작업을 찾지 못합니다.
> 기존 작업을 지우고 새 이름으로 다시 등록하세요:
> ```powershell
> schtasks /Delete /TN "StockLens-AI Daily Report" /F
> schtasks /Create /TN "M7 Terminal Daily Report" /TR "<run_report.bat 전체 경로> --scheduled" /SC DAILY /ST 10:00
> ```

매일 **10:00**에 `run_report.bat --scheduled`를 실행합니다. 미국장 마감(서머타임 기준 06:00 KST) 이후이고, **PC를 쓰기 시작하는 09:00보다 뒤**입니다 — 이 작업은 `LogonType=Interactive`라 로그온 상태에서만 돌기 때문에, 사용 시간대 안에 트리거가 들어와야 그날 바로 실행됩니다.

| 설정 | 이유 |
|---|---|
| `--scheduled` 인자 | 이 인자가 없으면 `run_report.bat`은 `pause`로 멈춘다. 스케줄러에서는 작업이 영원히 끝나지 않는다 |
| StartWhenAvailable | PC가 꺼져 있어 놓친 실행을 다음 부팅 때 따라잡는다. 스냅샷은 소급 생성이 불가능하므로 하루를 통째로 잃지 않는 편이 낫다 |
| MultipleInstances=IgnoreNew | 토스는 client당 유효 토큰이 1개다. 겹쳐 돌면 서로를 로그아웃시킨다 |
| ExecutionTimeLimit 30분 | 외부 API가 응답하지 않을 때 무한정 매달리지 않는다 |

로그는 `logs/report_YYYY-MM-DD.log`에 날짜별로 쌓입니다(gitignore 대상). 스케줄러가 기록하는 **Last Run Result**가 그대로 `main.py`의 exit code입니다.

> 콘솔로 리다이렉트된 출력은 cp949를 따라 한글이 깨지므로, 배치 파일이 `PYTHONIOENCODING=utf-8`을 지정합니다.

### 매매 엔진 (Phase 2 — PAPER)

```bash
python trade.py               # PAPER — 시장을 읽고, 아무것도 전송하지 않음
python trade.py --dry-run     # 리스크 게이트까지만. DB에 기록 없음
python trade.py --reconcile   # 미체결 LIVE 주문의 체결을 확인하고 OCO를 등록
python trade.py --live        # 현재 거부됨 (아래 참고)
```

`config.yaml`의 `trading.enabled`를 켜고 `trading.strategies`에 직접 작성한 전략을 등록해야 동작합니다. 전략은 `Strategy`를 상속하고 `evaluate(ctx) -> list[Signal]`만 구현하며, **그 안에서 I/O를 하면 안 됩니다.**

한 번 실행하면:

1. 시세·잔고·매도가능수량·상하한가·장 운영시간을 읽어 컨텍스트를 만들고
2. 전략이 신호를 내고
3. 리스크 게이트가 신호마다 통과/거부를 판정해 **승인·거부 전부** `signals`/`rejections`에 기록하고
4. 승인된 것만 `orders`에 `simulated`로 남습니다 (HTTP 전송 없음)

**`--reconcile`은 별도 경로입니다.** 전략도 리스크 게이트도 거치지 않고, 미체결 LIVE 주문(`submitted`/`unknown`/`partially_filled`)을 `GET /orders`로 조회해 체결을 `fills`에 기록하고, 매수 신호가 `stop_loss_price`/`take_profit_price`를 실었다면 체결 수량만큼 OCO 손절을 등록합니다. 전략 평가는 하루 한 번이면 되지만 체결 확인은 더 자주 돌려야 하므로 갈라뒀습니다 — 스케줄러에 별도 주기로 등록하세요.

> **`--live`는 아직 열려 있지 않습니다.** reconciler와 OCO는 이제 있지만, 설계 [10]이 요구하는 "최소 수량 1주로 실거래 검증"은 별도 단계입니다.

**그 검증은 `scripts/live_check.py`로 합니다.** `trade.py`는 전략이 낸 신호를 통째로 처리하는 것밖에 못 하고, bucket-dca는 한 번에 8건 수백 달러어치를 냅니다 — 첫 실거래로 삼을 수 없습니다.

```bash
python scripts/live_check.py --symbol SHY              # 전송 없음. 게이트 판정과 보낼 내용만
python scripts/live_check.py --symbol SHY --execute    # 실제로 1주 매수
python scripts/live_check.py --symbol SHY --amount 5 --execute   # 금액 주문 경로
python scripts/live_check.py --settle                  # 미체결 LIVE 주문 체결 확인
```

우회로가 아닙니다 — 같은 `build_context`, 같은 `RiskGate`, 같은 `OrderExecutor`를 씁니다. API를 직접 부르는 스크립트였다면 토스가 동작한다는 것만 증명하고 이 프로젝트가 실제로 작성한 부분은 전부 미검증으로 남습니다.

**금액 주문으로 한 번 더 돌릴 값어치가 있습니다.** bucket-dca 매수는 전부 금액 주문이고, reconciler는 그걸 수량 주문과 **다른 분기**에서 정산합니다 — 2026-09-10까지 `filled`에 도달할 수 없었던 그 분기입니다. 1주는 경로를 증명하고, 소액 금액 주문은 실제 주문이 탈 분기를 증명합니다.

**여기가 LIVE가 열리는 유일한 지점입니다.** `trade.py --live`는 여전히 거부되고, 예약된 Cloud Run 잡은 그 플래그를 넘기지 않습니다(`args: python trade.py`). 그래서 이 스크립트는 `--execute` 없이는 아무것도 보내지 않고, 보낼 때는 **종목코드를 그대로 타이핑**해야 하며(`y` 로는 안 됩니다), 대화형 터미널이 아니면 `--yes` 없이는 거부합니다. 어떤 잡의 인자에도 들어 있지 않습니다.

**OCO는 진입 체결 이후에만 등록됩니다.** executor가 주문을 낼 때가 아니라 — 아직 체결되지 않은 주문에 손절을 거는 건 의미가 없고, 부분체결이면 그 수량만큼만 걸어야 합니다. 그래서 신호의 `stop_loss_price`/`take_profit_price`는 주문에 실려 저장되고, `--reconcile`이 체결을 확인한 뒤에야 조건주문을 보냅니다. 브라켓 만료일(`trading.oco_expire_days`)과 손절 주문가의 슬리피지(`trading.oco_stop_loss_slippage`)는 `config.example.yaml`을 참고하세요.

> **알려진 한계**: 한국 주식의 실제 호가단위(가격대별로 1원~1,000원까지 계단식)는 적용하지 않습니다 — 확인된 근거가 없어 추측으로 규칙을 넣느니 `invalid-tick-size` 거부로 드러나게 뒀습니다. `GET /orders` 응답의 정확한 필드명(체결수량·평균단가 등)도 미확정이라 여러 철자를 방어적으로 시도합니다 — 시세·캘린더 파서와 같은 방식입니다.

**긴급 정지**: 프로젝트 루트에 `KILL_SWITCH` 파일을 만들면 모든 발주가 즉시 중단됩니다. 코드 수정도 재시작도 필요 없습니다.

```bash
touch KILL_SWITCH     # 정지
rm KILL_SWITCH        # 해제
```

대시보드 우상단 **PAUSE TRADING** 버튼과 Trading 화면이 같은 파일을 만들고 지웁니다. 화면에서 발동하면 사유가 파일 안(`reason:`)과 감사 로그에 함께 남고, 손으로 만든 빈 파일도 똑같이 유효한 정지입니다 — 그때는 발동 시각을 파일 수정시각에서 복원하고 화면에 그렇게 표시합니다. 해제는 확인 창을 거치지만 발동은 한 번의 클릭으로 끝납니다: 멈추는 방향은 언제나 안전한 쪽입니다.

### 대시보드

```bash
uvicorn src.dashboard.api:app --host 127.0.0.1 --port 8000
```

사이드바에 **Last Sync**(이 화면이 마지막으로 토스 API와 통신한 시각)와 **Snapshot**(일일 작업이 마지막으로 스냅샷을 적재한 시각)이 따로 표시됩니다. 둘은 다른 시계이고, 조용해질 수 있는 쪽은 후자뿐입니다 — 스냅샷이 26시간을 넘기면 Overview 상단에 경보가 뜹니다.

| 화면 | 내용 |
|---|---|
| **Overview** | 총자산 · 총손익(수수료·세금 차감) · 오늘 손익 · 매수가능금액, 자산 추이 차트, 배분 도넛, AI 코멘트, 매수 유의사항 경보 |
| **Holdings** | 종목별 수량·평단·현재가·손익·비중. **토스/수기 출처 배지** |
| **Reports** | 생성된 Notion 리포트 이력 |
| **Settings** | 설정 읽기전용 (자격증명 마스킹) |
| **Trading** | Phase 3 예정 — 현재 비활성 |

> ⚠️ **`127.0.0.1` 바인딩을 유지하세요.** 인증이 없고, Phase 2 이후에는 실계좌 제어 화면이 됩니다.

**자산 추이 차트는 과거를 소급 생성할 수 없습니다.** `main.py`를 실행할 때마다 한 점씩 쌓이므로 일찍 시작할수록 볼 것이 많아집니다. 스냅샷이 3개 미만이면 차트 대신 "이력 수집 중" 안내가 표시됩니다.

---

## 프로젝트 구조

```text
M7-Terminal/
├── main.py                  # 일일 리포트 실행
├── trade.py                 # 매매 엔진 (PAPER 기본)
├── .env                     # 자격증명 (gitignore)
├── config.example.yaml      # 설정 템플릿
├── scripts/smoke_test.py    # 읽기전용 연결 점검
├── docs/ui/                 # 대시보드 디자인 원본 (DESIGN.md, mockup.html)
├── tests/                   # 312개 — 네트워크·자격증명 불필요
└── src/
    ├── config.py            # .env 우선 자격증명, 심볼 정규화, v1 하위호환
    ├── toss/
    │   ├── client.py        # 토큰 캐시·레이트리밋·백오프·쓰기 가드
    │   ├── account.py       # 계좌 · 보유주식 · 매수가능금액
    │   ├── market.py        # 시세 · 종목마스터 · 환율 · 캘린더 · 유의사항
    │   ├── ratelimit.py     # 그룹별 토큰버킷
    │   └── errors.py        # 에러코드 → 예외 타입
    ├── sources/             # toss_source(자동) · manual_source(config)
    ├── portfolio.py         # 소스 병합 → KRW 환산 스냅샷
    ├── models.py            # Position · PortfolioSnapshot (전 구간 Decimal)
    ├── strategy/            # base.py(Signal · Strategy) · loader.py
    ├── execution/           # risk.py(리스크 게이트) · executor.py · reconciler.py(체결·OCO) · context.py · ids.py
    ├── store/               # Firestore (스냅샷 · 포지션 · 리포트 · 신호 · 주문)
    ├── data/                # 일봉 캐시 (로컬 SQLite) + yfinance 소스
    ├── dashboard/           # FastAPI + 정적 프론트엔드
    ├── news.py              # Google News RSS
    ├── analyst.py           # Gemini 분석
    ├── universe_review.py   # AI 거부권(신규매수 보류) · 편입 후보 제안
    ├── audit.py             # 설정 변경 감지 → 감사 로그
    └── notion.py            # Notion 리포트 생성
```

### 자동 실행은 Windows 파이썬으로 돕니다

일일 리포트는 Windows 작업 스케줄러(`M7 Terminal Daily Report`)가
`run_report.bat --scheduled`를 부르고, 그 배치는 **Windows 파이썬**을 씁니다.
WSL에서 개발한다면 파이썬 환경이 둘이라는 뜻입니다 — `requirements.txt`가
바뀌면 **양쪽 다** 설치해야 합니다:

```
# WSL
pip install -r requirements.txt
# Windows (PowerShell)
python -m pip install -r requirements.txt
```

의존성이 한쪽에만 깔린 상태는 조용합니다. 손으로 돌릴 땐 잘 되고 스케줄러만
매일 실패하며, 그 실패는 `logs/report_YYYY-MM-DD.log` 안에서만 보입니다.

`.env`의 `GOOGLE_APPLICATION_CREDENTIALS`는 어느 쪽 표기로 적어도 됩니다.
`load_config()`가 WSL(`/mnt/c/...`)과 Windows(`C:\...`) 표기를 서로 바꿔 가며
찾고, 그래도 없으면 저장소의 `secrets/firebase-service-account.json`을 씁니다.

**스케줄러가 도는지 확인하는 법** (PowerShell):

```powershell
Get-ScheduledTaskInfo -TaskName 'M7 Terminal Daily Report' |
  Format-List LastRunTime, LastTaskResult   # 0이면 정상
```

`LastTaskResult`가 0이 아니면 `logs\report_<날짜>.log`의 마지막 `----- exit N -----`
바로 위를 보세요. 종료 코드는 2=토스 API 오류, 3=그 외 예외입니다.

### 감사 로그 — 누가 언제 뭘 바꿨나

유니버스·전략 목록·전략 파라미터·리스크 한도가 바뀌면 다음 실행 때
**자동으로 감지되어** Firestore `audit_log`에 남습니다. 대시보드
**Audit Log** 탭에서 봅니다. 아무도 기록을 남길 의무가 없습니다 — 매 실행이
설정 지문(fingerprint)을 떠서 직전 지문과 비교하는 방식이라, `config.yaml`을
손으로 고치기만 하면 다음 `python main.py`나 `python trade.py`가 알아서 잡습니다.

항목마다 **시각이 둘**입니다. 서로 다른 사실이고, 확실한 쪽은 하나뿐이기 때문입니다.

- **`changed_at` (변경 시각)** — 설정이 담긴 파일에서 되찾습니다. 페이지에 크게
  표시되고, 없을 수도 있습니다.
- **`detected_at` (감지 시각)** — 시스템이 처음 본 시각. 항상 알 수 있고 틀릴 일이
  없지만, 실제 편집보다 한참 뒤일 수 있습니다. 목록 정렬은 이 값을 씁니다.

`changed_at`을 어떻게 알아냈는지가 `changed_by_method`에 남고, 읽는 사람이
신뢰도를 가늠할 수 있게 화면에도 적습니다:

| 근거 | 언제 | 정확도 |
|---|---|---|
| `git 커밋` | 설정이 추적 중이고 수정되지 않은 소스 파일에 있을 때 (예: 빈 `trading.universe` → `DEFAULT_UNIVERSE`) | **정확**. 커밋 시각과 **실제 작성자**까지 나옵니다 |
| `파일 수정시각` | `config.yaml`처럼 git이 모르는 파일 (시크릿 때문에 일부러 제외) | 근사치. **그 파일의 마지막 저장**이라, 유니버스를 토요일에 고치고 Notion 토큰을 일요일에 고쳤으면 일요일이 찍힙니다 |
| `실행 시점` | AI 보류·제안 | 방금 만들어진 것이라 변경과 감지에 간격이 없습니다 |
| (불명) | 파일을 읽을 수 없을 때 | **감지 시각으로 대체하지 않고 "변경 시각 불명"이라고 표시합니다** |

작업 트리가 더러우면(커밋 안 된 로컬 수정) git 경로를 쓰지 않고 mtime으로
내려갑니다 — 더러운 파일의 마지막 커밋 시각은 변경 시각이 아니고, 그걸 그대로
적는 것이 이 로그가 저질러선 안 되는 유일한 종류의 오류입니다.

**사람 actor는 추정이고 AI actor는 사실입니다.** git 경로에서는 커밋 작성자라
사실이지만, 그 외에는 변경을 처음 발견한 프로세스의 OS 사용자(`user@host`)입니다
— 1인 머신에서는 대개 맞지만 **보안적 주장이 아닙니다.** 이 대시보드에는 인증이
없습니다.

기록 대상은 "엔진이 무엇을 거래할 수 있는가"를 바꾸는 설정으로 한정했습니다
(Notion 토큰·뉴스 키워드·표시 이름은 제외). 전부 기록하는 감사 로그는 읽히지
않고 넘겨집니다.

### AI의 권한 경계

AI는 이 시스템에서 **매수 결정을 내리지 않습니다.** 종목을 고르는 건
백테스트할 수 있는 순위 규칙(`momentum_dca`)의 몫이고, AI에게는 그 규칙이
다룰 수 없는 두 가지만 맡깁니다 — 그리고 두 권한의 무게가 다릅니다.

| | 거부권 (veto) | 후보 제안 (candidate) |
|---|---|---|
| 하는 일 | 유니버스 종목의 **신규 매수 보류** | 유니버스 **밖** 종목 추천 |
| 자동 적용 | ✅ 리스크 게이트가 `symbol-vetoed`로 거부 | ❌ 리포트에만 표시 |
| 매도 | **절대 불가** | - |
| 허용 사유 | 상장폐지·거래정지·피인수·티커변경·회계조사·파산 **뿐** | 자유 |
| 근거 | 오늘 헤드라인 인용 필수 (없으면 폐기) | - |
| 상한 | 1회 `max_vetoes`건 (기본 3) | `max_candidates`건 (기본 5) |
| 수명 | `veto_ttl_days`일 후 자동 해제 (기본 7) | - |

**비대칭이 설계의 핵심입니다.** 틀린 보류는 한 종목의 며칠치 기회를 놓치는
것으로 끝나지만, 틀린 매수는 돈을 잃고, AI가 조용히 유니버스를 고쳐 쓰면
어떤 백테스트도 설명하지 못하는 전략이 됩니다. 그래서 **뺄 수만 있는 권한은
자동, 더하는 권한은 자문**입니다. 편입은 사람이 `config.yaml`을 고치는
일로 남습니다.

전략(`evaluate`)은 이 목록을 **볼 수 없습니다.** 보류는 전략이 판단을 마친
뒤 게이트가 적용하는 백스톱이고, 전략이 그걸 읽기 시작하면 순수성이 깨져
백테스트와 실거래가 갈라집니다.

### 설계상 주의점

- **금액은 전 구간 `Decimal`** — 토스 API가 모든 금액을 문자열로 주고, Firestore에도 문자열로 저장합니다. `float`/`REAL`은 원 단위 오차가 누적됩니다.
- **주문 엔드포인트는 구조적으로 차단** — `TossClient`는 `allow_write=False`가 기본이며, GET 외의 메서드는 `TossWriteBlockedError`를 던집니다. `src/toss/trading.py`만 `allow_write=True` 클라이언트를 생성하고, **PAPER 모드에서는 그것조차 하지 않습니다** — 읽기 전용 클라이언트를 쥐므로 버그로 `place_order`가 호출돼도 POST가 물리적으로 나갈 수 없습니다.
- **전략은 네트워크에 닿을 수 없다** — `Strategy`는 client·store·config를 받지 않습니다. 실수로 주문을 낼 수 없고, 테스트와 운영에서 다르게 동작할 수도 없습니다. 같은 `evaluate`를 과거 컨텍스트에 흘리면 그게 백테스트입니다.
- **리스크 게이트는 I/O를 하지 않는다** — 잔고·장운영시간·오늘 사용량은 `src/execution/context.py`가 읽어서 컨텍스트에 실어줍니다. 모든 규칙이 인자의 함수라 브로커를 목킹하지 않고 테스트합니다.
- **조회 실패는 통과가 아니라 거부** — 컨텍스트 빌더는 조회에 실패한 필드를 비워두고, `strict` 모드가 그것을 "확인 불가"로 읽어 거부합니다. 읽기 실패가 허용 범위를 넓히는 일은 없습니다.
- **대시보드는 서버측 캐시 필수** — `ACCOUNT` 그룹이 **1 TPS**라 브라우저 탭마다 API를 호출하면 즉시 429입니다. 모든 탭이 서버가 소유한 캐시 하나를 공유합니다.

---

## 테스트

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

83개 전부 네트워크·자격증명 없이 실행됩니다 (HTTP 모킹). 커버 범위: 토큰 캐시 재사용, 401 재발급 1회 제한, 429 `Retry-After` 준수, 쓰기 가드, KRW 환산·환차손익, 중복 심볼 병합, `Decimal` 정밀도, 대시보드 API·캐시.

---

## 진행 상태

| Phase | 내용 | 상태 |
|---|---|---|
| **1. 리포트 + 대시보드** | 토스 API 조회 → Notion → Firestore → 대시보드 | ✅ 완료 |
| **2. 자동매매** | 전략 엔진 · 리스크 게이트 · 조건주문(OCO) 손절 | 설계 완료 |
| **3. 대시보드 확장** | 전략 성과 · 주문 이력 · 킬 스위치 | Phase 2 이후 |

### 아키텍처 변천

| 항목 | 키움 OpenAPI (v0) | yfinance 하이브리드 (v1) | **토스 Open API (v2)** |
|---|---|---|---|
| 국내+해외 통합 | ❌ 모듈 분리 | ✅ | ✅ 단일 REST API |
| 실행 환경 | 32bit Python + OCX | 64bit | 64bit, HTTP만 |
| 보유 수량·평단 | 국내만 자동 | ❌ 전량 수기 | ✅ 토스 계좌분 자동 |
| 수수료·세금 반영 손익 | ❌ | ❌ | ✅ |
| 전일 대비 손익 | ❌ | ❌ | ✅ |
| 자산 추이 이력 | ❌ | ❌ | ✅ Firestore |

---

## 참고 문서

- **[docs/STRATEGY.md](docs/STRATEGY.md)** — 지금 돌고 있는 전략이 무엇을 하는지.
  유니버스, 매주 일어나는 일, 안전장치, 성과와 그 숫자를 믿을 때의 단서까지 한
  문서에 있습니다. 나머지 세 문서는 이 결론에 이르는 과정의 기록입니다.

| 문서 | 내용 |
|---|---|
| [DESIGN_Toss_API_Migration.md](./DESIGN_Toss_API_Migration.md) | Phase 1 설계 — 토스 API 조사 결과와 전환 근거 |
| [DESIGN_Trading_and_Dashboard.md](./DESIGN_Trading_and_Dashboard.md) | Phase 2·3 설계 — 자동매매 안전장치 10종, 대시보드 |
| [ISSUE_Kiwoom_Global_Limit.md](./ISSUE_Kiwoom_Global_Limit.md) | 키움 API 폐기 배경 |
| [FEATURE_AI_Analyst.md](./FEATURE_AI_Analyst.md) | AI 애널리스트 |

토스 Open API 공식 문서: https://developers.tossinvest.com/docs
