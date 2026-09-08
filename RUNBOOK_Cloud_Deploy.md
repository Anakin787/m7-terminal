# 런북: 이미지 빌드 → Cloud Run 배포

PC를 켜두지 않아도 배치가 돌게 하는 절차. **데이터(Firestore)는 이미 GCP에 있고,
이 문서는 남은 절반인 "컴퓨트와 트리거"를 옮기는 과정**이다.

> ✅ **여기 적힌 절차는 실제로 돌려서 확인했다** (최종 2026-09-08). `m7-daily`,
> `m7-trade`, `m7-dashboard` 셋 다 이 문서대로 빌드·배포된 상태다. 어긋난 부분을
> 발견하면 고쳐 둘 것 — 이 문서가 틀리면 급할 때 틀린다.

---

## TL;DR — 두 번째부터는 이것만

**빌드는 자동, 배포는 손으로.** `main`에 push하면 Cloud Build가 이미지를 커밋 해시로
태그해 굽고 **거기서 멈춘다**(`cloudbuild.yaml`). 배포는 그 이미지를 가리키게 하는
별도의 명령이다.

📁 **로컬 WSL · 리포 루트**

```bash
git push                                    # → Cloud Build가 :<커밋해시> 이미지를 굽는다
gcloud builds list --region=global --limit=1   # SUCCESS 확인 (보통 1~2분)
```

☁️ **아무 셸이나** — 배포할 대상만 골라서 친다

```bash
export TAG=$(git rev-parse --short HEAD)    # 📁 리포 루트에서만. 아니면 해시를 직접 적는다

gcloud run jobs update     m7-daily     --image=${IMAGE}:${TAG} --region=$REGION
gcloud run jobs update     m7-trade     --image=${IMAGE}:${TAG} --region=$REGION
gcloud run services update m7-dashboard --image=${IMAGE}:${TAG} --region=$REGION
```

**왜 한 번에 안 하나.** 이 저장소는 실주문을 낸다. 나쁜 커밋이 스스로 다음 예약
실행까지 걸어 들어갈 수 있으면, 알아채고 멈출 시간이 `git push`와 그 실행 사이밖에
없다. 미리 구워두는 것이 이 수동 단계를 싸게 만든다 — 배포가 빌드를 기다리는 일이
아니라 **포인터 한 번 바꾸는 몇 초**가 된다.

`update`는 인자·시크릿 볼륨·VPC 설정을 그대로 두고 이미지만 바꾼다. 그래서 이 한 줄에
이미지 말고는 아무것도 적지 않아도 된다.

> **설정만 바꿨다면 빌드도 배포도 필요 없다.** `config.yaml`은 시크릿이고 마운트가
> `:latest`라 Cloud Run이 실행할 때마다 다시 읽는다 — `gcloud secrets versions add
> m7-config --data-file=config.yaml` 한 줄이 배포 전부다(0-6 참고).

**0장(최초 1회 셋팅)은 처음 한 번만** 하면 된다.

---

## 이 문서를 읽는 법 — 명령을 "어디서" 치는가

모든 코드 블록 위에 실행 위치가 표시돼 있다. 세 가지뿐이다.

