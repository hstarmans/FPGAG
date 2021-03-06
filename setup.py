from setuptools import find_packages, setup

setup(
    name='FPGAG',
    package_dir={'': 'src'},
    packages = find_packages(where='src'),
    version='0.0.1',
    description='G code parser implemented on a FPGAG.',
    author='Rik Starmans',
    license='GPLv3',
    project_urls={
        'Blog': 'https://hackaday.io/project/21933-open-hardware-fast-high-resolution-laser',
        'Main page': 'https://www.hexatorm.com',
        'Source': 'https://github.com/hstarmans/hexastorm',
    }
)