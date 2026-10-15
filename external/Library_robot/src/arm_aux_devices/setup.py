from glob import glob
from setuptools import find_packages, setup


package_name = "arm_aux_devices"


setup(
    name=package_name,
    version="0.0.1",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml", "README.md"]),
        (f"share/{package_name}/config", glob("config/*.yaml")),
        (f"share/{package_name}/launch", glob("launch/*.launch.py")),
    ],
    install_requires=["setuptools", "pyserial"],
    zip_safe=True,
    maintainer="boreas",
    maintainer_email="boreas@example.com",
    description="ROS 2 auxiliary arm device drivers.",
    license="MIT",
    entry_points={
        "console_scripts": [
            "Turntable = arm_aux_devices.turntable:main",
            "lifttable = arm_aux_devices.lifttable:main",
        ],
    },
)