| 표시 | 어디서 | 왜 거기여야 하나 |
|---|---|---|
| 📁 **로컬 WSL · 리포 루트** | `cd /mnt/c/Users/seonb/orca/m7-terminal` 한 상태의 WSL 터미널 | **리포 파일이 필요한 명령.** `--source .`가 현재 폴더를 업로드하고, `config.yaml`·`bars.db`를 읽어 올린다. 다른 데서 치면 파일을 못 찾는다 |
| ☁️ **아무 셸이나** | 로컬 WSL이든, [GCP 콘솔](https://console.cloud.google.com)의 **Cloud Shell**(우상단 터미널 아이콘)이든 상관없음 | **GCP를 조작만 하는 명령.** 리포 파일을 안 쓴다. Cloud Shell은 gcloud가 이미 깔려 있고 로그인도 돼 있어서, 새 PC나 노트북에서 급할 때 편하다 |
| 🐳 **컨테이너 안** | `docker run`으로 띄운 컨테이너 내부 | 이미지에 뭐가 들어갔는지 확인할 때만 |

가장 흔한 실수는 **📁 표시가 붙은 명령을 리포 루트 밖에서 치는 것**이다.
`--source .`는 "지금 폴더를 통째로 올려라"는 뜻이라, 엉뚱한 폴더에서 치면 엉뚱한 걸 배포한다.
`0-6 시크릿 등록`도 `config.yaml`을 읽으므로 반드시 리포 루트여야 한다.

---

## 공통 변수

📁 **로컬 WSL · 리포 루트** (또는 ☁️ Cloud Shell — 어느 쪽이든 **그 셸을 새로 열 때마다** 다시 붙여넣어야 한다)

```bash
export PROJECT=quant-81f19          # .firebaserc의 default 프로젝트
export REGION=asia-northeast3       # 서울. Firestore 리전과 맞출 것 (0-1 참고)
export REPO=m7-terminal             # Artifact Registry 저장소 이름
export SA_EMAIL=m7-run-sa@${PROJECT}.iam.gserviceaccount.com        # Job/Service 실행용
export SCHED_SA=m7-scheduler-sa@${PROJECT}.iam.gserviceaccount.com  # Scheduler가 Job을 호출
export BUILD_SA=m7-build-sa@${PROJECT}.iam.gserviceaccount.com      # Cloud Build 전용
export BUCKET=gs://${PROJECT}-m7-bars-cache
export IMAGE=${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/m7-terminal
```

> 서비스계정이 셋인 것은 권한을 나누기 위해서다. 빌드는 이미지를 올리고 로그를 쓸 수만 있으면
> 되고, 실행은 Firestore·GCS·Secret에 닿아야 하며, 스케줄러는 Job을 부르는 것 외에 아무것도
> 할 필요가 없다. 하나로 합치면 편하지만, 그 하나가 새면 전부 새는 계정이 된다.

> `export`는 그 터미널 창에만 유효하다. 창을 닫았다 열면 값이 사라지므로,
> 아래 명령에서 `$PROJECT`가 빈 값으로 들어가 이상하게 실패하면 이걸 다시 붙여넣었는지부터 확인한다.

---

## 0. 최초 1회 셋팅

### 0-1. gcloud 로그인과 프로젝트 지정

☁️ **아무 셸이나** — 단 `gcloud auth login`은 브라우저가 열려야 하므로 **로컬에서 하는 게 편하다**
(Cloud Shell은 이미 로그인 상태라 이 줄이 아예 필요 없다).

```bash
gcloud auth login
gcloud config set project $PROJECT
gcloud firestore databases list        # 기존 Firestore의 리전 확인 → REGION을 여기 맞춘다
```

Firestore와 Cloud Run 리전이 다르면 매 호출마다 왕복 지연이 붙는다. 굳이 다르게 둘 이유가 없다.

> WSL에서 브라우저가 안 열리면 `gcloud auth login --no-launch-browser`로 URL을 복사해 쓴다.

### 0-2. API 활성화

☁️ **아무 셸이나**

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  storage.googleapis.com
```

### 0-3. 이미지 저장소

☁️ **아무 셸이나**

```bash
gcloud artifacts repositories create $REPO \
  --repository-format=docker --location=$REGION \
  --description="M7 Terminal container images"
```

### 0-4. bars.db 보관용 버킷

일봉 캐시는 의도적으로 Firestore가 아니라 로컬 SQLite다(`src/data/cache.py` 상단 주석 참고 —
유니버스 백필이 ~5만 write라 무료 티어를 넘긴다). Cloud Run은 실행이 끝나면 디스크가 사라지므로,
그 파일 하나를 GCS에 주차해 둔다. **잃어버려도 느린 실행 한 번일 뿐, 정합성 문제는 없다.**

> ✅ **완료 (2026-09-01).** 버킷 생성, `bars.db` 21.5MB 업로드, 실행 계정 권한까지.

☁️ **아무 셸이나** — 버킷 생성

```bash
gcloud storage buckets create $BUCKET --location=$REGION

# 실행 계정이 그 파일을 읽고 쓸 수 있게. 프로젝트 전체가 아니라 이 버킷에만 준다
gcloud storage buckets add-iam-policy-binding $BUCKET \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/storage.objectAdmin"
```

📁 **로컬 WSL · 리포 루트** — 초기 업로드는 `bars.db` 파일이 있는 곳에서 해야 한다

```bash
gcloud storage cp bars.db $BUCKET/bars.db
```

> 22MB짜리 파일이다. 이걸 미리 올려두면 첫 클라우드 실행이 Yahoo에서 16년치를 다시 받지 않는다.

### 0-5. 서비스계정과 권한

☁️ **아무 셸이나**

```bash
# 실행용 - Firestore와 Secret Manager. GCS는 0-4에서 버킷 단위로 따로 준다
gcloud iam service-accounts create m7-run-sa --display-name="M7 Terminal runner"
for ROLE in \
  roles/datastore.user \
  roles/secretmanager.secretAccessor
do
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:${SA_EMAIL}" --role="$ROLE"
done

# 빌드용 - 이미지를 올리고 로그를 쓰는 것이 전부
gcloud iam service-accounts create m7-build-sa --display-name="M7 Cloud Build"
for ROLE in roles/artifactregistry.writer roles/logging.logWriter
do
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:${BUILD_SA}" --role="$ROLE"
done

# 스케줄러용 - Job을 호출하는 것 외에는 아무것도
gcloud iam service-accounts create m7-scheduler-sa --display-name="M7 Scheduler"
```

> 레거시 `<번호>@cloudbuild.gserviceaccount.com` 계정은 **이 프로젝트에 없다.** 최근 만들어진
> 프로젝트는 그 계정을 만들지 않고 서비스계정을 직접 지정하게 바뀌었으므로, 빌드 권한은 위의
> `m7-build-sa`에 준다. `logging.logWriter`가 빠지면 `cloudbuild.yaml`이
> `CLOUD_LOGGING_ONLY`라 빌드가 시작하자마자 실패한다.

> **서비스계정 JSON 키는 만들지 않는다.** Cloud Run에서는 붙여둔 서비스계정으로 ADC가 자동 동작하므로
> `GOOGLE_APPLICATION_CREDENTIALS`가 필요 없다. 그 변수는 로컬 개발 전용이다.

### 0-6. 시크릿 등록

이미지에는 비밀이 하나도 들어가지 않는다(`.dockerignore` 참고). 런타임에 주입한다.

📁 **로컬 WSL · 리포 루트** — `config.yaml`과 `.env` 값을 읽으므로 **반드시 리포 루트에서**

> ✅ **완료 (2026-09-01).** 시크릿 6개가 모두 등록돼 있다. 이름은 `.env`의 변수명을 그대로
> 쓴다 - `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `NOTION_TOKEN`, `NOTION_DATABASE_ID`,
> `GOOGLE_AI_API_KEY`, 그리고 설정 파일 전체를 담은 `m7-config`.

```bash
# config.yaml 통째로 (전략 파라미터 + 수동 보유 종목)
gcloud secrets create m7-config --data-file=config.yaml

# API 키들. .env를 셸에 불러온 뒤 실행한다
set -a && . ./.env && set +a
for KEY in TOSS_CLIENT_ID TOSS_CLIENT_SECRET NOTION_TOKEN NOTION_DATABASE_ID GOOGLE_AI_API_KEY
do
  printf '%s' "$(eval echo \$$KEY)" | gcloud secrets create "$KEY" --data-file=-
done
```

> `printf`는 `echo`와 달리 끝에 개행을 안 붙인다. 토큰 끝에 `\n`이 붙으면 인증이 조용히 실패하므로
> 여기서는 `echo`를 쓰지 말 것.

값이 바뀌면 새 버전만 추가하면 된다. **이미지 재빌드는 필요 없다.**

📁 **로컬 WSL · 리포 루트**

```bash
gcloud secrets versions add m7-config --data-file=config.yaml
```

### 0-7. 토스 허용 IP

등록되지 않은 IP의 API 호출은 `403 edge-blocked`로 **전량 차단**된다(README 59행).
Cloud Run의 아웃바운드 IP는 기본적으로 동적이므로, 고정 IP를 만들어 그 IP를 등록해야 한다.

> ✅ **완료 (2026-09-01).** 고정 IP `34.22.70.110`, Cloud Router `m7-router`, NAT `m7-nat`이
> 모두 있고 토스에도 등록돼 있다. 실제 실행으로 403이 사라진 것까지 확인했다.
> **단, Job 쪽에 VPC egress를 붙여야 이 IP로 나간다** (2장 참고) - 인프라만 있고 Job이
> 연결돼 있지 않으면 여전히 403이다.

☁️ **아무 셸이나** — 필요해졌을 때만

```bash
# 개요만. 실제 적용 시 서브넷/커넥터 이름을 정하고 진행할 것.
gcloud compute addresses create m7-nat-ip --region=$REGION
gcloud compute routers create m7-router --network=default --region=$REGION
gcloud compute routers nats create m7-nat \
  --router=m7-router --region=$REGION \
  --nat-external-ip-pool=m7-nat-ip \
  --nat-all-subnet-ip-ranges

# 이 주소를 토스 WTS > 설정 > Open API > 허용 IP 관리 에 등록
gcloud compute addresses describe m7-nat-ip --region=$REGION --format='value(address)'
```

이후 Job에 `--vpc-connector`와 `--vpc-egress=all-traffic`을 붙여야 그 NAT를 타고 나간다.

---

## 1. 매번 하는 배포

### 1-1. (선택) 로컬 검증

클라우드 왕복 없이 빨리 깨지는지 보고 싶을 때만. **배포에 필수가 아니다.**
Docker Desktop이 실행 중이어야 한다.

📁 **로컬 WSL · 리포 루트**

```bash
docker build -t m7-terminal .

# 이미지에는 설정도 비밀도 없으므로 손으로 주입해야 돈다
docker run --rm \
  --env-file .env \
  -v "$PWD/config.yaml:/app/config.yaml:ro" \
  -v "$PWD/secrets:/secrets:ro" \
  -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/<서비스계정>.json \
  m7-terminal python main.py
```

🐳 **컨테이너 안** — 비밀이 정말 안 들어갔는지 확인. 셋 다 "No such file"이어야 정상

```bash
docker run --rm m7-terminal ls /app/config.yaml /app/.env /app/bars.db
```

### 1-2. 배포 — 빌드(자동)와 배포(수동)는 나뉘어 있다

`main`에 push하면 트리거 `m7-build-on-push`가 `cloudbuild.yaml`을 읽고 이미지를
`:<커밋해시>`와 `:latest` 두 태그로 굽는다. **빌드가 성공했을 때만** 레지스트리에
올라가므로 깨진 커밋은 저장소를 건드리지 않는다. 그리고 **거기서 멈춘다.**

배포는 그 이미지를 가리키게 하는 별도의 명령이다. 대상이 셋이고, 고친 코드에 따라
칠 것만 고른다:

| 무엇을 고쳤나 | 배포할 대상 |
|---|---|
| `main.py` · 리포트 파이프라인 | `m7-daily` |
| `trade.py` · 전략 · 리스크 게이트 | `m7-trade` (필요하면 `m7-reconcile`도) |
| `src/dashboard/` | `m7-dashboard` (**Job이 아니라 Service**) |
| `src/store/`, `src/toss/` 등 공용 | 해당하는 것 전부 |

☁️ **아무 셸이나**

```bash
gcloud run jobs update     m7-daily     --image=${IMAGE}:${TAG} --region=$REGION
gcloud run jobs update     m7-trade     --image=${IMAGE}:${TAG} --region=$REGION
gcloud run services update m7-dashboard --image=${IMAGE}:${TAG} --region=$REGION
```

**`services update`와 `jobs update`를 헷갈리지 말 것.** 대시보드만 Service다.
어느 쪽이든 인자·시크릿 볼륨·VPC 애노테이션은 그대로 두고 이미지만 바꾼다 — 그래서
이 줄에 이미지 말고 아무것도 안 적어도 되고, 반대로 **다른 플래그를 같이 적으면 적지
않은 설정이 날아갈 수 있다.**

배포 뒤 정의가 멀쩡한지 한 번 보는 게 싸다. 특히 `command`가 비어 있어야 한다 —
`--command`가 붙으면 `entrypoint.sh`를 건너뛰고 bars.db 동기화가 조용히 사라진다
(2026-09-02에 `m7-trade`가 그래서 매일 죽고 있었다).

```bash
gcloud run jobs describe m7-trade --region=$REGION --format=json \
  | python3 -c "import json,sys; c=json.load(sys.stdin)['spec']['template']['spec']['template']['spec']['containers'][0]; print(c.get('image')); print('command:', c.get('command')); print('args:', c.get('args'))"
```

<details>
<summary>빌드와 배포를 한 번에 하고 싶다면 (최초 생성 때 쓴 방법)</summary>

📁 **로컬 WSL · 리포 루트** — `--source .`가 현재 폴더를 업로드하므로 위치가 곧 배포 내용이다

```bash
gcloud run jobs deploy m7-daily --source . --region=$REGION
```

빌드 → 푸시 → Job 갱신까지 한 번에 한다. Job을 **처음 만들 때**는 이게 편하지만,
평소 배포로 쓰면 위에서 말한 "알아채고 멈출 시간"이 사라진다. 그리고 커밋되지 않은
로컬 변경까지 올라가므로 **배포된 것과 `git log`가 어긋난다.**
</details>

<details>
<summary>로컬 이미지를 직접 올리고 싶다면 (3단계)</summary>

📁 **로컬 WSL · 리포 루트** — Docker Desktop 실행 중이어야 함

```bash
export TAG=$(git rev-parse --short HEAD)
gcloud auth configure-docker ${REGION}-docker.pkg.dev   # 최초 1회
docker build -t ${IMAGE}:${TAG} .
docker push ${IMAGE}:${TAG}
gcloud run jobs update m7-daily --image=${IMAGE}:${TAG} --region=$REGION   # ← 빠뜨리기 쉬움
```

**`:latest`에 덮어쓰기만 하면 반영되지 않는다.** Cloud Run은 배포 시점에 태그를 digest로
고정하므로, 같은 태그로 push해도 Job은 예전 이미지를 계속 실행한다. 그래서 마지막 `update`가
반드시 필요하고, 애초에 커밋 해시를 태그로 쓰면 이 혼동이 사라지고 롤백도 쉬워진다.
</details>

---

## 2. Job 정의

`ENTRYPOINT`가 `entrypoint.sh`이고 그 안에서 `"$@"`를 실행하므로, **컨테이너 인자가 곧 실행할 명령**이다.

> ✅ **`m7-daily` 생성·검증 완료 (2026-09-01).** 실행 성공, Notion 리포트 발행, 스냅샷 저장까지
> 확인.
>
> ✅ **`m7-trade` 정상화 완료 (2026-09-02).** Job 자체는 그 전부터 있었지만 초기 정의 그대로였고,
> `config.yaml` 마운트가 없어 **매일 exit 3으로 죽고 있었다.** 아래 정의에 맞춰 고친 뒤 수동
> 실행으로 PAPER 배너 → 42종목 히스토리 → 보유 2종목 → 신호 0건(수요일) → exit 0 확인.
>
> **같은 실수를 세 Job이 공유하고 있었다.** `m7-report`(= `m7-daily` 중복)와 `m7-reconcile`도
> 같은 이유로 실패 중이었다. `m7-report`는 Job·스케줄 모두 삭제했고, `m7-reconcile`은 정의만
> 고쳐두고 스케줄은 정지시켰다 — LIVE 미체결 주문이 0건인 동안은 30분마다 돌 이유가 없다.

☁️ **아무 셸이나** — 이미 올라간 이미지를 가리키기만 하므로 리포 파일이 필요 없다

```bash
# 일일 리포트 (main.py)
gcloud run jobs create m7-daily \
  --image=${IMAGE}:<커밋해시> --region=$REGION \
  --service-account=$SA_EMAIL \
  --args=python,main.py \
  --task-timeout=30m --max-retries=1 --memory=2Gi \
  --network=default --subnet=m7-run-subnet --vpc-egress=all-traffic \
  --set-env-vars="M7_BAR_CACHE_GCS_URI=${BUCKET}/bars.db,M7_CONFIG_PATH=/secrets/config.yaml" \
  --set-secrets="/secrets/config.yaml=m7-config:latest,\
TOSS_CLIENT_ID=TOSS_CLIENT_ID:latest,\
TOSS_CLIENT_SECRET=TOSS_CLIENT_SECRET:latest,\
NOTION_TOKEN=NOTION_TOKEN:latest,\
NOTION_DATABASE_ID=NOTION_DATABASE_ID:latest,\
GOOGLE_AI_API_KEY=GOOGLE_AI_API_KEY:latest"

# 매매 엔진 (trade.py) — 위와 동일하되 이름과 인자만 다르다
gcloud run jobs create m7-trade \
  ... --args=python,trade.py
```

> **VPC 설정을 빠뜨리면 토스가 403으로 막는다.** 기본 상태의 Cloud Run은 나갈 때마다 IP가
> 바뀌므로 허용 IP 목록에 걸릴 수 없다. `--network/--subnet/--vpc-egress=all-traffic`을 붙여야
> NAT의 고정 IP(0-7)로 나간다. 이걸 빼고 만든 첫 Job이 정확히 이 이유로 실패했다.

> **`config.yaml`은 `/secrets/`에 마운트한다.** Cloud Run의 시크릿 볼륨은 마운트 지점이 속한
> 디렉터리를 덮으므로, `/app/config.yaml`로 걸면 애플리케이션 코드가 통째로 가려질 수 있다.
> 앱은 `M7_CONFIG_PATH`로 그 위치를 전달받는다.

> **`--command`는 쓰지 말 것.** ENTRYPOINT를 덮어써서 `entrypoint.sh`를 건너뛰게 되고,
> 그러면 bars.db GCS 동기화가 사라져 매 실행마다 유니버스 전체를 다시 받는다. 반드시 `--args`만 쓴다.

기존 Job 수정은 `create` 대신 `update`. 이미 만든 뒤 시크릿 하나만 추가할 때도 마찬가지다.

### 관련 CLI 인자

| 명령 | 용도 |
|---|---|
| `python main.py` | 일일 리포트 (Notion 발행) |
| `python trade.py` | 매매 엔진 (기본 PAPER) |
| `python trade.py --reconcile` | 미체결 LIVE 주문 체결 확인 + OCO 등록 |
| `python trade.py --dry-run` | 리스크 게이트까지만, DB 기록 없음 |

`--live`는 아직 **코드에서 거부된다**(exit 4). 설계 6절 [10]에서 최소 수량 1주 검증 후에 열린다.

### 종료 코드

`main.py` / `trade.py` 공통: `0` 정상 · `2` 토스 API 오류 · `3` 예기치 못한 오류.
`trade.py`는 추가로 `1` 엔진 비활성 · `4` `--live` 차단.

---

## 3. 스케줄 (Cloud Scheduler)

> ✅ **완료 (2026-09-01).** `m7-daily-schedule`, 평일 10:00 KST. 수동 발사로 Job이 실제로
> 실행되어 Notion 리포트까지 나가는 것을 확인했다. 시간은 기존 Windows 작업 스케줄러가
> 쓰던 10:00에 맞췄다.
>
> ✅ **`m7-trade-schedule`, KST 월~금 23:35 (2026-09-02).** 킬 스위치(6장)가 붙은 뒤라 조건이
> 충족됐다.
>
> **처음에 10:10으로 걸었던 것은 틀렸다.** 리스크 게이트는 `regularMarket`만 열린 것으로 친다
> (`src/execution/context.py`의 `_sessions`가 `live_session(...) == "regular"`로 좁힌다).
> 토스 캘린더에 `dayMarket`(주간거래)·`preMarket`·`afterMarket`이 같이 오지만 셋 다
> `is_open=False`다. 10:10 KST는 `dayMarket` 시간대라 신호가 나와도 전량
> `order-hours-closed`로 거부됐을 것이다.
>
> 미국 정규장은 KST로 **22:30~05:00**(겨울 23:30~06:00)이고, `bucket-dca`의 매수는 금액
> 주문이라 마감 60분 전 컷오프(`AMOUNT_ORDER_CUTOFF_MINUTES`)까지 걸린다. 두 조건을 서머타임과
> 겨울 양쪽에서 만족하는 창이 **23:30~04:00**이고, 23:35는 그 안이다.
>
> ✅ **`m7-daily-schedule`, KST 화~토 10:00 (2026-09-02).** 미국 정규장이 KST D일 22:30 →
> D+1일 05:00에 걸쳐 있으므로, KST 아침 10:00 리포트가 담는 것은 **전날 밤에 시작된 미국 장**이다.
> 월~금으로 두면 **월요일은 금요일 장을 이틀 늦게 반복**하고 **미국 금요일 장이 토요일 아침에
> 안 나온다.** 화~토가 미국 거래일과 1:1로 맞는 유일한 배치다.
>
> **`m7-reconcile-schedule`은 PAUSED.** 정의는 `m7-trade`와 같게 맞춰뒀으니, 설계 [10]에서
> LIVE를 열 때 `gcloud scheduler jobs resume m7-reconcile-schedule --location=$REGION` 한 줄로
> 재개한다.

☁️ **아무 셸이나**

```bash
gcloud scheduler jobs create http m7-daily-schedule \
  --location=$REGION \
  --schedule="35 23 * * 1-5" \
  --time-zone="Asia/Seoul" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/m7-daily:run" \
  --http-method=POST \
  --oauth-service-account-email=$SCHED_SA

# Scheduler가 Job을 실행하려면 이 권한이 필요하다
gcloud run jobs add-iam-policy-binding m7-daily \
  --member="serviceAccount:${SCHED_SA}" --role="roles/run.invoker" --region=$REGION
```

시간은 실제 운용에 맞춰 조정할 것. 리밸런싱 요일은 `config.yaml`의 `rebalance_weekday`가
따로 정하므로(0=월요일, 그날이 휴장이면 그 주 첫 거래일), **스케줄은 매일 돌려도 무방하다.**

---

## 4. 확인 · 로그 · 롤백

☁️ **아무 셸이나** — 전부 GCP 조회/조작이라 리포 파일이 필요 없다.
급할 때 폰이나 다른 PC에서 Cloud Shell로 들어가 그대로 칠 수 있다.

```bash
# 즉시 한 번 실행
gcloud run jobs execute m7-daily --region=$REGION --wait

# 실행 이력
gcloud run jobs executions list --job=m7-daily --region=$REGION

# 로그
gcloud logging read \
  'resource.type="cloud_run_job" AND resource.labels.job_name="m7-daily"' \
  --limit=100 --format='value(textPayload)'

# 롤백 — 이전 커밋 해시 태그로 되돌린다. 빌드도 push도 필요 없다
gcloud run jobs update     m7-daily     --image=${IMAGE}:<이전해시> --region=$REGION
gcloud run services update m7-dashboard --image=${IMAGE}:<이전해시> --region=$REGION

# 어떤 해시가 레지스트리에 있는지
gcloud artifacts docker images list ${IMAGE} --include-tags \
  --sort-by=~UPDATE_TIME --limit=10
```

대시보드(Service)는 리비전 이력이 따로 있어서 트래픽만 되돌려도 된다:

```bash
gcloud run revisions list --service=m7-dashboard --region=$REGION
gcloud run services update-traffic m7-dashboard --region=$REGION --to-revisions=<리비전>=100
```

---

## 5. 자주 걸리는 것들

| 증상 | 원인 |
|---|---|
| 코드를 고쳤는데 클라우드는 옛날 동작 | `push`만 하고 `jobs update`를 안 함. 태그가 digest로 고정돼 있다 |
| `$PROJECT`가 빈 값으로 들어가 실패 | 터미널을 새로 열고 "공통 변수" `export`를 다시 안 붙여넣음 |
| 엉뚱한 내용이 배포됨 | 📁 명령을 리포 루트 밖에서 실행. `--source .`는 "지금 폴더"를 올린다 |
| `m7-dashboard`에 `jobs update`가 안 먹음 | 대시보드만 **Service**다. `services update`를 쓴다 |
| push했는데 이미지가 안 생김 | 빌드 실패. `gcloud builds list --region=global`로 상태부터 본다 |
| `403 edge-blocked` | 토스 허용 IP. 클라우드 IP가 등록돼 있는지 확인 (0-7) |
| 에러 없이 이상한 설정으로 매매 | `config.yaml` 마운트 누락. 이제는 `require_config_file`이 중단시킨다 |
| 매 실행마다 Yahoo 재다운로드 | `M7_BAR_CACHE_GCS_URI` 미설정, 또는 `--command`로 entrypoint를 덮어씀 |
| Firestore 권한 오류 | 서비스계정에 `roles/datastore.user` 누락 |
| API 키가 유효하지 않다는데 로컬은 됨 | 시크릿에 잘못된 값이 들어간 것. 아래로 길이를 비교한다 |
| 급히 멈춰야 함 | 대시보드에서 킬 스위치 ON. Firestore `system/kill_switch` 문서 — **재배포 불필요, 즉시 반영** |

시크릿 값이 `.env`와 같은지는 값을 화면에 띄우지 않고 길이로 확인할 수 있다. 실제로
`GOOGLE_AI_API_KEY`에 20바이트짜리 엉뚱한 값이 들어가 AI 분석만 조용히 실패한 적이 있다.

📁 **로컬 WSL · 리포 루트**

```bash
set -a && . ./.env && set +a
for K in TOSS_CLIENT_ID TOSS_CLIENT_SECRET NOTION_TOKEN NOTION_DATABASE_ID GOOGLE_AI_API_KEY
do
  L=$(eval echo -n "\${#$K}")
  S=$(gcloud secrets versions access latest --secret=$K | wc -c)
  [ "$L" = "$S" ] && echo "$K 일치" || echo "$K 불일치 (.env=$L, secret=$S)"
done
```

고칠 때는 새 버전만 추가하면 된다. Job은 `:latest`를 보므로 **재배포가 필요 없다.**

```bash
printf '%s' "$GOOGLE_AI_API_KEY" | gcloud secrets versions add GOOGLE_AI_API_KEY --data-file=-
```

> ⚠️ **`config.yaml`이 없어도 크래시하지 않는다.** `_load_yaml()`은 파일이 없으면 빈 dict를
> 반환한다(`src/config.py:268`). 즉 마운트를 깜빡하면 의도한 전략이 아닌 기본값으로 조용히 주문이
> 나갈 수 있다. 클라우드 운용 전에 "설정이 비어 있으면 즉시 중단" 가드를 넣는 것이 좋다. **(미착수)**

---

## 6. 대시보드 (Cloud Run **Service**)

여기까지는 전부 Job — "실행하고 끝나는" 배치다. 대시보드는 **계속 떠 있어야** 하므로
같은 이미지를 Cloud Run **Service**로 따로 올린다.

> ⚠️ **순서가 중요하다.** 킬 스위치를 밖에서 내리려면 대시보드가 먼저 떠 있어야 한다.
> `Job 배포 → 대시보드 + IAP → 그 다음에 Scheduler 켜기` 순서를 지킬 것.
> 브레이크에 손이 닿기 전에 시동을 걸지 않는다.

### 6-1. 인증 없이 공개하면 안 되는 이유

`src/dashboard/api.py:5`에 그대로 적혀 있다 — **이 앱에는 인증이 없다.**
지금까지 안전했던 건 오직 `127.0.0.1`에 묶여 있었기 때문이다.

그대로 공개하면 **URL을 아는 누구나 킬 스위치를 내리고 포트폴리오 전체를 볼 수 있다.**
그래서 6-3의 IAP가 선택이 아니라 필수다. 6-2와 6-3은 반드시 붙여서 진행한다.

### 6-2. Service 배포

> ✅ **완료 (2026-09-01).** `m7-dashboard` 배포됨. 잠금 확인 - 인증 없이 403, 토큰으로 200.
> 이미 빌드된 이미지를 쓰므로 `--source .` 대신 `--image`를 썼다.
>
> **이 403은 여기까지의 상태다.** 6-3에서 IAP를 앞에 세우고 나면 같은 요청이
> **302**(구글 로그인으로 리다이렉트)로 바뀐다. 둘 다 "잠겨 있음"이고, 지금 확인하면
> 302가 정답이다 — 6-3 5)번 참고.
>
> 이후 코드 변경을 반영할 때는 1-2의 `gcloud run services update --image` 한 줄이면
> 된다. 여기 있는 긴 명령은 **처음 만들 때** 쓰는 것이다.

