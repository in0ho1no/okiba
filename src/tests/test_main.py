"""テストモジュール（テンプレート）。

このファイルはサンプルテストです。
実際のテストを追加する際の参考にしてください。
"""

import pytest

from main import function_example, main


class TestFunctionExample:
    """function_example() のテストクラス。"""

    def test_function_example_with_valid_args(self) -> None:
        """正常な引数で呼び出すテスト。"""
        try:
            function_example('テスト', 42)
        except Exception as e:
            pytest.fail(f'Unexpected exception: {e}')

    def test_function_example_with_string(self) -> None:
        """第1引数が文字列であるテスト。"""
        try:
            function_example('sample', 10)
        except Exception as e:
            pytest.fail(f'Unexpected exception: {e}')


class TestMain:
    """main() のテストクラス。"""

    def test_main_execution(self) -> None:
        """main() が正常に実行できるテスト。"""
        try:
            main()
        except Exception as e:
            pytest.fail(f'Unexpected exception: {e}')
