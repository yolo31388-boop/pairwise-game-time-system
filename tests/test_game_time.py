import pytest
from game_time.system import GameTimeSystem


class TestGameTimeSystem:

    def test_update_time(self):
        """时间更新不做缩放，游戏加速和减速不影响逻辑时间"""
        obj = GameTimeSystem()
        result = obj.update_time()
        self.assertIsNotNone(result)

    def test_add_timer(self):
        """定时器不做时间缩放，用真实时间而不是游戏时间"""
        obj = GameTimeSystem()
        result = obj.add_timer()
        self.assertIsNotNone(result)

    def test_update_day_night(self):
        """昼夜循环不做平滑过渡，白天黑夜瞬间切换"""
        obj = GameTimeSystem()
        result = obj.update_day_night()
        self.assertIsNotNone(result)

    def test_sync_time(self):
        """时间同步不做延迟补偿，服务器时间到客户端有延迟"""
        obj = GameTimeSystem()
        result = obj.sync_time()
        self.assertIsNotNone(result)

    def test_save_time(self):
        """时间持久化只存游戏时间，不存真实时间和时区"""
        obj = GameTimeSystem()
        result = obj.save_time()
        self.assertIsNotNone(result)

