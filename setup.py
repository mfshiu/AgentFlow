from setuptools import setup, find_packages

setup(
    name="mas-agentflow",  
    version="2025.8.22.1933",
    packages=find_packages(where="src"),  # Tell setuptools to look for packages in 'src'
    package_dir={"": "src"},  # Set 'src' as the root directory for the package
    include_package_data=True,  # If you have any data files (e.g., non-Python files)
    install_requires=[
        # mqtt_broker uses paho's CallbackAPIVersion, added in paho-mqtt 2.0.
        "paho-mqtt>=2.0,<3",
    ],
    entry_points={
        # If you have console scripts to expose as CLI, you can define them here.
        # Example:
        # 'console_scripts': [
        #     'agentflow-cli=agentflow.cli:main',
        # ],
    },
    # Issue #77: the floor is the lowest Python the CI matrix tests
    # (.github/workflows/ci.yml), and the classifiers name the versions it
    # tests; tests/unit/test_packaging_metadata.py keeps them in step.
    python_requires='>=3.11',
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.13",
    ],
)
