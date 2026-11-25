"""Setup configuration for phonos-sdk."""
from setuptools import setup, find_packages
import os

# Read the README file
def read_file(filename):
    filepath = os.path.join(os.path.dirname(__file__), filename)
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    return ''

setup(
    name='phonos-sdk',
    version='1.0.0',
    description='Python SDK for the Phonos computational biology platform',
    long_description=read_file('README.md'),
    long_description_content_type='text/markdown',
    author='FyreFlySystems',
    author_email='support@fyreflysystems.de',
    url='https://github.com/FyreFly-Systems/phonos-sdk-python',
    project_urls={
        'Documentation': 'https://phonos.fyreflysystems.de/docs',
        'Source': 'https://github.com/FyreFly-Systems/phonos-sdk-python',
        'Tracker': 'https://github.com/FyreFly-Systems/phonos-sdk-python/issues',
    },
    packages=find_packages(exclude=['tests', 'tests.*']),
    install_requires=[
        'requests>=2.25.0',
    ],
    extras_require={
        'dev': [
            'pytest>=7.0.0',
            'pytest-cov>=3.0.0',
            'black>=22.0.0',
            'flake8>=4.0.0',
            'mypy>=0.950',
        ],
    },
    python_requires='>=3.7',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Intended Audience :: Science/Research',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Topic :: Scientific/Engineering :: Bio-Informatics',
        'Topic :: Software Development :: Libraries :: Python Modules',
    ],
    keywords='phonos biology bioinformatics protein-folding computational-biology api-client sdk',
    license='MIT',
    include_package_data=True,
    zip_safe=False,
)
