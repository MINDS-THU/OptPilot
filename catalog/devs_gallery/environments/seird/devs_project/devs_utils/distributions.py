### BEGIN: Import Section
import random
import math
from abc import ABC, abstractmethod
### END: Import Section

### BEGIN: Distribution Strategies

class Distribution(dict, ABC):
    """
    继承 dict，使得实例可以直接被 json.dumps 序列化。
    无需手动编写 to_dict 或在日志中调用转换方法。
    """
    @abstractmethod
    def sample(self):
        pass
    
    def __call__(self, *args, **kwds):
        return self.sample(*args, **kwds)

# --- 基础分布 ---

class Fixed(Distribution):
    def __init__(self, value):
        # 1. 初始化字典部分 (用于 Log)
        super().__init__(distribution="Fixed", value=value)
        # 2. 初始化属性部分 (用于计算)
        self.value = value
        
    def sample(self):
        return self.value

class UniformDist(Distribution):
    def __init__(self, min_val, max_val):
        super().__init__(distribution="Uniform", min=min_val, max=max_val)
        self.min_val = min_val
        self.max_val = max_val
        
    def sample(self):
        return random.uniform(self.min_val, self.max_val)

class NormalDist(Distribution):
    def __init__(self, mu, sigma):
        super().__init__(distribution="Normal", mu=mu, sigma=sigma)
        self.mu = mu
        self.sigma = sigma
        
    def sample(self):
        return random.normalvariate(self.mu, self.sigma)

class LogNormalDist(Distribution):
    def __init__(self, mu, sigma):
        super().__init__(distribution="LogNormal", mu=mu, sigma=sigma)
        self.mu = mu
        self.sigma = sigma

    def sample(self):
        return math.exp(random.normalvariate(self.mu, self.sigma))

class ExponentialDist(Distribution):
    def __init__(self, lambd):
        mean_val = 1.0/lambd if lambd != 0 else "inf"
        # 注意: 'lambda' 是保留字，作为 dict key 使用字符串没问题
        super().__init__(distribution="Exponential", lambd=lambd, mean_interval=mean_val)
        self.lambd = lambd
        
    def sample(self):
        return random.expovariate(self.lambd)

class PoissonDist(Distribution):
    def __init__(self, mu):
        super().__init__(distribution="Poisson", mu=mu)
        self.mu = mu
        
    def sample(self):
        # 简单模拟
        L = math.exp(-self.mu)
        k = 0
        p = 1.0
        while p > L:
            k += 1
            p *= random.random()
        return k - 1

class ChiSquaredDist(Distribution):
    def __init__(self, df):
        super().__init__(distribution="ChiSquared", df=df)
        self.df = df
        
    def sample(self):
        return random.gammavariate(self.df / 2.0, 2.0)

class StudentTDist(Distribution):
    def __init__(self, df):
        super().__init__(distribution="StudentT", df=df)
        self.df = df
        
    def sample(self):
        z = random.normalvariate(0, 1)
        v = random.gammavariate(self.df / 2.0, 2.0)
        return z / math.sqrt(v / self.df)

class GammaDist(Distribution):
    def __init__(self, alpha, beta):
        super().__init__(distribution="Gamma", alpha=alpha, beta=beta)
        self.alpha = alpha
        self.beta = beta

    def sample(self):
        return random.gammavariate(self.alpha, self.beta)

# --- 高级操作 ---

class Shifted(Distribution):
    """
    装饰器模式：对任何分布进行平移。
    """
    def __init__(self, dist: Distribution, offset: float):
        # dist 本身也是一个 dict (Distribution)，所以嵌套序列化没问题
        super().__init__(distribution="Shifted", offset=offset, base_distribution=dist)
        self.dist = dist
        self.offset = offset
    
    def sample(self):
        base_val = self.dist.sample()
        return base_val + self.offset

### END: Distribution Strategies