import logging
import time
from abc import ABC
from abc import abstractmethod
from typing import Any
from typing import Callable
from typing import Dict
from typing import Optional
from typing import Type
from typing import Union

from typing_extensions import Protocol

from paasta_tools.utils import load_system_paasta_config

log = logging.getLogger(__name__)

try:
    import yelp_meteorite
except ImportError:
    yelp_meteorite = None

_metrics_interfaces: Dict[str, Type['BaseMetrics']] = {}


class TimerProtocol(Protocol):
    def start(self) -> None:
        raise NotImplementedError()

    def stop(self) -> None:
        raise NotImplementedError()


class GaugeProtocol(Protocol):
    def set(self, value: Union[int, float]) -> None:
        raise NotImplementedError()


class BaseMetrics(ABC):
    def __init__(self, base_name: str) -> None:
        self.base_name = base_name

    @abstractmethod
    def create_timer(self, name: str, **kwargs: Any) -> TimerProtocol:
        raise NotImplementedError()

    @abstractmethod
    def create_gauge(self, name: str, **kwargs: Any) -> GaugeProtocol:
        raise NotImplementedError()


def get_metrics_interface(base_name: str) -> BaseMetrics:
    metrics_provider = load_system_paasta_config().get_metrics_provider()
    return _metrics_interfaces[metrics_provider](base_name)


def register_metrics_interface(name: Optional[str]) -> Callable[[Type[BaseMetrics]], Type[BaseMetrics]]:
    def outer(func: Type[BaseMetrics]) -> Type[BaseMetrics]:
        _metrics_interfaces[name] = func
        return func
    return outer


@register_metrics_interface('meteorite')
class MeteoriteMetrics(BaseMetrics):
    def __init__(self, base_name: str) -> None:
        self.base_name = base_name
        if yelp_meteorite is None:
            raise ImportError("yelp_meteorite not imported, pleast try another metrics provider")

    def create_timer(self, name: str, **kwargs: Any) -> TimerProtocol:
        return yelp_meteorite.create_timer(self.base_name + '.' + name, kwargs)

    def create_gauge(self, name: str, **kwargs: Any) -> GaugeProtocol:
        return yelp_meteorite.create_gauge(self.base_name + '.' + name, kwargs)


class Timer(TimerProtocol):
    def __init__(self, name: str) -> None:
        self.name = name

    def start(self) -> None:
        log.debug("timer {} start at {}".format(self.name, time.time()))

    def stop(self) -> None:
        log.debug("timer {} stop at {}".format(self.name, time.time()))


class Gauge(GaugeProtocol):
    def __init__(self, name: str) -> None:
        self.name = name

    def set(self, value: Union[int, float]) -> None:
        log.debug("gauge {} set to {}".format(self.name, value))


@register_metrics_interface(None)
class NoMetrics(BaseMetrics):
    def __init__(self, base_name: str) -> None:
        self.base_name = base_name

    def create_timer(self, name: str, **kwargs: Any) -> Timer:
        return Timer(self.base_name + '.' + name)

    def create_gauge(self, name: str, **kwargs: Any) -> Gauge:
        return Gauge(self.base_name + '.' + name)
