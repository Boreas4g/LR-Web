from robot_decision.bookarm_sdk_client import BookArmSdkClient


def test_sdk_http_client_exposes_takeout_and_putback_capabilities() -> None:
    client = BookArmSdkClient("http://127.0.0.1:18080/api/v1", 1.0)

    capabilities = client.capabilities()

    assert capabilities["bookWorkflowModes"] == ["takeout", "putback"]
    assert capabilities["softwareStop"] is False
    assert capabilities["liftAbsolutePositionFeedback"] is True
    assert capabilities["liftHomeSensor"] is False


def test_sdk_http_client_maps_health_and_forwards_book_context(monkeypatch) -> None:
    client = BookArmSdkClient("http://127.0.0.1:18080/api/v1", 1.0)
    requests = []

    def request(method, path, payload=None):
        requests.append((method, path, payload))
        if path == "/health":
            return {
                "available": True,
                "armReady": False,
                "liftReady": True,
                "cameraReady": True,
                "rodReady": True,
                "message": "ready",
                "armConnected": True,
                "armEnabled": False,
                "armState": "standby",
                "liftAbsolutePositioningReady": True,
            }
        return {"workflow": {"id": "BA-1", "active": True, "waiting": True}}

    monkeypatch.setattr(client, "_request", request)
    health = client.health()
    workflow = client.start_take_book({"taskId": "TSK-1", "bookId": "CPY-1", "targetHeightMm": 620})

    assert health.available is True
    assert health.arm_connected is True
    assert health.arm_enabled is False
    assert health.arm_state == "standby"
    assert health.lift_absolute_positioning_ready is True
    assert workflow["id"] == "BA-1"
    assert requests[-1] == (
        "POST",
        "/take-book",
        {"taskId": "TSK-1", "bookId": "CPY-1", "targetHeightMm": 620},
    )


def test_sdk_http_client_forwards_return_book_to_dedicated_endpoint(monkeypatch) -> None:
    client = BookArmSdkClient("http://127.0.0.1:18080/api/v1", 1.0)
    requests = []
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, path, payload=None: requests.append((method, path, payload))
        or {"workflow": {"id": "BA-RETURN-1", "active": True}},
    )

    workflow = client.start_return_book(
        {
            "taskId": "TSK-RETURN-1",
            "bookId": "CPY-1",
            "targetHeightMm": 620,
            "leftNeighborSpineTemplatePath": "/private/left.jpg",
            "sourceSlot": 1,
        }
    )

    assert workflow["id"] == "BA-RETURN-1"
    assert requests == [
        (
            "POST",
            "/return-book",
            {
                "taskId": "TSK-RETURN-1",
                "bookId": "CPY-1",
                "targetHeightMm": 620,
                "leftNeighborSpineTemplatePath": "/private/left.jpg",
                "sourceSlot": 1,
            },
        )
    ]
