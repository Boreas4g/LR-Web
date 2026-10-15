from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware

from .api.auth import router as auth_router
from .api.chassis_params import router as chassis_params_router
from .api.operations import router as operations_router
from .api.robots import open_router as robots_open_router
from .api.robots import router as robots_router
from .api.system import router as system_router
from .config import get_settings
from .dependencies import get_current_user
from .realtime import robot_realtime_hub
from .schemas import HealthResponse, StationIdentity, User
from .store import store

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    robot_realtime_hub.start()
    store.set_robot_update_listener(robot_realtime_hub.publish_robot)
    store.configure(settings)
    yield
    store.set_robot_update_listener(None)
    store.shutdown()
    store.flush_persist()
    robot_realtime_hub.stop()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(chassis_params_router, dependencies=[Depends(get_current_user)])
app.include_router(robots_router, dependencies=[Depends(get_current_user)])
# 免鉴权直控：jog 单独挂载，不经过登录依赖。
app.include_router(robots_open_router)
app.include_router(operations_router, dependencies=[Depends(get_current_user)])
app.include_router(system_router, dependencies=[Depends(get_current_user)])


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(
        status=store.health_status(),
        service=settings.app_name,
        environment=settings.environment,
        dependencies=store.dependency_status(),
        station=StationIdentity(id=settings.station_id, name=settings.station_name),
    )


@app.get("/api/v1/current-user", response_model=User, tags=["auth"])
def current_user(user: User = Depends(get_current_user)) -> User:
    return user


@app.websocket("/api/v1/realtime/robots")
async def robot_realtime(websocket: WebSocket) -> None:
    """Stream server-validated robot positions to an authenticated browser."""
    websocket_protocol = websocket.headers.get("sec-websocket-protocol", "")
    if settings.auth_enabled:
        token = websocket_protocol.removeprefix("lr-auth.")
        if not store.user_for_token(token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
    # Test clients and embedded deployments may not run the application lifespan.
    robot_realtime_hub.start()
    await websocket.accept(subprotocol=websocket_protocol)
    queue = robot_realtime_hub.subscribe()
    try:
        await websocket.send_json(
            {"type": "robots.snapshot", "robots": [robot.model_dump(mode="json") for robot in store.robots]}
        )
        while True:
            await websocket.send_json(await queue.get())
    except (RuntimeError, WebSocketDisconnect):
        # A telemetry frame can race with a browser closing its socket.
        pass
    finally:
        robot_realtime_hub.unsubscribe(queue)
