from setuptools import find_packages, setup

setup(
    name="saeka",
    version="25.0",
    packages=find_packages(),
    include_package_data=True,
    package_data={"saeka": ["setup.sh"]},
    entry_points={"console_scripts": ["saeka=saeka.qshell:main"]},
)
