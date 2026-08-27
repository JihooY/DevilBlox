# DevilBlox

DevilBlox Discord bot.

## 구조

```text
DevilBlox/
  main.py              # 얇은 실행 진입점
  core/                # 런타임 설정, Rich 로그, Cog 로더, 봇 클라이언트
  cogs/                # Discord 명령어와 이벤트 리스너
  database/            # MongoDB 저장소 계층
  utils/               # 임베드, 에셋, 역할, 권한, 티켓 유틸
  scripts/             # 유지보수와 마이그레이션 스크립트
  assets/              # 로고, 배너, GIF 패널
```

## 실행 준비

```bash
uv sync
```

`.env.example`을 복사해 `.env`를 만들고 Discord/MongoDB 값을 채워 주세요.

```env
DISCORD_TOKEN=
MONGO_URI=mongodb://127.0.0.1:27017
MONGO_DB_NAME=devilblox
```

## 실행

```bash
uv run python main.py
```

터미널 출력은 Rich 기반으로 정리되어 시작 패널, 서버 요약, 읽기 쉬운 traceback을 보여줍니다.

## 런타임 옵션

- `LOG_LEVEL`: 봇 로그 레벨입니다. 예: `INFO`, `DEBUG`.
- `DISCORD_LOG_LEVEL`: Discord 라이브러리 로그 레벨입니다. 보통 `WARNING`을 사용합니다.
- `SYNC_COMMANDS`: `false`로 두면 슬래시 명령어 동기화를 건너뜁니다.
- `COGS_PACKAGE`: Cog 자동 탐색에 사용할 import 패키지입니다. 기본값은 `cogs`이며, 패키지 안에서 `cogs_`로 시작하는 모듈만 확장으로 로드합니다.
- `DISABLED_COGS`: 비활성화할 Cog 이름 또는 전체 확장 경로를 쉼표로 적습니다.
- `MESSAGE_CONTENT_INTENT`: 메시지 내용을 읽어야 할 때만 `true`로 설정합니다.
- `MUSEUM_URL`: `/이벤트시작`이 안내하는 2,000명 기념 역사 박물관 페이지 주소입니다. 소스는 `web/museum/index.html`이며, 직접 호스팅한 뒤 그 주소로 덮어써주세요.

## 거래중개 시스템

관리자는 기존 설정 명령으로 아래 키를 먼저 연결한 뒤 `/설정확인`으로 점검합니다.

- `/역할설정`: `admin`(관리자), `brokerage_helper`(티켓 도우미), `brokerage_alert`(거래 알림)
- `/채널설정`: `brokerage`(게시판), `brokerage_panel`(통합 패널), `brokerage_admin`(관리 패널), `brokerage_log`(감사 로그), `brokerage_report`(신고)
- `/카테고리설정`: `brokerage_ticket`(진행 티켓), `brokerage_closed`(종료 티켓)

`/거래중개패널`은 신용도·패널티 확인, 거래 등록, 알림 기준/중복 방지 설정과 본인 인증을 제공하는 통합 패널을 설치합니다. `/거래중개관리패널`은 거래 삭제, 신용도·문제 횟수 조정, 운영 정책 변경과 현황 확인을 제공하며 관리자만 사용할 수 있습니다. 게시물의 `구매 / 예약`, `신고`, `좋아요` 버튼으로 구매 대기열과 도우미가 참여하는 거래 티켓을 운영합니다.

관리자용 세부 명령은 `/거래중개설정`, `/신용도조정`, `/거래삭제`, `/거래문제처리`, `/거래문제해결`, `/거래후기수정`입니다. 문제 처리 명령은 환불·회수 금액 기반 감점과 패널티를 적용하고, 해결 명령은 오판 취소 시 문제 횟수와 감점을 복원합니다.

기본 정책은 다음과 같으며 관리 패널에서 조정할 수 있습니다.

- 신용도 범위는 `-100..100`이며, 거래 금액 `1,000원`당 1점(최소 1점, 최대 10점)을 계산합니다. 문제 없는 거래는 완료 7일 후 가점되고 환불·회수 등 문제가 확인되면 같은 기준으로 감점됩니다.
- 완료 후 7일 동안 구매자가 1~5점 후기와 내용을 남길 수 있고, 2점 이하는 문제 1회로 집계됩니다. 별점 테러 등 오판은 관리 패널에서 신용도와 문제 횟수를 정정할 수 있습니다.
- 문제 1~3회에는 추가 배율이 없고 4회부터 패널티 1단계가 시작됩니다. 다음 감점은 단계마다 10%씩 커집니다(4회 10%, 5회 20%).
- 게시 재등록 주기는 신용도 `80 이상 10분`, `50~79 20분`, `0~49 30분`, `-1 이하 60분`입니다. 좋아요 보상과 재등록 단축 기준도 관리 패널에서 변경할 수 있습니다.
- 이메일 인증은 10점, 전화번호 인증은 20점을 한 번 지급합니다.

