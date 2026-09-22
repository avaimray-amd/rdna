"""Parser implementations. Importing this package registers every parser."""

from . import ooxml  # noqa: F401
from . import ole  # noqa: F401
from . import pdf  # noqa: F401
from . import graphics  # noqa: F401
from . import plain  # noqa: F401

__all__ = ["ooxml", "ole", "pdf", "graphics", "plain"]
