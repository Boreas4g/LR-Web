from glob import glob
from setuptools import find_packages, setup


package_name = "car_ctrl"

setup(
    name=package_name,
    version="0.0.1",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/config", glob("config/*.yaml")),
        (f"share/{package_name}/launch", glob("launch/*.launch.py")),
        (f"share/{package_name}/rviz", glob("rviz/*.rviz")),
    ],
    install_requires=["setuptools"],
    tests_require=["pytest"],
    zip_safe=True,
    maintainer="boreas",
    maintainer_email="boreas@example.com",
    description="Differential drive controller for DDSM115 motors via Waveshare DDSM Driver HAT (A).",
    license="MIT",
    entry_points={
        "console_scripts": [
            "ddsm_hat_diff_drive_node = car_ctrl.ddsm_hat_diff_drive_node:main",
            "ddsm_hat_motor_test = car_ctrl.ddsm_hat_motor_test_node:main",
            "imu_driver = car_ctrl.imu_driver:main",
            "car_odometry = car_ctrl.car_odometry:main",
            "scan_odometry = car_ctrl.scan_odometry:main",
            "map_pose_recorder = car_ctrl.map_pose_recorder:main",
        ],
    },
)
