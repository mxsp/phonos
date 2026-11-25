from setuptools import setup, find_packages

setup(
    name='phonos_iot',
    version='1.0.0',
    packages=find_packages(),
    install_requires=['requests>=2.25.0'],
    python_requires='>=3.8',
    description='Phonos IoT SDK for Jupyter Notebooks',
    author='Phonos',
    author_email='support@phonos.io',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Science/Research',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
    ],
)
