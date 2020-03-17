#!/usr/bin/env python
# -*- coding: utf-8 -*-

from setuptools import setup

with open('README.rst') as readme_file:
    readme = readme_file.read()

requirements = [
    'click',
    'python-digitalocean',
    'spur',
    'tabulate',
    'sphinx',
    'pyyaml',
    'gitpython',
    'python-openstackclient'
]

test_requirements = [
    # TODO: put package test requirements here
]

setup(
    name='cloud_deploy',
    version='0.2.0',
    description="A simple tool for deploying Docker containers in the cloud.",
    long_description=readme,
    author="Andrew P. Davison",
    author_email='andrew.davison@cnrs.fr',
    url='https://github.com/CNRSUNIC/cloud-deploy',
    packages=['cloud_deploy'],
    package_dir={'cloud_deploy': 'cloud_deploy'},
    entry_points={
        'console_scripts': [
            'cld=cloud_deploy.cli:cli'
        ]
    },
    include_package_data=True,
    install_requires=requirements,
    license="Apache Software License 2.0",
    zip_safe=False,
    keywords='cloud Docker',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: Apache Software License',
        'Natural Language :: English',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
    ],
    test_suite='tests',
    tests_require=test_requirements
)
