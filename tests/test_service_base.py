from plugin.framework.service_base import ServiceBase

class MyService(ServiceBase):
    name = "my_service"

def test_service_base_methods():
    svc = MyService()

    # These should not raise exceptions
    svc.initialize("mock_ctx")
    svc.shutdown()