☁️ **아무 셸이나** (아래는 `--source .` 형태이므로 📁 로컬 리포 루트)

```bash
gcloud run deploy m7-dashboard --source . --region=$REGION \
  --service-account=$SA_EMAIL \
  --args=uvicorn,src.dashboard.api:app,--host,0.0.0.0,--port,8080 \
  --port=8080 \
  --no-allow-unauthenticated \
  --min-instances=0 \
  --set-env-vars="M7_CONFIG_PATH=/secrets/config.yaml" \
  --set-secrets="/secrets/config.yaml=m7-config:latest,\
TOSS_CLIENT_ID=TOSS_CLIENT_ID:latest,\
TOSS_CLIENT_SECRET=TOSS_CLIENT_SECRET:latest"
```

포인트 넷:

- **`--host 0.0.0.0`** — 로컬에서 쓰던 `127.0.0.1`이면 Cloud Run이 컨테이너에 요청을 넣지 못해
  배포가 그대로 실패한다. 이 한 줄이 로컬과 클라우드의 유일한 실행 차이다.
- **`--no-allow-unauthenticated`** — 기본을 잠근 상태로 띄운다. 6-3에서 IAP로 열 때까지
  아무도 못 들어온다. **이 플래그를 빼고 띄우는 순간 킬 스위치가 인터넷에 공개된다.**
