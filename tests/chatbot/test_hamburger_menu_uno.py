# WriterAgent - Native UNO Test for Hamburger Menu
from plugin.testing_runner import native_test


@native_test
def test_popup_menu_image_and_command(ctx):
    """Verify PopupMenu supports setItemImage with 3 args and setCommand."""
    smgr = ctx.getServiceManager()
    popup = smgr.createInstanceWithContext("com.sun.star.awt.PopupMenu", ctx)
    assert popup is not None, "PopupMenu service creation failed"

    popup.insertItem(1, "Test Item", 0, 0)
    assert popup.getItemCount() == 1

    # Verify command binding
    popup.setCommand(1, "org.extension.writeragent:scripting.run_python_dialog")
    assert popup.getCommand(1) == "org.extension.writeragent:scripting.run_python_dialog"

    # Verify graphic loading and 3-arg setItemImage for all menu icons
    from plugin.chatbot.hamburger_menu import _load_graphic

    for icon in ("python_32.png", "python_cell_32.png", "search_32.png", "latex_32.png", "jupyter_32.png", "running_16.png", "stopped_16.png"):
        g = _load_graphic(ctx, icon)
        assert g is not None, f"Failed to load graphic for {icon}"
    popup.setItemImage(1, g, False)
