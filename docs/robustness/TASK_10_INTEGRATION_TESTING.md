# Task 10: Integration Testing & Validation

## Objective
Create comprehensive integration tests to validate all robustness improvements.

## Scope
- End-to-end testing scenarios
- Error injection testing
- Performance validation
- User experience validation

## Deliverables
1. **test_integration.py** - Comprehensive integration tests
2. **test_error_injection.py** - Error scenario tests
3. **test_performance.py** - Performance validation
4. **INTEGRATION_TEST_PLAN.md** - Test plan documentation

## Implementation Steps

### 1. Create Integration Test Framework
```python
class IntegrationTestBase(unittest.TestCase):
    """Base class for integration tests."""
    
    @classmethod
    def setUpClass(cls):
        """Set up test environment."""
        cls.test_config = {
            "endpoint": "http://localhost:11434",
            "model": "test-model",
            "temperature": 0.7
        }
        cls.mock_services = {}
        
        # Set up logging
        logging.basicConfig(level=logging.DEBUG)
    
    def setUp(self):
        """Set up individual test."""
        self.test_id = str(uuid.uuid4())
        self.events = []
        
        # Mock external services
        self._mock_external_services()
    
    def tearDown(self):
        """Clean up after test."""
        self._verify_no_errors()
        self._cleanup_mocks()
    
    def _mock_external_services(self):
        """Mock external services for testing."""
        # Mock LLM client
        from unittest.mock import Mock
        mock_client = Mock()
        mock_client.stream_completion.return_value = self._mock_stream_response()
        self.mock_services['llm_client'] = mock_client
    
    def _mock_stream_response(self):
        """Generate mock stream response."""
        def generator():
            yield {"content": "Test response", "finish_reason": "stop"}
        return generator()
    
    def _verify_no_errors(self):
        """Verify no unexpected errors occurred."""
        error_events = [e for e in self.events if e.get('type') == 'error']
        if error_events:
            self.fail(f"Unexpected errors occurred: {error_events}")
    
    def _cleanup_mocks(self):
        """Clean up mock services."""
        self.mock_services.clear()
```

### 2. Create Error Injection Tests
```python
class TestErrorInjection(IntegrationTestBase):
    """Test error handling by injecting errors."""
    
    def test_tool_execution_error(self):
        """Test handling of tool execution errors."""
        from plugin.framework.tool_registry import ToolRegistry
        from plugin.framework.errors import ToolExecutionError
        
        registry = ToolRegistry()
        
        # Create a tool that will fail
        class FailingTool(ToolBase):
            name = "failing_tool"
            
            def execute(self, **kwargs):
                raise ValueError("Simulated tool error")
        
        registry.register(FailingTool())
        
        # Test execution
        with self.assertRaises(ToolExecutionError) as cm:
            registry.execute("failing_tool", {})
        
        error = cm.exception
        self.assertEqual(error.code, "TOOL_EXECUTION_FAILED")
        self.assertIn("FailingTool", error.details.get('tool', ''))
        self.assertIn("Simulated tool error", error.details.get('original_error', ''))
    
    def test_network_retry_logic(self):
        """Test network retry logic with injected failures."""
        from plugin.framework.retry_decorator import retry_with_backoff
        from plugin.framework.errors import NetworkError
        
        attempt_count = [0]
        
        @retry_with_backoff(max_attempts=3, base_delay=0.01)
        def failing_operation():
            attempt_count[0] += 1
            if attempt_count[0] < 3:
                raise ConnectionError("Simulated network failure")
            return "success"
        
        # Should succeed on 3rd attempt
        result = failing_operation()
        self.assertEqual(result, "success")
        self.assertEqual(attempt_count[0], 3)
    
    def test_document_disposed_error(self):
        """Test handling of disposed document errors."""
        from plugin.framework.uno_safety import check_disposed
        from plugin.framework.errors import UnoObjectError
        
        # Create mock disposed object
        class MockDisposedObject:
            def isDisposed(self):
                return True
        
        disposed_obj = MockDisposedObject()
        
        # Test check
        with self.assertRaises(UnoObjectError) as cm:
            check_disposed(disposed_obj, "Test Object")
        
        error = cm.exception
        self.assertEqual(error.code, "UNO_OBJECT_DISPOSED")
        self.assertIn("Test Object", error.message)
```

