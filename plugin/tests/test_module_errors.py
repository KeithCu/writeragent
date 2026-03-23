import pytest
from unittest.mock import MagicMock
from plugin.modules.draw.shapes import DrawShapes, DrawError

def test_draw_shapes_safe_create_shape_valid():
    """Test safe_create_shape with valid inputs creates and adds the shape."""
    draw_shapes = DrawShapes()

    page = MagicMock()
    shape = MagicMock()
    page.createInstance.return_value = shape

    position = MagicMock()
    position.X = 100
    position.Y = 200

    size = MagicMock()
    size.Width = 300
    size.Height = 400

    shape_type = "RectangleShape"

    result = draw_shapes.safe_create_shape(page, shape_type, position, size)

    page.createInstance.assert_called_once_with("com.sun.star.drawing.RectangleShape")
    shape.setPosition.assert_called_once_with(position)
    shape.setSize.assert_called_once_with(size)
    page.add.assert_called_once_with(shape)

    assert result == shape

def test_draw_shapes_safe_create_shape_invalid_page():
    """Test safe_create_shape raises DrawError when page is None."""
    draw_shapes = DrawShapes()

    position = MagicMock()
    position.X = 100
    position.Y = 200

    size = MagicMock()
    size.Width = 300
    size.Height = 400

    with pytest.raises(DrawError) as exc_info:
        draw_shapes.safe_create_shape(None, "RectangleShape", position, size)

    assert exc_info.value.code == "DRAW_PAGE_NULL"

def test_draw_shapes_safe_create_shape_invalid_position():
    """Test safe_create_shape raises DrawError when position is invalid."""
    draw_shapes = DrawShapes()

    page = MagicMock()

    # Missing X/Y
    position = MagicMock()
    del position.X

    size = MagicMock()
    size.Width = 300
    size.Height = 400

    with pytest.raises(DrawError) as exc_info:
        draw_shapes.safe_create_shape(page, "RectangleShape", position, size)

    assert exc_info.value.code == "DRAW_INVALID_POSITION"

def test_draw_shapes_safe_create_shape_invalid_size():
    """Test safe_create_shape raises DrawError when size is invalid."""
    draw_shapes = DrawShapes()

    page = MagicMock()

    position = MagicMock()
    position.X = 100
    position.Y = 200

    # Missing Width/Height
    size = MagicMock()
    size.Width = 0 # Invalid
    size.Height = 400

    with pytest.raises(DrawError) as exc_info:
        draw_shapes.safe_create_shape(page, "RectangleShape", position, size)

    assert exc_info.value.code == "DRAW_INVALID_SIZE"

def test_draw_shapes_safe_create_shape_creation_failed():
    """Test safe_create_shape raises DrawError when shape creation fails."""
    draw_shapes = DrawShapes()

    page = MagicMock()
    page.createInstance.return_value = None

    position = MagicMock()
    position.X = 100
    position.Y = 200

    size = MagicMock()
    size.Width = 300
    size.Height = 400

    with pytest.raises(DrawError) as exc_info:
        draw_shapes.safe_create_shape(page, "UnknownShape", position, size)

    assert exc_info.value.code == "DRAW_SHAPE_CREATION_FAILED"

def test_draw_shapes_safe_create_shape_exception_handling():
    """Test safe_create_shape wraps generic exceptions in DrawError."""
    draw_shapes = DrawShapes()

    page = MagicMock()
    page.createInstance.side_effect = Exception("Some UNO error")

    position = MagicMock()
    position.X = 100
    position.Y = 200

    size = MagicMock()
    size.Width = 300
    size.Height = 400

    with pytest.raises(DrawError) as exc_info:
        draw_shapes.safe_create_shape(page, "RectangleShape", position, size)

    assert exc_info.value.code == "DRAW_SHAPE_CREATION_ERROR"
    assert "Some UNO error" in exc_info.value.details["original_error"]
