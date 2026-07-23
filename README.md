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
- `COGS_PACKAGE`: Cog 자동 탐색에 사용할 import 패키지입니다. 기본값은 `cogs`입니다.
- `DISABLED_COGS`: 비활성화할 Cog 이름 또는 전체 확장 경로를 쉼표로 적습니다.
- `MESSAGE_CONTENT_INTENT`: 메시지 내용을 읽어야 할 때만 `true`로 설정합니다.

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
