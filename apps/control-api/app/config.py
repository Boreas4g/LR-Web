from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

def _default_cruise_points_file() -> str:
    """Locate an ops-deployed route snapshot, with a Docker-safe fallback.

    The repository only ships ``infra/cruise-points.example.yaml`` (a dev-lab
    sample); it is deliberately NOT picked up here. A real deployment copies
    the approved snapshot next to it as ``infra/cruise-points.yaml``, or mounts
    one at the container path below, and opts in via LR_CRUISE_POINTS_ENABLED.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "infra" / "cruise-points.yaml"
        if candidate.is_file():
            return str(candidate)
    return "/app/config/cruise-points.yaml"


DEFAULT_CRUISE_POINTS_FILE = _default_cruise_points_file()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="LR_", extra="ignore")

    app_name: str = "LR Control API"
    environment: str = "development"
    station_name: str = "LR 控制站"
    # An appliance installer generates a stable ID once. The development
    # default deliberately makes local test instances distinguishable.
    station_id: str = "development-station"
    station_url: str = ""
    host: str = "0.0.0.0"
    port: int = 8080
    cors_origins: str = "http://localhost:8000"
    command_ttl_seconds: int = 30
    cruise_command_ttl_seconds: int = 14400
    control_lock_seconds: int = 45
    # Debounce window for full database write-through; 0 persists synchronously.
    persist_delay_seconds: float = 0.5
    map_storage_dir: str = "data/maps"
    # 单机交付时控制站与车端同机：导入地图后顺带把地图注册进车端地图库
    # （/var/lib/lr-robot/maps），车端才允许切换到该图。分离部署时关闭。
    vehicle_map_register_enabled: bool = True
    # On-robot mapping agent (SLAM orchestration) reachable from the station host.
    robot_agent_url: str = "http://127.0.0.1:18090"
    # The control center reads an approved route snapshot for map visualization.
    cruise_points_file: str = DEFAULT_CRUISE_POINTS_FILE
    # 巡航点注入总开关：默认关闭，任何地图导入都不会被注入开发样例点。
    # 仅在控制站持有已批准巡航点副本（cruise_points_file 显式存在）时开启。
    cruise_points_enabled: bool = False
    storage_backend: Literal["auto", "memory", "postgres"] = "auto"
    database_url: str | None = None
    redis_url: str | None = None
    mqtt_url: str | None = None
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_tls_enabled: bool = False
    mqtt_ca_file: str | None = None
    mqtt_client_cert_file: str | None = None
    mqtt_client_key_file: str | None = None
    require_mqtt: bool = False
    # Single-operator debug mode: role gates on manual operations are
    # bypassed. Never enable on shared deployments.
    single_operator: bool = False
    # Delivery default: no login wall. Every request runs as a synthetic
    # operator identity (role 系统管理员), so all role gates pass. Set
    # LR_AUTH_ENABLED=true to restore Bearer-token login on shared deployments.
    auth_enabled: bool = False
    # A containerised API must never attempt to manipulate host systemd, CAN
    # or serial services.  A host-local development API can opt in explicitly.
    host_start_all_script: str | None = None
    host_restart_all_script: str | None = None
    # Production uses a deliberately fixed-command host agent over this private
    # Unix socket.  It is not a general host shell or a Docker socket.
    host_start_agent_socket: str | None = None
    # 机械臂硬件启动确认（运行总览页开关）的持久化文件（纯文本 true/false）；
    # start-all.sh 在该文件可读时优先于宿主代理单元里的静态 LR_BOOKARM_ALLOW_START。
    bookarm_start_policy_file: str = "/etc/lr-platform/bookarm-start-policy.conf"
    # On-robot bookarm service (takeout/return workflows) over a private Unix
    # socket; the token matches the daemon's --token when it sets one. Empty
    # means no Authorization header is sent (env LR_BOOKARM_TOKEN).
    bookarm_socket_path: str = "/run/lr-bookarm/bookarm.sock"
    bookarm_token: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def use_postgres(self) -> bool:
        return self.storage_backend == "postgres" or (self.storage_backend == "auto" and bool(self.database_url))

    @property
    def sqlalchemy_database_url(self) -> str | None:
        if not self.database_url:
            return None
        if self.database_url.startswith("postgresql://"):
            return self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
