"""Production package initialization."""

# Ensure common submodules are discoverable for tooling that relies on attribute
# resolution (e.g., ``unittest.mock.patch`` lookups used in the test suite).
# Importing here is lightweight and makes ``production.config`` available as an
# attribute on the package, preventing ``AttributeError`` during test
# collection.
from . import config  # noqa: F401