- **`M7_BAR_CACHE_GCS_URI`를 설정하지 않는다** — 이유는 6-4.
- **`--min-instances=0`** — 안 볼 때는 0원. 대신 첫 접속이 몇 초 느리다. 급할 때 그 몇 초가
  아깝다면 `1`로 올린다(상시 과금).

Notion/AI 키는 대시보드가 쓰지 않으므로 뺐다. 화면에서 뭔가 비어 보이면 그때 추가한다.

### 6-3. IAP로 잠그기 — 내 구글 계정만

> ✅ **완료 (2026-09-02).** 인증 없이 접근하면 구글 로그인으로 302 리다이렉트되고,
> `acc22ai@gmail.com`만 통과한다. 코드는 한 줄도 건드리지 않았다.

**핵심은 커스텀 OAuth를 쓰는 것이다.** 기본값인 *Google 관리 OAuth*는 이 프로젝트에서 동작하지
않는다 - 클라이언트를 Google이 자동 발급하는데 그 발급이 **조직 소속 프로젝트를 전제로 한다**.
조직 없는 개인 프로젝트라 발급이 안 되고, 그 결과가 모든 요청에 대한 **502**다. 502 본문을
직접 읽어야 이유가 나온다(브라우저는 감춘다):

```bash
curl -s https://m7-dashboard-ofhlnvd7jq-du.a.run.app
# Empty Google Account OAuth client ID(s)/secret(s).
```

