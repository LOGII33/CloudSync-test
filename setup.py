from setuptools import setup, find_packages
setup(
    name="cloudsync", version="0.1.0",
    package_dir={"": "src"}, packages=find_packages(where="src"),
    python_requires=">=3.10",
    install_requires=["click>=8.0", "pyyaml>=6.0"],
    extras_require={"dev": ["pytest>=7.0", "pytest-cov"]},
    entry_points={"console_scripts": ["cloudsync=cloudsync.cli:main"]},
)
