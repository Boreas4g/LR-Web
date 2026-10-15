from glob import glob
from setuptools import find_packages, setup


package_name = "robot_decision"

setup(
    name=package_name,
    version="0.0.1",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/config", glob("config/*.yaml")),
        (f"share/{package_name}/launch", glob("launch/*.launch.py")),
        (f"share/{package_name}/target", glob("target/*.jpg")),
    ],
    install_requires=["setuptools"],
    tests_require=["pytest"],
    zip_safe=True,
    maintainer="boreas",
    maintainer_email="boreas@example.com",
    description="Decision nodes for localization initialization and Nav2 waypoint cruising.",
    license="MIT",
    entry_points={
        "console_scripts": [
            "init_pose_node = robot_decision.init_pose_node:main",
            "cruise_node = robot_decision.cruise_node:main",
            "edge_gateway_node = robot_decision.edge_gateway_node:main",
            "book_shelf_fine_tune_node = robot_decision.book_shelf_fine_tune_node:main",
            "bookarm_sdk_service = robot_decision.bookarm_sdk_service:main",
            "auto_localization_node = robot_decision.auto_localization_node:main",
            "point3_fine_tune_node = robot_decision.point3_fine_tune_node:main",
            "book_center_detector_node = robot_decision.book_center_detector_node:main",
            "vehicle_nudge_node = robot_decision.vehicle_nudge_node:main",
        ],
    },
)