**1) OAuth 클라이언트 만들기** —
[클라이언트 페이지](https://console.cloud.google.com/auth/clients?project=quant-81f19) >
클라이언트 만들기 > **웹 애플리케이션** > 이름 `M7 Terminal IAP`.
동의 화면(브랜딩)이 없으면 먼저 만들라고 안내한다. 대상은 **외부**, 테스트 사용자에 본인 계정.
게시(Publish)는 하지 않는다 - 본인만 쓰는 화면에 Google 검수를 붙일 이유가 없다.

**2) 리디렉션 URI 추가** — 만든 클라이언트를 다시 열어 **승인된 리디렉션 URI**에:

```
https://iap.googleapis.com/v1/oauth/clientIds/633981904995-hs1j2joo9c5p3plafijda813dii6pq3l.apps.googleusercontent.com:handleRedirect
```

클라이언트 ID 전체 뒤에 `:handleRedirect`를 붙인 형태다. **JavaScript 원본은 비워둔다** -
IAP는 서버 측 리다이렉트만 쓴다. 클라이언트 ID는 비밀이 아니지만(로그인할 때마다 브라우저를
지나간다) **보안 비밀은 어디에도 적지 않는다.**

**3) IAP에 그 클라이언트를 물린다** —
[IAP 페이지](https://console.cloud.google.com/security/iap?project=quant-81f19)에서
**'모든 웹 서비스' 행의 설정**을 연다. 서비스 행의 ⋮ 메뉴가 아니라 **상위 행**이다.
여기서 한참 헤맸다.

> OAuth 구성 > **커스텀 OAuth** 선택 > 클라이언트 ID와 보안 비밀 입력 > 저장.
> 보안 비밀은 **저장 후 다시 조회할 수 없으니** 먼저 따로 보관해둘 것.

**4) 접근 권한** — 코드가 아니라 IAM으로 정한다.

