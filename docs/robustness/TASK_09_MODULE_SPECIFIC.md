# Task 9: Module-Specific Error Handling

## Objective
Improve error handling in specific modules (Calc, Draw, Launcher).

## Scope
- `plugin/calc/manipulator.py` - Calc operations
- `plugin/draw/*` - Draw/Impress operations
- `plugin/launcher/*` - External process management

## Critical Issues to Fix

### 1. Calc Manipulator Errors
**Current:** Broad exception handling in Calc operations

**Fix:**
- Specific exception handling for Calc operations
- Cell reference validation
- Formula error handling

### 2. Draw Operations Errors
**Current:** Limited error handling in Draw operations

**Fix:**
- Shape operation validation
- Slide operation safety
- Image handling errors

## Deliverables
1. **calc/manipulator.py** - Enhanced error handling
2. **draw/*` - Robust Draw operations
3. **launcher/*` - Safe process management
4. **test_module_errors.py** - Unit tests

## Implementation Steps

### 1. Enhance Calc Manipulator
In `plugin/calc/manipulator.py`:

```python
class CalcManipulator:
    def safe_get_cell_value(self, sheet, cell_address):
        """Safely get cell value with comprehensive error handling."""
        try:
            # Validate sheet
            if not sheet:
                raise CalcError(
                    "Sheet is None",
                    code="CALC_SHEET_NULL",
                    details={"operation": "get_cell_value"}
                )
            
            # Validate cell address
            if not self._is_valid_cell_address(cell_address):
                raise CalcError(
                    f"Invalid cell address: {cell_address}",
                    code="CALC_INVALID_ADDRESS",
                    details={"address": cell_address}
                )
            
            # Get cell
            cell = sheet.getCellRangeByName(cell_address)
            if not cell:
                raise CalcError(
                    f"Cell not found: {cell_address}",
                    code="CALC_CELL_NOT_FOUND",
                    details={"address": cell_address}
                )
            
            # Get value with type handling
            cell_type = cell.getType()
            
            if cell_type == com.sun.star.table.CellContentType.EMPTY:
                return None
            elif cell_type == com.sun.star.table.CellContentType.VALUE:
                return cell.getValue()
            elif cell_type == com.sun.star.table.CellContentType.TEXT:
                return cell.getString()
            elif cell_type == com.sun.star.table.CellContentType.FORMULA:
                try:
                    return cell.getValue()
                except Exception as e:
                    # Formula error
                    error_code = cell.getError()
                    raise CalcError(
                        f"Formula error in {cell_address}: {self._get_error_name(error_code)}",
                        code="CALC_FORMULA_ERROR",
                        details={
                            "address": cell_address,
                            "error_code": error_code,
                            "error_name": self._get_error_name(error_code)
                        }
                    ) from e
            else:
                raise CalcError(
                    f"Unknown cell type: {cell_type}",
                    code="CALC_UNKNOWN_CELL_TYPE",
                    details={"address": cell_address, "type": cell_type}
                )
                
        except CalcError:
            # Re-raise our calc errors
            raise
        except Exception as e:
            # Wrap other exceptions
            raise CalcError(
                f"Failed to get cell value: {str(e)}",
                code="CALC_CELL_VALUE_ERROR",
                details={
                    "address": cell_address,
                    "original_error": str(e),
                    "error_type": type(e).__name__
                }
            ) from e
```

### 2. Add Calc-Specific Error Class
```python
class CalcError(WriterAgentException):
    """Calc-specific errors."""
    
    def __init__(self, message, code="CALC_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)
```

### 3. Enhance Draw Operations
In `plugin/draw/shapes.py`:

```python
class DrawShapes:
    def safe_create_shape(self, page, shape_type, position, size):
        """Safely create shape with error handling."""
        try:
            # Validate inputs
            if not page:
                raise DrawError(
                    "Page is None",
                    code="DRAW_PAGE_NULL",
                    details={"operation": "create_shape", "shape_type": shape_type}
                )
            
            if not self._is_valid_position(position):
                raise DrawError(
                    f"Invalid position: {position}",
                    code="DRAW_INVALID_POSITION",
                    details={"position": position}
                )
            
            if not self._is_valid_size(size):
                raise DrawError(
                    f"Invalid size: {size}",
                    code="DRAW_INVALID_SIZE",
                    details={"size": size}
                )
            
            # Create shape
            shape = page.createInstance(f"com.sun.star.drawing.{shape_type}")
            if not shape:
                raise DrawError(
                    f"Failed to create shape of type: {shape_type}",
                    code="DRAW_SHAPE_CREATION_FAILED",
                    details={"shape_type": shape_type}
                )
            
            # Set properties
            shape.setPosition(position)
            shape.setSize(size)
            
            # Add to page
            page.add(shape)
            
            return shape
            
        except DrawError:
            # Re-raise our draw errors
            raise
        except Exception as e:
            # Wrap other exceptions
            raise DrawError(
                f"Failed to create shape: {str(e)}",
                code="DRAW_SHAPE_CREATION_ERROR",
                details={
                    "shape_type": shape_type,
                    "position": position,
                    "size": size,
                    "original_error": str(e),
                    "error_type": type(e).__name__
                }
            ) from e
```

### 4. Add Draw-Specific Error Class
```python
class DrawError(WriterAgentException):
    """Draw-specific errors."""
    
    def __init__(self, message, code="DRAW_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)
```

### 5. Enhance Launcher Process Management
In `plugin/launcher/__init__.py`:

```python
class ProcessLauncher:
    def safe_launch_process(self, command, args, timeout=None):
        """Safely launch external process with error handling."""
        try:
            # Validate command
            if not command:
                raise LauncherError(
                    "Command is empty",
                    code="LAUNCHER_EMPTY_COMMAND",
                    details={"command": command, "args": args}
                )
            
            # Check if command exists
            if not self._command_exists(command):
                raise LauncherError(
                    f"Command not found: {command}",
                    code="LAUNCHER_COMMAND_NOT_FOUND",
                    details={"command": command}
                )
            
            # Prepare process
            full_command = [command] + (args if args else [])
            
            # Launch process
            process = subprocess.Popen(
                full_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Monitor process
            try:
                stdout, stderr = process.communicate(timeout=timeout)
                
                if process.returncode != 0:
                    raise LauncherError(
                        f"Process failed with exit code {process.returncode}",
                        code="LAUNCHER_PROCESS_FAILED",
                        details={
                            "command": command,
                            "args": args,
                            "exit_code": process.returncode,
                            "stdout": stdout,
                            "stderr": stderr
                        }
                    )
                
                return {
                    "success": True,
                    "exit_code": process.returncode,
                    "stdout": stdout,
                    "stderr": stderr
                }
                
            except subprocess.TimeoutExpired:
                # Kill process
                process.kill()
                raise LauncherError(
                    f"Process timed out after {timeout} seconds",
                    code="LAUNCHER_PROCESS_TIMEOUT",
                    details={
                        "command": command,
                        "args": args,
                        "timeout": timeout
                    }
                )
                
        except LauncherError:
            # Re-raise our launcher errors
            raise
        except Exception as e:
            # Wrap other exceptions
            raise LauncherError(
                f"Failed to launch process: {str(e)}",
                code="LAUNCHER_LAUNCH_ERROR",
                details={
                    "command": command,
                    "args": args,
                    "original_error": str(e),
                    "error_type": type(e).__name__
                }
            ) from e
```

### 6. Add Launcher-Specific Error Class
```python
class LauncherError(WriterAgentException):
    """Launcher-specific errors."""
    
    def __init__(self, message, code="LAUNCHER_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)
```

## Testing
Create comprehensive tests for:
- Calc operation error handling
- Draw operation error handling
- Launcher process management
- Error propagation
- Recovery scenarios

## Success Criteria
- ✅ Calc operations handle errors gracefully
- ✅ Draw operations validate inputs
- ✅ Launcher manages processes safely
- ✅ Module-specific error classes used
- ✅ Comprehensive test coverage
