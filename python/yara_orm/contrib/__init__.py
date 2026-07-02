"""Optional integrations between yara-orm and third-party libraries.

Each submodule guards its own third-party imports, so importing
``yara_orm.contrib`` itself never requires any optional dependency:

- :mod:`yara_orm.contrib.factory` — `factory_boy`_ integration
  (``pip install "yara-orm[factory]"``).

.. _factory_boy: https://factoryboy.readthedocs.io/
"""
