from setuptools import setup, find_packages
from os.path import exists

setup(
    name='phonos',
    version='1.0.0',
    packages=find_packages(),
    install_requires=[
        'requests>=2.25.0',
    ],
    python_requires='>=3.8',
    author='Phonos',
    description='Phonos SDK for Notebook IDE - Access AI models and submit jobs',
    long_description=open('README.md').read() if exists('README.md') else '',
)
