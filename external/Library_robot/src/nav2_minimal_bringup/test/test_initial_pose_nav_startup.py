import importlib.util
from pathlib import Path
from types import SimpleNamespace

from std_msgs.msg import String


spec = importlib.util.spec_from_file_location(
    "initial_pose_nav_startup",
    Path(__file__).parents[1] / "scripts" / "initial_pose_nav_startup.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_navigation_waits_for_localization_and_does_not_reseed_amcl():
    node = module.InitialPoseNavStartup.__new__(module.InitialPoseNavStartup)
    node.localized = False
    node.startup_requested = False
    node.startup_complete = False
    requests = []
    node.navigation_client = SimpleNamespace(
        wait_for_service=lambda **_kwargs: True,
        call_async=lambda request: requests.append(request)
        or SimpleNamespace(add_done_callback=lambda _callback: None),
    )

    node._localization_status_callback(String(data="manual_initializing"))
    node._request_navigation_startup()
    assert requests == []
    node._localization_status_callback(String(data="localized"))
    node._localization_status_callback(String(data="localized"))
    assert len(requests) == 1
    assert requests[0].command == module.ManageLifecycleNodes.Request.STARTUP
