# DevilBlox

DevilBlox Discord bot.

## 주요 구조

- `main.py`: 봇 실행, MongoDB 연결, Cog 로딩
- `database/`: MongoDB 저장소 계층
- `cogs/`: 인증, 구매, 문의, 중개, 계정, 알림, 설정 기능
- `utils/`: 임베드, 권한, 역할, 에셋, 티켓 유틸
- `scripts/migrate_sqlite_to_mongo.py`: 기존 `UserData.db` 마이그레이션

## 실행 준비

1. 의존성 동기화

```bash
uv sync
```

2. `.env.example`을 참고해서 `.env` 작성

```env
DISCORD_TOKEN=
MONGO_URI=mongodb://127.0.0.1:27017
MONGO_DB_NAME=devilblox
```

3. 실행

```bash
uv run python main.py
```

## SQLite 마이그레이션

zip에서 나온 `UserData.db`를 MongoDB로 옮길 때:

```bash
uv run python scripts/migrate_sqlite_to_mongo.py --sqlite DEVILROBLOX_extracted/UserData.db
```

마이그레이션 후 Discord에서 `/설정확인`으로 역할/채널/카테고리 설정을 확인하세요.