### 3. Create State Machine Error Tests
```python
class TestStateMachineErrors(IntegrationTestBase):
    """Test state machine error handling."""
    
    def test_error_state_transition(self):
        """Test state machine transitions to error state."""
        from plugin.modules.chatbot.state_machine import SendHandlerState, ErrorEvent
        from plugin.modules.chatbot.tool_loop_state import handle_error_event
        
        # Initial state
        initial_state = SendHandlerState(
            handler_type="test",
            status="running",
            query_text="test query",
            model={},
            doc_type_str="writer"
        )
        
        # Error event
        error_event = ErrorEvent(
            error=ValueError("Test error"),
            context="test_context",
            is_recoverable=False
        )
        
        # Handle error
        new_state, effects = handle_error_event(initial_state, error_event)
        
        # Verify error state
        self.assertEqual(new_state.status, "error")
        self.assertIsNotNone(new_state.error_info)
        self.assertIn("Test error", new_state.error_info.get('error', ''))
        
        # Verify effects
        self.assertGreater(len(effects), 0)
        log_effects = [e for e in effects if hasattr(e, 'error')]
        self.assertGreater(len(log_effects), 0)
    
    def test_recovery_state_transition(self):
        """Test state machine recovery transitions."""
        from plugin.modules.chatbot.state_machine import SendHandlerState, ErrorEvent
        from plugin.modules.chatbot.tool_loop_state import handle_error_event
        
        # Initial state with recovery attempts
        initial_state = SendHandlerState(
            handler_type="test",
            status="running",
            query_text="test query",
            model={},
            doc_type_str="writer",
            recovery_attempts=1
        )
        
        # Recoverable error event
        error_event = ErrorEvent(
            error=ValueError("Recoverable error"),
            context="test_context",
            is_recoverable=True
        )
        
        # Handle error
        new_state, effects = handle_error_event(initial_state, error_event)
        
        # Verify recovery state
        self.assertEqual(new_state.status, "recovering")
        self.assertEqual(new_state.recovery_attempts, 2)
        self.assertIsNotNone(new_state.timeout_at)
        
        # Verify recovery effects
        recovery_effects = [e for e in effects if hasattr(e, 'handler_type')]
        self.assertGreater(len(recovery_effects), 0)
```

### 4. Create Performance Tests
```python
class TestPerformance(IntegrationTestBase):
    """Test performance characteristics."""
    
    def test_error_handling_overhead(self):
        """Test that error handling doesn't add excessive overhead."""
        import time
        from plugin.framework.errors import safe_json_loads
        
        # Test data
        test_json = '{"key": "value", "array": [1, 2, 3]}'
        iterations = 10000
        
        # Time safe_json_loads
        start_time = time.time()
        for _ in range(iterations):
            result = safe_json_loads(test_json)
        safe_time = time.time() - start_time
        
        # Time standard json.loads
        start_time = time.time()
        for _ in range(iterations):
            result = json.loads(test_json)
        standard_time = time.time() - start_time
        
        # Overhead should be minimal (< 20%)
        overhead = ((safe_time - standard_time) / standard_time) * 100
        self.assertLess(overhead, 20, f"Error handling overhead too high: {overhead:.1f}%")
    
    def test_retry_backoff_timing(self):
        """Test that retry backoff timing is correct."""
        import time
        from plugin.framework.retry_decorator import retry_with_backoff
        
        attempt_times = []
        
        @retry_with_backoff(max_attempts=4, base_delay=0.1, max_delay=0.5)
        def failing_operation():
            attempt_times.append(time.time())
            raise ConnectionError("Always fails")
        
        # This should fail after 4 attempts
        start_time = time.time()
        with self.assertRaises(Exception):
            failing_operation()
        end_time = time.time()
        
        # Should have 4 attempts
        self.assertEqual(len(attempt_times), 4)
        
        # Total time should be reasonable (allowing for some jitter)
        total_time = end_time - start_time
        expected_min = 0.1 + 0.2 + 0.4 + 0.0  # Last attempt doesn't delay
        expected_max = 0.15 + 0.3 + 0.6 + 0.0  # With jitter
        
        self.assertGreater(total_time, expected_min)
        self.assertLess(total_time, expected_max)
```