☁️ **아무 셸이나**

```bash
gcloud iap web add-iam-policy-binding --resource-type=cloud-run \
  --service=m7-dashboard --region=$REGION \
  --member="user:someone@example.com" --role="roles/iap.httpsResourceAccessor"
```

IAP 서비스 에이전트가 뒷단을 호출할 수 있어야 한다(`--iap`를 켤 때 자동으로 붙지만 확인할 것):

```bash
gcloud run services add-iam-policy-binding m7-dashboard --region=$REGION \
  --member="serviceAccount:service-<프로젝트번호>@gcp-sa-iap.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

**5) 확인** — **302**가 정답이다. 200이면 잠기지 않은 것이고, 502면 3)이 빠진 것이다.

```bash
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" \
  https://m7-dashboard-ofhlnvd7jq-du.a.run.app
```

브라우저로 볼 때는 **시크릿 창**에서 한다. 로그인된 창은 그냥 통과해서 잠금 여부를 구분할 수 없다.

> **CLI만으로는 끝낼 수 없다.** `gcloud iap web enable`의 `--oauth2-client-id/-secret`은
> `app-engine`과 `backend-services`만 지원하고 `cloud-run`은 받지 않는다.
> `gcloud iap settings get --resource-type=cloud-run`은 리소스 파싱조차 못 한다.
> IAP OAuth 관리 API는 2026-03-19에 종료됐다. 3)은 콘솔에서만 가능하다.

### 6-4. 대시보드에는 bars.db를 공유하지 않는다

대시보드도 전일 종가 계산에 일봉을 쓴다(`src/dashboard/service.py:96`, 12일치).
그런데 Job과 같은 GCS 파일을 공유하게 두면 **`entrypoint.sh`가 종료 시 업로드**하므로,
12일치만 든 얇은 캐시가 Job의 16년치 캐시를 덮어쓸 수 있다. Service는 인스턴스가 여럿 뜨고
종료 시점도 제각각이라 경합이 생긴다.

그래서 Service에는 `M7_BAR_CACHE_GCS_URI`를 **설정하지 않는다.** 동기화 스크립트는 이 변수가
없으면 아무것도 하지 않고 그냥 빠진다(`scripts/sync_bar_cache.py`의 `_blob()`).
대신 인스턴스가 새로 뜰 때마다 yfinance에서 12일치를 받는데, 이건 충분히 싸다.

---

## 7. 푸시하면 이미지가 자동으로 만들어지게 (빌드만)

`main`에 푸시하면 Cloud Build가 이미지를 만들어 Artifact Registry에 넣는다.
**거기서 멈춘다** — 돌고 있는 Job은 건드리지 않는다.

왜 배포까지 자동으로 하지 않는가: 이 리포는 실제로 주문을 낸다. 나쁜 커밋이 스스로 다음 스케줄
실행까지 도달할 수 있으면, 알아채고 멈출 수 있는 시간이 `git push`와 그 실행 사이뿐이다.
미리 빌드해두는 것이 오히려 수동 배포를 싸게 만든다 — 배포가 빌드를 기다리는 일이 아니라
**포인터를 바꾸는 몇 초**가 되기 때문이다.

설정은 `cloudbuild.yaml`에 있다.

### 7-1. 트리거 만들기 (최초 1회 — 완료됨)

**2026-09-01에 설정 완료.** 다시 만들 일이 생겼을 때를 위해 절차를 남긴다.

**GitHub 연결은 콘솔에서만 된다.** 권한을 주는 주체가 GitHub이라 OAuth 승인이 필요하고,
`gcloud`가 대신할 수 없다. 연결하면 GitHub에 Cloud Build 앱이 설치되어 ① 푸시 웹훅과
② 저장소 읽기 권한을 얻는다.

**1) 저장소 연결** — 셋 중 아무거나. 콘솔 창이 좁으면 버튼이 접혀 안 보이니 창을 넓힐 것.

- [연결 마법사](https://console.cloud.google.com/cloud-build/triggers/connect?project=quant-81f19) 직행
- GitHub에서 먼저 설치: [github.com/apps/google-cloud-build](https://github.com/apps/google-cloud-build)
  → Anakin787 → **Only select repositories** → `m7-terminal`만 체크
- [트리거](https://console.cloud.google.com/cloud-build/triggers?project=quant-81f19) >
  트리거 만들기 > 소스 드롭다운 > **새 저장소 연결**

**1세대**를 쓴다. 리전은 **`global (전역)`** — 2세대는 트리거 인자 형식이 달라 굳이 복잡해진다.
`호스트 연결`은 GitHub Enterprise/자체 호스팅용이니 건드리지 않는다.

**2) 트리거 폼**

| 항목 | 값 |
|---|---|
| 이름 | `m7-build-on-push` |
| 리전 | `global (전역)` — 연결과 같아야 한다 |
| 이벤트 | 브랜치로 푸시 |
| 소스 | `Anakin787/m7-terminal (1세대)`, 브랜치 `^main$` |
| 구성 유형 | **Cloud Build 구성 파일(YAML)** — 자동 감지 아님 |
| 구성 파일 위치 | `cloudbuild.yaml` |
| 서비스 계정 | `m7-build-sa@quant-81f19.iam.gserviceaccount.com` |

"샘플 트리거"는 쓰지 않는다. 빌드 구성이 "자동 감지"라 `cloudbuild.yaml`을 무시하고,
태그가 커밋 해시로 붙지 않으며 브랜치도 `main` 한정이 아니다.

☁️ **아무 셸이나** — 확인

```bash
gcloud builds triggers list --region=global \
  --format='table(name,github.push.branch,filename,serviceAccount)'
