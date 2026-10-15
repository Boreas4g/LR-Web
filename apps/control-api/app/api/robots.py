import json
from pathlib import Path
from typing import Any, TypeVar
from urllib import error as urllib_error
from urllib import request as urllib_request

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ValidationError

from ..config import get_settings
from ..dependencies import get_current_user
from ..schemas import (
    CreateMapAnnotationRequest,
    CreateRobotRequest,
    DispatchTask,
    ManualJogRequest,
    MapImportRequest,
    MappingActionResponse,
    MappingJogRequest,
    MappingMarkRequest,
    MappingMarkResponse,
    MappingMarksClearResponse,
    MappingMarksResponse,
    MappingPoseResponse,
    MappingStatusResponse,
    MapVersion,
    MoveLiftRequest,
    ResetEmergencyStopRequest,
    RobotCommand,
    RobotCommandRequest,
    RobotSummary,
    SaveMappingRequest,
    SaveMappingResponse,
    SetInitialPoseRequest,
    SwitchMapRequest,
    User,
)
from ..friendly_errors import describe_map_operation_failure
from ..store import store

router = APIRouter(prefix="/api/v1", tags=["robots"])
# 免鉴权直控路由：main.py 挂载时不附带登录依赖（局域网单人调试，用户明确要求）。
open_router = APIRouter(prefix="/api/v1", tags=["robots"])
settings = get_settings()

# 开始/结束建图要停起整套导航栈与 SLAM，车端可能耗时数十秒；保存地图还要
# 等 SLAM 落盘与序列化，大场景会更久。超时仅说明代理未及时应答，操作本身
# 可能仍在车端继续，因此单独返回 504 文案与 502 不可达区分。
MAPPING_AGENT_TIMEOUT_SECONDS = 45
MAPPING_AGENT_SAVE_TIMEOUT_SECONDS = 300
MAPPING_AGENT_TIMEOUT_DETAIL = "切换/保存仍在进行，请稍后刷新状态确认结果"
# One click = one direct result: the switch endpoint waits for the vehicle's
# switched/failed/rejected event up to this budget before answering.
MAP_SWITCH_RESULT_TIMEOUT_SECONDS = 40.0


class MapSwitchResultView(BaseModel):
    """Synchronous outcome of one direct map-switch click."""

    ok: bool
    mapId: str
    commandId: str
    message: str


class AnnotationNavigateRequest(BaseModel):
    """Start an orchestrated navigate → low-speed fine-tune run to an annotation."""

    mapId: str
    annotationId: str
    # 只需粗到位的场景可跳过低速精调：导航成功即算完成。
    skipFineTune: bool = False


@router.post(
    "/robots/{robot_id}/annotations/navigate",
    response_model=DispatchTask,
    status_code=status.HTTP_202_ACCEPTED,
)
def navigate_to_annotation(
    robot_id: str, request: AnnotationNavigateRequest, user: User = Depends(get_current_user)
) -> DispatchTask:
    """Annotation navigation with the takeout-style low-speed fine-tune stage.

    The created parent task exposes realtime progress (workflowMessage) and
    the per-stage command task rows, so the map page can show live feedback.
    """
    if not any(item.id == robot_id for item in store.robots):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机器人不存在")
    try:
        return store.start_annotation_navigation(robot_id, request.mapId, request.annotationId, user, skip_fine_tune=request.skipFineTune)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post(
    "/robots/{robot_id}/annotations/navigate/{task_id}/cancel",
    response_model=DispatchTask,
)
def cancel_annotation_navigation(
    robot_id: str, task_id: str, user: User = Depends(get_current_user)
) -> DispatchTask:
    """Cancel a running annotation navigation flow (stops the vehicle stage)."""
    if not any(item.id == robot_id for item in store.robots):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机器人不存在")
    try:
        return store.cancel_annotation_navigation(task_id, user)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


def _require_robot(robot_id: str) -> None:
    if not any(item.id == robot_id for item in store.robots):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机器人不存在")


def _is_timeout_error(error: object) -> bool:
    # socket.timeout is an alias of TimeoutError on Python 3.10+; the string
    # check additionally catches wrapped "timed out" reasons.
    return isinstance(error, TimeoutError) or "timed out" in str(error).lower()


