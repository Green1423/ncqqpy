from setuptools import setup, find_packages

setup(
    name="ncqqpy",
    version="1.0.0",
    description="ncqqpy — NapCatQQ WebSocket 协议的 Python 机器人框架",
    author="ncqqpy",
    packages=find_packages(exclude=["plugins"]),
    install_requires=[
        "websockets>=13.0",
        "pydantic>=2.0",
        "aiohttp>=3.9",
        "aiofiles>=24.0",
    ],
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Framework :: AsyncIO",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    ],
)
