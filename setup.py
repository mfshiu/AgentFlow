from setuptools import setup, find_packages

setup(
    name="mas-agentflow",  
    version="2025.5.31.1932",
    packages=find_packages(where="src"),  # Tell setuptools to look for packages in 'src'
    package_dir={"": "src"},  # Set 'src' as the root directory for the package
    include_package_data=True,  # If you have any data files (e.g., non-Python files)
    install_requires=[
        # List your dependencies here, for example:
        # "numpy>=1.21",
    ],
    entry_points={
        # If you have console scripts to expose as CLI, you can define them here.
        # Example:
        # 'console_scripts': [
        #     'agentflow-cli=agentflow.cli:main',
        # ],
    },
    python_requires='>=3.11',  # Specify the Python version you require
)
