from glob import glob
from setuptools import find_packages, setup


package_name = "mobile_robot_voice_interaction"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    py_modules=["Speech_Lib"],
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}", ["README.md"]),
        (f"share/{package_name}/launch", glob("launch/*.launch.py")),
        (f"share/{package_name}/config", glob("config/*.yaml")),
    ],
    install_requires=["setuptools", "pyserial"],
    zip_safe=True,
    maintainer="baotianyi",
    maintainer_email="baotianyi@example.com",
    description="ROS 2 speech-module package for command reading and interaction bridging.",
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "voice_cmd_reader = mobile_robot_voice_interaction.voice_cmd_reader:main",
            "speech_interaction = mobile_robot_voice_interaction.speech_interaction:main",
        ],
    },
)
