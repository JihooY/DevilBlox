from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote_plus

import motor.motor_asyncio

log = logging.getLogger(__name__)


@dataclass(slots=True)
class MongoConfig:
    uri: str | None
    db_name: str
    host: str
    port: int
    user: str | None
    password: str | None
    auth_db: str
    use_ssh: bool
    ssh_host: str | None
    ssh_port: int
    ssh_user: str | None
    ssh_key: str | None


class MongoRuntime:
    def __init__(self, config: MongoConfig):
        self.config = config
        self.client: motor.motor_asyncio.AsyncIOMotorClient | None = None
        self.tunnel = None

    def _build_uri(self, host: str, port: int) -> str:
        if self.config.user and self.config.password:
            user = quote_plus(self.config.user)
            password = quote_plus(self.config.password)
            auth_db = quote_plus(self.config.auth_db)
            return f"mongodb://{user}:{password}@{host}:{port}/?authSource={auth_db}"
        return f"mongodb://{host}:{port}"

    def _start_tunnel(self) -> tuple[str, int]:
        if not self.config.ssh_host:
            raise RuntimeError("MONGO_USE_SSH=true but SSH_HOST is empty.")

        try:
            from sshtunnel import SSHTunnelForwarder
        except ImportError as exc:
            raise RuntimeError("sshtunnel is required when MONGO_USE_SSH=true.") from exc

        self.tunnel = SSHTunnelForwarder(
            (self.config.ssh_host, self.config.ssh_port),
            ssh_username=self.config.ssh_user,
            ssh_pkey=self.config.ssh_key,
            remote_bind_address=(self.config.host, self.config.port),
        )
        self.tunnel.start()
        log.info("MongoDB SSH tunnel opened on 127.0.0.1:%s", self.tunnel.local_bind_port)
        return "127.0.0.1", self.tunnel.local_bind_port

    async def connect(self):
        if self.config.uri:
            uri = self.config.uri
        else:
            host, port = self.config.host, self.config.port
            if self.config.use_ssh:
                host, port = self._start_tunnel()
            uri = self._build_uri(host, port)

        self.client = motor.motor_asyncio.AsyncIOMotorClient(
            uri,
            serverSelectionTimeoutMS=8000,
            uuidRepresentation="standard",
        )
        try:
            await self.client.admin.command("ping")
        except Exception:
            await self.close()
            raise
        log.info("MongoDB connected: database=%s", self.config.db_name)
        return self.client[self.config.db_name]

    async def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.tunnel is not None:
            self.tunnel.stop()
            self.tunnel = None
            log.info("MongoDB SSH tunnel closed")
