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

## SQLite 마이그레이션

```bash
uv run python scripts/migrate_sqlite_to_mongo.py --sqlite DEVILROBLOX_extracted/UserData.db
```