### 5. Create User Experience Tests
```python
class TestUserExperience(IntegrationTestBase):
    """Test user-facing error handling."""
    
    def test_user_friendly_error_messages(self):
        """Test that errors are presented in user-friendly format."""
        from plugin.framework.errors import format_error_for_display, ConfigError
        
        # Test various error types
        test_errors = [
            ConfigError("Configuration file not found", code="CONFIG_NOT_FOUND"),
            ValueError("Invalid input value"),
            Exception("Generic error")
        ]
        
        for error in test_errors:
            display_message = format_error_for_display(error)
            
            # Should be a string
            self.assertIsInstance(display_message, str)
            
            # Should not be empty
            self.assertGreater(len(display_message), 0)
            
            # Should not contain traceback or technical details
            self.assertNotIn("Traceback", display_message)
            self.assertNotIn("File \"", display_message)
            self.assertNotIn("line ", display_message)
    
    def test_error_recovery_options(self):
        """Test that errors provide recovery options when possible."""
        from plugin.framework.errors import ToolExecutionError
        
        # Test recoverable error
        recoverable_error = ToolExecutionError(
            "Network connection lost",
            code="NETWORK_CONNECTION_LOST",
            details={
                'recoverable': True,
                'suggestion': 'Check your internet connection and try again'
            }
        )
        
        # Should have recovery information
        self.assertTrue(recoverable_error.details.get('recoverable'))
        self.assertIn('suggestion', recoverable_error.details)
        
        # Test non-recoverable error
        fatal_error = ToolExecutionError(
            "Critical system error",
            code="CRITICAL_ERROR",
            details={
                'recoverable': False,
                'action': 'restart_application'
            }
        )
        
        # Should indicate non-recoverable
        self.assertFalse(fatal_error.details.get('recoverable'))
```

## Test Plan

### Integration Test Plan
1. **Setup**: Configure test environment with mock services
2. **Error Injection**: Test all error handling paths
3. **State Machine**: Test all state transitions including errors
4. **Performance**: Validate performance characteristics
5. **User Experience**: Test user-facing error handling
6. **Cleanup**: Verify proper resource cleanup

### Test Execution
```bash
# Run all integration tests
python -m pytest plugin/tests/test_integration.py -v

# Run specific test groups
python -m pytest plugin/tests/test_error_injection.py::TestErrorInjection -v
python -m pytest plugin/tests/test_state_machine_errors.py::TestStateMachineErrors -v

# Run with coverage
python -m pytest --cov=plugin --cov-report=html plugin/tests/test_integration.py
```

## Success Criteria
- ✅ All integration tests pass
- ✅ Error injection tests validate error handling
- ✅ State machine tests cover all transitions
- ✅ Performance tests meet benchmarks
- ✅ User experience tests validate friendly errors
- ✅ Test coverage > 90% for critical paths

## Continuous Integration
Add to CI pipeline:
```yaml
- name: Run Integration Tests
  run: |
    python -m pytest plugin/tests/test_integration.py --tb=short
    python -m pytest plugin/tests/test_error_injection.py --tb=short
    python -m pytest plugin/tests/test_performance.py --tb=short
```

## Documentation
Create **INTEGRATION_TEST_PLAN.md** with:
- Detailed test scenarios
- Expected outcomes
- Setup instructions
- Troubleshooting guide
- Performance benchmarks
