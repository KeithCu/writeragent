
import dataclasses
import pytest
import pytest
from plugin.framework.service import BaseState, FsmTransition
from plugin.framework.service import ServiceRegistry, ServiceBase
'Tests for plugin.framework.state FSM contracts.'

@dataclasses.dataclass(frozen=True)
class _TrivialState(BaseState):
    n: int = 0

def _trivial_next(state: _TrivialState, increment: bool) -> FsmTransition[_TrivialState]:
    if increment:
        return FsmTransition(state=dataclasses.replace(state, n=(state.n + 1)), effects=['tick'])
    return FsmTransition(state=state, effects=[])

def test_base_state_subclass_frozen():
    s = _TrivialState(n=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.n = 2

def test_fsm_transition_immutable():
    t = FsmTransition(_TrivialState(n=0), [])
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.state = _TrivialState(n=1)

def test_trivial_next_pure():
    s0 = _TrivialState()
    t1 = _trivial_next(s0, True)
    assert (s0.n == 0)
    assert (t1.state.n == 1)
    assert (t1.effects == ['tick'])
    t2 = _trivial_next(t1.state, False)
    assert (t2.state.n == 1)
    assert (t2.effects == [])
'Tests for plugin.framework.service.'

class DummyService(ServiceBase):
    name = 'dummy'

class AnotherService(ServiceBase):
    name = 'another'

class TestServiceBase():

    def test_service_base_methods(self):

        class MyService(ServiceBase):
            name = 'my_service'
        svc = MyService()
        svc.initialize('mock_ctx')
        svc.shutdown()

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

    def test_register(self):
        reg = ServiceRegistry()
        obj = {'hello': 'world'}
        reg.register('myobj', obj)
        assert (reg.get('myobj') is obj)

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
        assert (set(reg.service_names) == {'dummy', 'another'})

class TestLifecycle():

    def test_initialize_all(self):
        reg = ServiceRegistry()
        initialized = []

        class InitService(ServiceBase):
            name = 'init_svc'

            def initialize(self, ctx):
                initialized.append(ctx)
        reg.register('init_svc', InitService())
        reg.initialize_all('fake_ctx')
        assert (initialized == ['fake_ctx'])

    def test_shutdown_all_swallows_errors(self):
        reg = ServiceRegistry()

        class BadShutdown(ServiceBase):
            name = 'bad'

            def shutdown(self):
                raise RuntimeError('boom')
        reg.register('bad', BadShutdown())
        reg.shutdown_all()
