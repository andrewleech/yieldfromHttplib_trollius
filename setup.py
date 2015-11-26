from setuptools import setup, find_packages

setup(
    packages = ['yieldfrom_t', 'yieldfrom_t.http'], #find_packages(), #['http', 'yieldfrom_t.http'],
    package_dir = {'yieldfrom_t': 'yieldfrom_t'},
    version = '0.1.2',
    namespace_packages = ['yieldfrom_t'],
    name = 'yieldfrom_t.http.client',
    description = 'asyncio version of http.client',
    install_requires = ['setuptools',],

    author = 'David Keeney',
    author_email = 'dkeeney@rdbhost.com',
    license = 'Python Software Foundation License',

    keywords = 'asyncio, http, http.client',
    url = 'http://github.com/rdbhost/',
    zip_safe=False,
    )