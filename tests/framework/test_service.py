
import dataclasses
import pytest
from plugin.framework.service import BaseState, FsmTransition
from plugin.framework.service import ServiceRegistry, ServiceBase
'Tests for plugin.framework.state FSM contracts.'

@dataclasses.dataclass(frozen=True)
class _TrivialState(BaseState):
    n: int = 0

def test_base_state_subclass_frozen():
    s = _TrivialState(n=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.n = 2

def test_fsm_transition_immutable():
    t = FsmTransition(_TrivialState(n=0), [])
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.state = _TrivialState(n=1)

'Tests for plugin.framework.service.'

class DummyService(ServiceBase):
    name = 'dummy'

class AnotherService(ServiceBase):
    name = 'another'

class TestRegister():

    def test_register_and_get(self):
        reg = ServiceRegistry()
        svc = DummyService()
        reg.register('dummy', svc)
        assert (reg.get('dummy') is svc)

    def test_register_duplicate_raises(self):
        reg = ServiceRegistry()
        reg.register('dummy', DummyService())
        with pytest.raises(ValueError, match='already registered'):
            reg.register('dummy', DummyService())

class TestAccess():

    def test_getattr(self):
        reg = ServiceRegistry()
        svc = DummyService()
        reg.register('dummy', svc)
        assert (reg.dummy is svc)

    def test_getattr_missing_raises(self):
        reg = ServiceRegistry()
        with pytest.raises(AttributeError, match='No service registered'):
            _ = reg.nonexistent

    def test_contains(self):
        reg = ServiceRegistry()
        reg.register('dummy', DummyService())
        assert ('dummy' in reg)
        assert ('missing' not in reg)

    def test_get_returns_none_for_missing(self):
        reg = ServiceRegistry()
        assert (reg.get('nope') is None)

    def test_service_names(self):
        reg = ServiceRegistry()
        reg.register('dummy', DummyService())
        reg.register('another', AnotherService())
        assert reg.names() == ["dummy", "another"]

