"""
Global Simulation Context
用于存储全局仿真时钟引用，避免在模型间层层传递 clock 参数。
"""

from xdevs.sim import SimulationClock

class SimContext:
    _clock_ref = None

    @classmethod
    def set_global_clock(cls, clock_obj: SimulationClock):
        """设定全局时钟对象 (引用传递)"""
        cls._clock_ref = clock_obj

    @classmethod
    def get_time(cls):
        """获取当前时间"""
        if cls._clock_ref:
            return cls._clock_ref.time 
        return 0.0

def set_global_clock(clock):
    SimContext.set_global_clock(clock)

def get_current_time():
    return SimContext.get_time()