```

> **리전은 `global`이다.** 이미지가 올라가는 Artifact Registry(asia-northeast3)와는 별개이고,
> 서로 달라도 정상이다. `--region=asia-northeast3`으로 조회하면 아무것도 안 나온다.

### 7-2. 빌드 결과 확인

☁️ **아무 셸이나**

```bash
# 최근 빌드 상태
gcloud builds list --region=$REGION --limit=5 \
  --format='table(id,status,substitutions.SHORT_SHA,createTime)'

# 실패했으면 로그
gcloud builds log <BUILD_ID> --region=$REGION

# 레지스트리에 올라온 태그 목록 (최신순)
gcloud artifacts docker tags list $IMAGE --format='table(tag,version)' | head
```

빌드는 커밋 해시(`$SHORT_SHA`)와 `latest` 두 개의 태그를 붙인다.
**배포에는 해시를 쓴다** — `latest`는 사람이 눈으로 최신을 찾기 위한 것이고,
무엇이 배포됐는지 나중에 말해주지 못한다.

### 7-3. 만들어진 이미지를 배포하기

여기가 사람이 결정하는 지점이다. 빌드는 이미 끝나 있으므로 몇 초면 된다.

☁️ **아무 셸이나** — 리포 파일이 필요 없다. 폰이나 다른 PC의 Cloud Shell에서도 된다

```bash
# 1. 배포할 커밋을 정한다 (로컬이라면 git rev-parse --short HEAD)
export TAG=<커밋해시>

# 2. Job을 그 이미지로 가리킨다
gcloud run jobs update m7-daily --image=${IMAGE}:${TAG} --region=$REGION
gcloud run jobs update m7-trade --image=${IMAGE}:${TAG} --region=$REGION

# 3. 스케줄을 기다리지 말고 한 번 돌려서 확인한다
gcloud run jobs execute m7-daily --region=$REGION --wait

# 4. 지금 무엇이 배포돼 있는지
gcloud run jobs describe m7-daily --region=$REGION \
  --format='value(template.template.containers[0].image)'
```

대시보드도 같은 이미지를 쓴다:

```bash
gcloud run services update m7-dashboard --image=${IMAGE}:${TAG} --region=$REGION
```

**롤백은 같은 명령에 이전 해시를 넣는 것**이 전부다. 빌드도 재현도 필요 없다.

```bash
gcloud run jobs update m7-daily --image=${IMAGE}:<이전해시> --region=$REGION
```

### 7-4. 배포 전 확인할 것

이미지가 만들어졌다는 것은 **빌드가 됐다는 뜻이지, 동작한다는 뜻이 아니다.**
`cloudbuild.yaml`은 테스트를 돌리지 않는다(에뮬레이터가 필요해서 빌드 환경에서 무겁다).

📁 **로컬 WSL · 리포 루트** — 배포할 커밋에서

```bash
python3 -m pytest -q
```

특히 매매 로직(`src/execution/`, `src/strategy/`)을 건드린 커밋은 이걸 통과시키고 배포한다.

---

## 남은 일

- [ ] **실패 알림** — Job 실패가 조용히 묻히지 않도록 로그 기반 알림 설정. 이제 실패 로그가
      깨끗해졌으니(중복 Job 삭제, reconcile 정지) 알림을 붙일 만한 상태다
- [ ] **로컬 Windows 작업 스케줄러 해제** — 클라우드로 넘어왔는데 로컬 예약 실행이 살아 있다.
      2026-09-02 10:00에 로컬(스냅샷 14행)과 `m7-daily`(15행)가 **둘 다** 돌아 노션에 리포트가
      두 번 올라갔다. 이건 PC에서 직접 꺼야 한다
- [ ] **첫 리밸런스 실행 확인** — `bucket-dca`의 `rebalance_weekday: 0`은 "월요일 장에 체결"이
      아니라 **"월요일 봉으로 판단"**이고, 백테스트는 그 판단을 다음 봉 시가에 체결한다
      (`src/backtest/fills.py`). 따라서 리밸런스가 도는 것은 **KST 화요일 23:35**이고 체결은
      미국 화요일 정규장이다. 미해결 B1(매수가능금액이 USD로 잡히는가)이 드러날 자리다
- [ ] **`risk_limits()`의 float→Decimal 변환** — `RiskLimits(**self.limits)`가 YAML의 float를
      그대로 넘기는데 필드는 `Decimal`로 선언돼 있다. `Decimal("0.60") > 0.6`이 참이라
      정확히 같은 값으로 맞춘 한도가 초과로 거부된다. config.yaml이 override를 "경계 반올림
      여유를 둔 값"으로 패딩해 둔 것이 이 증상의 우회다 — 지금은 실害 없음

### 완료

- [x] 자동 빌드 (7장) · `m7-daily` Job (2장) · 스케줄 (3장) · 고정 IP/NAT (0-7)
- [x] 시크릿·버킷·서비스계정 (0-4~0-6) · 대시보드 배포 + IAP (6장)
- [x] `m7-trade` Job 정상화 + 정규장 스케줄 (2·3장, 2026-09-02)
- [x] 활성 전략을 `bucket-dca`로 정정 + 미마감 세션 봉 가드 (2026-09-02)
- [x] 중복 `m7-report` 삭제 · `m7-reconcile` 정의 수정 후 스케줄 정지 (2026-09-02)

---

## 부록 A. 구조 — 무엇이 무엇에 걸려 있나

네 개의 **사슬**로 되어 있다. 각각 독립적으로 끊기므로, 뭔가 안 될 때는 먼저
**어느 사슬인지** 좁히는 것이 가장 빠르다.

### 1. 배포 — 코드가 실행 가능한 물건이 되기까지

```
[내 PC] git push
   │
   ▼
