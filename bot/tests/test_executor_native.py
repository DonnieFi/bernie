import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestNativeExecutor(unittest.TestCase):
    def test_native_executor_conforms_to_protocol(self):
        from executor import Executor
        from executors.native import NativeToolExecutor
        from tool_gateway import ToolGateway
        from tools import _registry

        gw = ToolGateway(registry=_registry)
        ne = NativeToolExecutor(gateway=gw)
        self.assertIsInstance(ne, Executor)