본인 인증을 사용하려면 `.env`에 `BROKERAGE_VERIFICATION_PEPPER`를 긴 무작위 비밀값으로 반드시 지정합니다. 이메일은 `BROKERAGE_SMTP_HOST`, `BROKERAGE_SMTP_PORT`, `BROKERAGE_SMTP_USERNAME`, `BROKERAGE_SMTP_PASSWORD`, `BROKERAGE_SMTP_FROM`, `BROKERAGE_SMTP_USE_TLS`를, SMS는 `BROKERAGE_TWILIO_ACCOUNT_SID`, `BROKERAGE_TWILIO_AUTH_TOKEN`, `BROKERAGE_TWILIO_FROM_NUMBER`를 설정합니다. 이메일·전화번호·인증번호 원문은 저장하지 않으며, 중복 확인용 HMAC 해시와 마스킹된 표시값만 보관합니다.

## 서버 관리와 트래픽 보호

관리자가 `/서버관리패널`을 실행하면 CPU, RAM, 디스크, NVIDIA GPU, 네트워크 송수신 속도,
Discord 게이트웨이 지연 시간과 TCP 연결 지연 시간을 표시하는 패널을 설치합니다.
`OPERATIONS_CHANNEL_ID`를 지정하면 해당 채널에 패널을 자동 설치하고 traceback 경보도 같은 채널로 보냅니다.

네트워크 수신/송신 속도가 설정값을 연속 `MONITOR_TRIGGER_SAMPLES`회 넘으면 비상 절전 모드가 켜집니다.
이 상태에서는 새 GIF 전송을 차단한 뒤 추적 중인 Embed 및 Components V2 패널에서 GIF 첨부와
`MediaGallery` 참조를 제거합니다. 트래픽이 기준값 × `MONITOR_RECOVERY_RATIO` 아래에서 연속으로
유지되고 최소 쿨다운이 지나야 자동 복구되므로 경계값 부근에서 ON/OFF가 반복되지 않습니다.

GIF 전달 방식은 다음과 같습니다.

- `GIF_DELIVERY_MODE=local`: `assets/gifs` 파일을 Discord에 직접 첨부합니다.
- `GIF_DELIVERY_MODE=cdn`: `GIF_CDN_BASE_URL/<파일명>`을 직접 참조하고 로컬 GIF는 업로드하지 않습니다.
- `GIF_DELIVERY_MODE=auto`: CDN 주소가 있으면 CDN, 없으면 local을 사용합니다.
- `GIF_ROTATION_ENABLED=false`: 기존의 매분 원본 GIF 재업로드를 중단합니다. 기본값이자 권장값입니다.
- `GIF_LOCAL_VARIANT=optimized`: 로컬 모드에서 `assets/gifs_optimized`를 우선해 업로드 크기를 더 줄입니다.
- `GIF_RECOVERY_UPLOAD_INTERVAL=5`: 비상 상태 해제 후 local 모드에서 새 GIF 업로드를 시도할 때,
  호출 시점 기준으로 지정한 최소 간격 안에는 최대 한 건만 허용합니다. 별도의 5초 주기 복구
  스케줄러는 아니며, 보호는 복구 제어기가 명시적으로 종료할 때까지 시간 만료 없이 유지됩니다.
  CDN 모드는 파일 업로드가 없으므로 이 gate를 거치지 않고 URL만 복원합니다.

비상/수동 절전 상태는 MongoDB의 `operations_state`에 저장됩니다. 프로세스가 공격이나 자원 부족으로
재시작돼도 저장된 차단 상태를 먼저 복원한 뒤 모니터링을 재개합니다. 새로 생성되는 공개 패널과 열린
티켓의 시작 메시지는 추적되며, 비상 정리 시 일반 Embed 이미지와 Components V2 `MediaGallery`를 함께
제거합니다. 관리 패널의 `비상 절전 ON`, `자동 모드`, `GIF 즉시 정리` 버튼으로 수동 대응도 가능합니다.

GPU는 `nvidia-smi`가 있을 때 NVIDIA 사용률, VRAM, 온도를 표시하며 그 외 환경에서는 미지원으로
표시합니다. 컨테이너에서 실행하면 시스템 수치는 호스트 전체가 아니라 컨테이너가 볼 수 있는 범위일
수 있습니다. 이 자동 완화는 봇 자체의 송신량과 부하를 줄이는 기능이며, 인바운드 DDoS 차단은 호스팅
업체의 방화벽, Anycast/CDN, reverse proxy 및 rate limit을 함께 사용해야 합니다.

전체 traceback은 `LOG_FILE`에 크기 순환 방식으로 저장됩니다. 예기치 않은 Slash 명령, 이벤트,
백그라운드 작업 오류는 오류 ID와 축약 traceback으로 관리 채널에 전송되며 같은 오류는 짧은 시간 동안
중복 전송하지 않습니다.

## SQLite 마이그레이션

```bash
uv run python scripts/migrate_sqlite_to_mongo.py --sqlite DEVILROBLOX_extracted/UserData.db
```