def _mapping_agent_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = MAPPING_AGENT_TIMEOUT_SECONDS,
) -> tuple[int, bytes]:
    """Forward one call to the on-robot mapping agent without extra dependencies."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib_request.Request(
        f"{settings.robot_agent_url.rstrip('/')}{path}", data=body, headers=headers, method=method
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib_error.HTTPError as http_error:
        return http_error.code, http_error.read()
    except urllib_error.URLError as url_error:
        if _is_timeout_error(url_error.reason):
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=MAPPING_AGENT_TIMEOUT_DETAIL
            ) from url_error
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="建图代理不可达") from url_error
    except OSError as connect_error:
        if _is_timeout_error(connect_error):
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=MAPPING_AGENT_TIMEOUT_DETAIL
            ) from connect_error
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="建图代理不可达") from connect_error


def _agent_error_detail(body: bytes, fallback: str) -> str:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return fallback
    if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
        return parsed["detail"]
    return fallback


def _mapping_agent_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = MAPPING_AGENT_TIMEOUT_SECONDS,
) -> Any:
    status_code, body = _mapping_agent_request(method, path, payload, timeout)
    if status_code >= 400:
        detail = _agent_error_detail(body, "建图代理请求失败")
        if status_code == status.HTTP_409_CONFLICT:
            detail = describe_map_operation_failure(detail)
        raise HTTPException(status_code=status_code, detail=detail)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as parse_error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="建图代理返回了无法解析的响应"
        ) from parse_error


ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


def _validate_agent_payload(payload: Any, model: type[ResponseModel]) -> ResponseModel:
    try:
        return model.model_validate(payload)
    except ValidationError as validation_error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="建图代理返回了无法解析的响应"
        ) from validation_error


@router.get("/robots", response_model=list[RobotSummary])
def list_robots(_: User = Depends(get_current_user)) -> list[RobotSummary]:
    return store.list_robots()


@router.post("/robots", response_model=RobotSummary, status_code=status.HTTP_201_CREATED)
def create_robot(request: CreateRobotRequest, user: User = Depends(get_current_user)) -> RobotSummary:
    try:
        return store.create_robot(request.id, request.name, request.gatewayHost, user)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.delete("/robots/{robot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_robot(robot_id: str, user: User = Depends(get_current_user)) -> None:
    try:
        store.delete_robot(robot_id, user)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.post("/robots/{robot_id}/commands", response_model=RobotCommand, status_code=status.HTTP_202_ACCEPTED)
def create_command(robot_id: str, request: RobotCommandRequest, user: User = Depends(get_current_user)) -> RobotCommand:
    if request.command in {
        "cruise",
        "start_manual_control",
        "manual_jog",
        "stop_manual_control",
        "book_task",
        "book_task_continue",
        "book_task_cancel",
        "bookarm_test_start",
        "bookarm_test_continue",
        "bookarm_test_cancel",
        "lift_move",
        "switch_map",
    }:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请使用对应的受控操作接口")
    robot = next((item for item in store.robots if item.id == robot_id), None)
    if not robot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机器人不存在")
    try:
        return store.create_command(robot, request, user, settings.command_ttl_seconds)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/robots/{robot_id}/emergency-stop/reset", response_model=RobotSummary)
def reset_software_emergency_stop(
    robot_id: str, request: ResetEmergencyStopRequest, user: User = Depends(get_current_user)
) -> RobotSummary:
    try:
        return store.reset_software_emergency_stop(robot_id, request, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@open_router.post(
    "/robots/{robot_id}/manual-control/jog", response_model=RobotCommand, status_code=status.HTTP_202_ACCEPTED
)
def send_manual_jog(robot_id: str, request: ManualJogRequest) -> RobotCommand:
    # 免鉴权直控：局域网单人调试，点动指令不附带租约、不做用户门控。
    if not any(item.id == robot_id for item in store.robots):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机器人不存在")
    try:
        return store.send_manual_jog(robot_id, request)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/robots/{robot_id}/lift/move", response_model=RobotCommand, status_code=status.HTTP_202_ACCEPTED)
def move_lift(robot_id: str, request: MoveLiftRequest, user: User = Depends(get_current_user)) -> RobotCommand:
    try:
        return store.move_lift(robot_id, request, user)
    except PermissionError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post(
    "/robots/{robot_id}/localization/initial-pose",
    response_model=RobotCommand,
    status_code=status.HTTP_202_ACCEPTED,
)
def set_initial_pose(
    robot_id: str, request: SetInitialPoseRequest, user: User = Depends(get_current_user)
) -> RobotCommand:
    try:
        return store.set_robot_initial_pose(robot_id, request, user, settings.command_ttl_seconds)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post(
    "/robots/{robot_id}/localization/auto-localize",
    response_model=RobotCommand,
    status_code=status.HTTP_202_ACCEPTED,
)
def auto_localize(robot_id: str, user: User = Depends(get_current_user)) -> RobotCommand:
    try:
        return store.auto_localize_robot(robot_id, user, settings.command_ttl_seconds)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/robots/{robot_id}/map/switch", response_model=MapSwitchResultView)
def switch_robot_map(robot_id: str, request: SwitchMapRequest, user: User = Depends(get_current_user)) -> MapSwitchResultView:
    """Direct per-click map switch: dispatch and report the vehicle's outcome.

    The transport stays the reviewed MQTT command, but the operator no longer
    gets a fire-and-forget queue ticket: the endpoint waits for the vehicle's
    switched/failed/rejected event and returns one synchronous result.
    """
    robot = next((item for item in store.robots if item.id == robot_id), None)
    if robot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="机器人不存在")
    if not any(item.id == request.mapId for item in store.map_versions):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="地图不存在")
    if robot.mapId == request.mapId:
        # Already on the target: the desired end state holds, so a click is an
        # idempotent success instead of a dispatch the vehicle would reject.
        return MapSwitchResultView(
            ok=True,
            mapId=request.mapId,
            commandId="",
            message="机器人已在该地图上",
        )
    try:
        command = store.switch_robot_map(robot_id, request, user)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    outcome, reason = store.wait_map_switch_result(
        robot_id, request.mapId, command.commandId, MAP_SWITCH_RESULT_TIMEOUT_SECONDS
    )
    if outcome == "succeeded":
        return MapSwitchResultView(
            ok=True,
            mapId=request.mapId,
            commandId=command.commandId,
            message="地图已切换，机器人正在重新定位",
        )
    detail = describe_map_operation_failure(reason) if reason else "地图切换失败"
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


@router.get("/robots/{robot_id}/mapping/status", response_model=MappingStatusResponse)
def get_mapping_status(robot_id: str, _: User = Depends(get_current_user)) -> MappingStatusResponse:
    _require_robot(robot_id)
    return _validate_agent_payload(_mapping_agent_json("GET", "/mapping/status"), MappingStatusResponse)


@router.post("/robots/{robot_id}/mapping/start", response_model=MappingActionResponse)
def start_mapping(robot_id: str, _: User = Depends(get_current_user)) -> MappingActionResponse:
    _require_robot(robot_id)
    return _validate_agent_payload(_mapping_agent_json("POST", "/mapping/start", {}), MappingActionResponse)


@router.post("/robots/{robot_id}/mapping/stop", response_model=MappingActionResponse)
def stop_mapping(robot_id: str, _: User = Depends(get_current_user)) -> MappingActionResponse:
    _require_robot(robot_id)
    return _validate_agent_payload(_mapping_agent_json("POST", "/mapping/stop", {}), MappingActionResponse)


@router.post("/robots/{robot_id}/mapping/jog")
def jog_mapping(robot_id: str, request: MappingJogRequest, _: User = Depends(get_current_user)) -> Any:
    # The agent only guarantees a 200/409 status for jog, so forward its body verbatim.
    _require_robot(robot_id)
    return _mapping_agent_json("POST", "/mapping/jog", {"linear": request.linear, "angular": request.angular})


@router.get("/robots/{robot_id}/mapping/snapshot.png")
def get_mapping_snapshot(robot_id: str, _: User = Depends(get_current_user)) -> Response:
    _require_robot(robot_id)
    status_code, body = _mapping_agent_request("GET", "/mapping/snapshot.png")
    if status_code >= 400:
        raise HTTPException(status_code=status_code, detail=_agent_error_detail(body, "建图快照尚未生成"))
    return Response(content=body, media_type="image/png")


@router.get("/robots/{robot_id}/mapping/pose", response_model=MappingPoseResponse)
def get_mapping_pose(robot_id: str, _: User = Depends(get_current_user)) -> MappingPoseResponse:
    _require_robot(robot_id)
    return _validate_agent_payload(_mapping_agent_json("GET", "/mapping/pose"), MappingPoseResponse)


@router.get("/robots/{robot_id}/mapping/loc-overlay")
def get_mapping_loc_overlay(robot_id: str, _: User = Depends(get_current_user)) -> Response:
    # 点云已由车端投影到 map 坐标系；成功与失败（503）响应都原样透传给前端叠图。
    _require_robot(robot_id)
    status_code, body = _mapping_agent_request("GET", "/mapping/loc-overlay")
    return Response(content=body, status_code=status_code, media_type="application/json")


@router.get("/robots/{robot_id}/mapping/marks", response_model=MappingMarksResponse)
def list_mapping_marks(robot_id: str, _: User = Depends(get_current_user)) -> MappingMarksResponse:
    _require_robot(robot_id)
    return _validate_agent_payload(_mapping_agent_json("GET", "/mapping/marks"), MappingMarksResponse)


@router.post("/robots/{robot_id}/mapping/mark", response_model=MappingMarkResponse, status_code=status.HTTP_201_CREATED)
def create_mapping_mark(
    robot_id: str, request: MappingMarkRequest, _: User = Depends(get_current_user)
) -> MappingMarkResponse:
    _require_robot(robot_id)
    return _validate_agent_payload(
        _mapping_agent_json("POST", "/mapping/mark", {"label": request.label}), MappingMarkResponse
    )


@router.delete("/robots/{robot_id}/mapping/marks/{index}")
def delete_mapping_mark(robot_id: str, index: int, _: User = Depends(get_current_user)) -> Any:
    """Delete one shelf mark by list index (vehicle contract: 404 when out of range)."""
    _require_robot(robot_id)
    return _mapping_agent_json("DELETE", f"/mapping/marks/{index}")


@router.post("/robots/{robot_id}/mapping/marks/clear", response_model=MappingMarksClearResponse)
def clear_mapping_marks(robot_id: str, _: User = Depends(get_current_user)) -> MappingMarksClearResponse:
    _require_robot(robot_id)
    return _validate_agent_payload(_mapping_agent_json("POST", "/mapping/marks/clear", {}), MappingMarksClearResponse)


@router.post("/robots/{robot_id}/mapping/save", response_model=SaveMappingResponse)
def save_mapping(robot_id: str, request: SaveMappingRequest, user: User = Depends(get_current_user)) -> SaveMappingResponse:
    _require_robot(robot_id)
    payload = _mapping_agent_json("POST", "/mapping/save", {}, timeout=MAPPING_AGENT_SAVE_TIMEOUT_SECONDS)
    if not isinstance(payload, dict) or not all(
        isinstance(payload.get(key), str) for key in ("mapId", "yamlB64", "pgmB64")
    ):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="建图代理返回的保存结果不完整")
    map_id = payload["mapId"]
    # 车端保存幂等：同一次建图会话重复保存返回首存 mapId 且 alreadySaved 为真。
    # 本地已有该图时直接返回首存记录，避免重复的"地图 ID 已存在" 409。
    already_saved = payload.get("alreadySaved") is True
    if already_saved:
        existing = next((item for item in store.map_versions if item.id == map_id), None)
        if existing is not None:
            return SaveMappingResponse(**existing.model_dump(), alreadySaved=True)
    yaml_path = payload.get("yamlPath") if isinstance(payload.get("yamlPath"), str) else f"{map_id}.yaml"
    pgm_path = payload.get("pgmPath") if isinstance(payload.get("pgmPath"), str) else f"{map_id}.pgm"
    try:
        import_request = MapImportRequest(
            id=map_id,
            name=(request.name or "").strip() or f"手动建图 {map_id}",
            yamlName=Path(yaml_path).name,
            yamlContent=payload["yamlB64"],
            pgmName=Path(pgm_path).name,
            pgmContent=payload["pgmB64"],
        )
    except ValidationError as validation_error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="建图代理返回的地图数据不符合入库格式"
        ) from validation_error
    try:
        map_version = store.import_map(import_request, user)
    except ValueError as import_error:
        message = str(import_error)
        if "已存在" in message:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{message}；车端建图文件保留在 {yaml_path} 与 {pgm_path}，可改名后重新保存。",
            ) from import_error
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message) from import_error
    imported_marks, skipped_marks = _import_mapping_marks(map_version, payload.get("marks"), user)
    return SaveMappingResponse(
        **map_version.model_dump(),
        importedMarks=imported_marks,
        skippedMarks=skipped_marks,
        alreadySaved=already_saved,
    )


def _import_mapping_marks(map_version: MapVersion, marks: Any, user: User) -> tuple[int, int]:
    """Best-effort import of on-robot shelf marks as annotations; never breaks the map save.

    Returns (imported, skipped): 越界与损坏的标注计入 skipped，不阻断入库。
    """
    imported = 0
    skipped = 0
    if not isinstance(marks, list):
        return (imported, skipped)
    for index, mark in enumerate(marks, start=1):
        if not isinstance(mark, dict):
            skipped += 1
            continue
        try:
            x = float(mark["x"])
            y = float(mark["y"])
            yaw = float(mark.get("yaw", 0.0))
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        # Inverse of store.create_map_annotation's pixel-to-map conversion.
        pixel_x = round((x - map_version.origin.x) / map_version.resolution)
        pixel_y = round(map_version.height - 1 - (y - map_version.origin.y) / map_version.resolution)
        if not (0 <= pixel_x < map_version.width and 0 <= pixel_y < map_version.height):
            skipped += 1
            continue
        raw_label = mark.get("label")
        label = (raw_label.strip() if isinstance(raw_label, str) else "") or f"书架点 {index}"
        try:
            store.create_map_annotation(
                map_version.id,
                CreateMapAnnotationRequest(
                    kind="shelf",
                    label=label,
                    pixelX=pixel_x,
                    pixelY=pixel_y,
                    yaw=max(-6.284, min(6.284, yaw)),
                ),
                user,
            )
        except ValueError:
            skipped += 1
            continue
        imported += 1
    return (imported, skipped)
