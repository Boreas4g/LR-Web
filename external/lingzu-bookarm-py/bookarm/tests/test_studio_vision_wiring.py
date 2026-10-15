"""Studio vision-debug 的 plan_getter 接线回归测试。

服务入口曾写成 ``lambda: sdk.take_book.plan``——但 ``sdk.take_book`` 是公开的
工作流**方法**，不是用例对象，spine-check/align-run 每次调用都以
AttributeError（'function' object has no attribute 'plan'）崩溃成 500。
"""

from __future__ import annotations

import unittest

from bookarm.api.sdk import BookArmSDK
from bookarm.application.task_manager import TaskManager
from bookarm.service.__main__ import take_book_plan_getter


class FakeTakeBookUseCase:
    def __init__(self) -> None:
        self.plan = object()


def _sdk_with(use_case: FakeTakeBookUseCase) -> BookArmSDK:
    return BookArmSDK(
        task_manager=TaskManager(),
        take_book=use_case,
        return_book=object(),
        return_book_to_end=object(),
    )


class TakeBookPlanGetterTests(unittest.TestCase):
    def test_reads_plan_from_the_use_case(self) -> None:
        use_case = FakeTakeBookUseCase()
        getter = take_book_plan_getter(_sdk_with(use_case))
        self.assertIs(getter(), use_case.plan)

    def test_sees_hot_swapped_plan(self) -> None:
        # 调参页热更新就是替换用例的 plan 属性；读取入口必须跟上。
        use_case = FakeTakeBookUseCase()
        getter = take_book_plan_getter(_sdk_with(use_case))
        new_plan = object()
        use_case.plan = new_plan
        self.assertIs(getter(), new_plan)


if __name__ == "__main__":
    unittest.main()
