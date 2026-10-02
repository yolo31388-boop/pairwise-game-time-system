"""Give plain (non-unittest) test classes unittest-style assertion methods."""

import unittest


def pytest_collection_modifyitems(items):
    for item in items:
        cls = getattr(item, "cls", None)
        if cls is None or issubclass(cls, unittest.TestCase):
            continue
        if not hasattr(cls, "failureException"):
            cls.failureException = AssertionError
        for name in dir(unittest.TestCase):
            if name.startswith(("assert", "fail")) and not hasattr(cls, name):
                setattr(cls, name, getattr(unittest.TestCase, name))