[GitHub] Anakin787/m7-terminal
   │   웹훅 ← Cloud Build 앱이 설치돼 있어서 가능
   ▼
[Cloud Build] 트리거 m7-build-on-push        (리전: global)
   │   · cloudbuild.yaml 을 읽어 빌드
   │   · m7-build-sa 신원으로 실행
   ▼
[Artifact Registry] m7-terminal:<커밋해시> + :latest   (리전: asia-northeast3)
   │
   │   ✋ 자동은 여기서 끝. 반영은 사람이 결정한다 (7-3)
   ▼
[Cloud Run] m7-daily (Job) · m7-dashboard (Service)
```

셋의 역할이 다르다. **GitHub 연결**은 웹훅과 소스 읽기 권한, **트리거**는 언제 무엇으로
빌드할지, **`cloudbuild.yaml`**은 어떻게 빌드할지. 하나만 빠져도 자동 빌드가 멈춘다.

트리거는 `global`, 레지스트리는 `asia-northeast3`. **서로 달라도 정상이다** — 1세대 GitHub
연결이 global에 살기 때문. 엉뚱한 리전으로 조회하면 아무것도 없는 것처럼 보인다.

### 2. 인증 — 밖에서 대시보드로 들어오는 길

```
[브라우저]
   │
   ▼
[IAP] ── 로그인 안 됨? ──► [OAuth 클라이언트] ──► accounts.google.com
   │                        M7 Terminal IAP          │
   │                        + 동의 화면(브랜딩)         │
   │   ◄──────── :handleRedirect 로 돌아옴 ◄──────────┘
   │
   ├─ 이 사람 들어와도 되나? → IAM: iap.httpsResourceAccessor
   │
   ▼
[IAP 서비스 에이전트] ── run.invoker ──► [Cloud Run m7-dashboard]
```

| 요소 | 답하는 질문 |
|---|---|
| OAuth 클라이언트 | 로그인 화면을 **어떻게** 띄우나 |
| 동의 화면(브랜딩) | 로그인 화면에 **뭐라고** 표시하나 |
| `iap.httpsResourceAccessor` | **누가** 들어올 수 있나 |
| IAP 에이전트의 `run.invoker` | IAP가 **뒷단에 닿을** 수 있나 |

502가 하루를 잡아먹은 건 첫 줄이 비어 있어서였다. 나머지 셋은 내내 맞아 있었다 — 사슬이라
하나만 끊겨도 전체가 안 된다.

대시보드 서비스 자체는 여전히 `--no-allow-unauthenticated`로 잠겨 있다. **IAP가 유일한
통로다.**

### 3. 신원 — 코드가 무엇을 만질 수 있나

```
[Cloud Scheduler] m7-daily-schedule
   │  m7-scheduler-sa · run.invoker   ← Job을 부를 권한만
   ▼
[Cloud Run Job] m7-daily
   │  m7-run-sa 신원으로 컨테이너 실행
   ├──► Firestore        (datastore.user)
   ├──► GCS bars-cache   (objectAdmin — 이 버킷에만)
   └──► Secret Manager   (secretAccessor)
```

| 계정 | 할 수 있는 것 |
|---|---|
| `m7-build-sa` | 이미지 올리기, 로그 쓰기. 그게 전부 |
| `m7-run-sa` | Firestore · GCS · Secret 접근 |
| `m7-scheduler-sa` | Job 호출. 그게 전부 |

**2번과 3번은 완전히 별개다.** 2번은 사람이 들어오는 문, 3번은 코드가 나가는 손이다. 같은
IAM이지만 방향이 반대다.

### 4. 네트워크 — 토스가 우리를 알아보는 길

```
[Cloud Run Job/Service]
   │  --vpc-egress=all-traffic   ← 이게 없으면 아래를 안 탄다
   ▼
[VPC] default / m7-run-subnet
   ▼
[Cloud NAT] m7-nat ── m7-router
   ▼
고정 IP 34.22.70.110
   ▼
[토스 허용 IP 목록] ✅
```

고정 IP와 NAT가 있어도 **Job에 VPC 플래그가 없으면 소용없다.** 첫 실행이 403으로 죽은 것이
정확히 이 이유였다 — 인프라는 다 있었는데 Job이 그리로 나가지 않았다.

### 곁가지: 설정과 데이터

```
config.yaml ──► Secret Manager(m7-config) ──► /secrets/config.yaml
                                                   ▲
                             M7_CONFIG_PATH 가 위치를 알려준다
```

`/app/config.yaml`이 아닌 이유는 Cloud Run 시크릿 볼륨이 **마운트 지점이 속한 디렉터리를
덮기** 때문이다. `/app`에 걸면 애플리케이션 코드가 가려진다.

`bars.db`는 실행 전후로 GCS와 동기화하되 **대시보드에는 연결하지 않는다** (6-4).

### 한 장으로

```
      코드 ──1──► 이미지 ──(수동)──► 실행 중인 컨테이너
                                          │
      사람 ──2──► IAP ────────────────────┤  (들어옴)
                                          │
                                          ├──3──► Firestore/GCS/Secret  (신원)
                                          └──4──► NAT 고정 IP ──► 토스   (나감)
```

### 증상으로 사슬 좁히기

| 증상 | 사슬 |
|---|---|
| 푸시했는데 이미지가 안 생김 | 1 — 트리거/웹훅 |
| 이미지는 생겼는데 동작이 그대로 | 1 — 배포는 수동이다 (7-3) |
| 대시보드가 502 / 로그인 화면이 안 뜸 | 2 — OAuth 클라이언트 |
| 대시보드에서 403 | 2 — `iap.httpsResourceAccessor` |
| Firestore·Secret 권한 오류 | 3 — `m7-run-sa` 역할 |
| 스케줄이 Job을 못 부름 | 3 — `m7-scheduler-sa`의 `run.invoker` |
| 토스 `403 edge-blocked` | 4 — VPC 플래그 또는 허용 IP |
