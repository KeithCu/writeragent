# WriterAgent - tests for notebook run button wiring

from __future__ import annotations

from unittest.mock import MagicMock, patch


import plugin.notebook.notebook_controls as notebook_controls
import plugin.framework.thread_guard as tg
from plugin.notebook.notebook_controls import (
    NotebookFormContainerListener,
    NotebookFormRunListener,
    NotebookRunButtonListener,
    ensure_form_design_mode_off,
    form_run_listeners,
    get_control_view_for_model,
    prune_dead_listeners,
    wire_all_notebook_run_buttons,
)


def setup_function() -> None:
    notebook_controls._listener_refs = []
    notebook_controls._warned_no_container = set()
    notebook_controls._doc_listener = None


def test_wire_all_ok_off_main_thread(monkeypatch):
    """File Open XFilter is Dummy-2, not threading.main_thread()."""
    monkeypatch.setattr(tg, "on_main_thread", lambda: False)
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    was = tg.GUARD_ON
    tg.GUARD_ON = True
    try:
        with patch("plugin.notebook.notebook_controls.load_registry", return_value=None):
            result = wire_all_notebook_run_buttons(MagicMock(), MagicMock())
        assert result == 0

        doc = MagicMock()
        doc.getCurrentController.return_value = None
        ensure_form_design_mode_off(doc)
        assert doc.ApplyFormDesignMode is False
    finally:
        tg.GUARD_ON = was


def test_wire_all_with_registry_off_main_thread_does_not_raise(monkeypatch):
    """Saved registry + Dummy-2 must not call guarded get_runtime_uid.

    File Open reaches wire_all after save_registry. get_runtime_uid raises
    when the guard is on and the caller is not the main thread; filter()
    then returns False and loadComponentFromURL returns None.
    """
    monkeypatch.setattr(tg, "on_main_thread", lambda: False)
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    was = tg.GUARD_ON
    tg.GUARD_ON = True
    doc = MagicMock()
    doc.getURL.return_value = ""
    doc.getRuntimeUID.return_value = "uid-dummy-2"
    state = MagicMock()
    state.code_cells = [MagicMock(), MagicMock(), MagicMock()]
    try:
        with (
            patch("plugin.notebook.notebook_controls.load_registry", return_value=state),
            patch("plugin.notebook.notebook_controls._form_and_container", return_value=(None, None)),
        ):
            result = wire_all_notebook_run_buttons(MagicMock(), doc)
        assert result == 0
        assert notebook_controls._doc_key(doc) == "uid:uid-dummy-2"
        assert len(form_run_listeners(doc)) == 0
    finally:
        tg.GUARD_ON = was


def test_wire_all_returns_0_no_container():
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = ""
    doc.getRuntimeUID.return_value = "uid-form-no-container"

    state = MagicMock()
    state.code_cells = [MagicMock()]

    with (
        patch("plugin.notebook.notebook_controls.load_registry", return_value=state),
        patch("plugin.notebook.notebook_controls._form_and_container", return_value=(None, None))
    ):
        result = wire_all_notebook_run_buttons(ctx, doc)

    assert result == 0
    assert len(form_run_listeners(doc)) == 0


def test_install_notebook_run_button_wiring_global_listener():
    ctx = MagicMock()
    smgr = MagicMock()
    ctx.getServiceManager.return_value = smgr
    broadcaster = MagicMock()
    smgr.createInstanceWithContext.return_value = broadcaster

    with (
        patch("plugin.framework.uno_context.get_active_document", return_value=None),
        patch("plugin.framework.uno_context.get_active_document", return_value=None),
    ):
        notebook_controls.install_notebook_run_button_wiring(ctx)

    smgr.createInstanceWithContext.assert_called_with("com.sun.star.frame.GlobalEventBroadcaster", ctx)
    broadcaster.addDocumentEventListener.assert_called_once()
    assert notebook_controls._doc_listener is not None


def test_doc_event_listener_wires_buttons():
    ctx = MagicMock()
    smgr = MagicMock()
    ctx.getServiceManager.return_value = smgr
    broadcaster = MagicMock()
    smgr.createInstanceWithContext.return_value = broadcaster

    with (
        patch("plugin.framework.uno_context.get_active_document", return_value=None),
    ):
        notebook_controls.install_notebook_run_button_wiring(ctx)

    listener = notebook_controls._doc_listener
    assert listener is not None

    doc = MagicMock()
    doc_event = MagicMock()
    doc_event.EventName = "OnViewCreated"
    doc_event.Source = doc
    doc_event.ViewController = None

    with (
        patch("plugin.notebook.notebook_controls.wire_all_notebook_run_buttons") as wire_all,
    ):
        listener.on_document_event(doc_event)

    wire_all.assert_called_once_with(ctx, doc, new_view=True)


def test_ensure_form_design_mode_off_no_controller():
    doc = MagicMock()
    doc.getCurrentController.return_value = None
    ensure_form_design_mode_off(doc)
    assert doc.ApplyFormDesignMode is False

def test_ensure_form_design_mode_off_with_controller():
    doc = MagicMock()
    controller = MagicMock()
    doc.getCurrentController.return_value = controller
    ensure_form_design_mode_off(doc)
    assert doc.ApplyFormDesignMode is False
    controller.setFormDesignMode.assert_called_once_with(False)

def test_get_control_view_uses_gettypebyname_for_xcontrolaccess():
    doc = MagicMock()
    controller = MagicMock()
    doc.getCurrentController.return_value = controller
    controller.getControl.return_value = None
    model = MagicMock()
    view = MagicMock()
    access = MagicMock()
    access.getControl.return_value = view

    type_mock = MagicMock()
    with patch("plugin.notebook.notebook_controls.uno.getTypeByName", return_value=type_mock) as get_type:
        controller.queryInterface.return_value = access
        result = get_control_view_for_model(doc, model)
    assert result is view
    get_type.assert_called_with("com.sun.star.view.XControlAccess")
    controller.queryInterface.assert_called_once_with(type_mock)
    access.getControl.assert_called_once_with(model)


def test_notebook_run_button_listener_resolve_doc_order():
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = "file:///tmp/nb.odt"
    doc.getRuntimeUID.return_value = "uid-resolve-order"
    listener = NotebookRunButtonListener(ctx, doc, "abc")
    listener._doc_weak = None

    # URL set, weakref empty, active document key matches -> resolve_document_by_url not called
    active_doc = MagicMock()
    active_doc.getURL.return_value = "file:///tmp/nb.odt"
    active_doc.getRuntimeUID.return_value = "uid-resolve-order"
    with (
        patch("plugin.framework.uno_context.get_active_document", return_value=active_doc),
        patch("plugin.framework.uno_context.resolve_document_by_url") as resolve_url,
    ):
        assert listener._resolve_doc() is active_doc
        resolve_url.assert_not_called()

    # Active key differs -> URL resolve is called
    diff_doc = MagicMock()
    diff_doc.getURL.return_value = "file:///tmp/other.odt"
    diff_doc.getRuntimeUID.return_value = "uid-different"
    resolved_doc = MagicMock()
    with (
        patch("plugin.framework.uno_context.get_active_document", return_value=diff_doc),
        patch("plugin.framework.uno_context.resolve_document_by_url", return_value=(resolved_doc, "writer")) as resolve_url,
    ):
        assert listener._resolve_doc() is resolved_doc
        resolve_url.assert_called_once_with(ctx, "file:///tmp/nb.odt")

    # Disposed weakref falls through
    dead_doc = MagicMock()
    dead_doc.getRuntimeUID.side_effect = Exception("disposed")
    dead_doc.getURL.side_effect = Exception("disposed")
    import weakref

    listener._doc_weak = weakref.ref(dead_doc)
    with (
        patch("plugin.framework.uno_context.get_active_document", return_value=active_doc),
        patch("plugin.framework.uno_context.resolve_document_by_url") as resolve_url,
    ):
        assert listener._resolve_doc() is active_doc
        assert listener._doc_weak is None
        resolve_url.assert_not_called()


def test_listener_init_when_get_url_raises():
    ctx = MagicMock()
    doc = MagicMock()
    doc.getRuntimeUID.return_value = "uid-runtime-works"
    doc.getURL.side_effect = RuntimeError("broken getURL")
    listener = NotebookRunButtonListener(ctx, doc, "abc")
    assert listener._doc_key_val == "uid:uid-runtime-works"
    assert listener._doc_url == ""
    assert notebook_controls._doc_key(doc) == "uid:uid-runtime-works"


def test_notebook_run_button_listener_calls_runner():
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = ""
    listener = NotebookRunButtonListener(ctx, doc, "deadbeef")
    with patch("plugin.notebook.notebook_runner.run_cell_for_doc_hex") as run:
        listener.on_action_performed(MagicMock())
    run.assert_called_once_with(ctx, doc, "deadbeef")


def test_notebook_run_button_listener_untitled_resolves_by_runtime_uid():
    """Hidden / non-current untitled docs are not getCurrentComponent; URL is empty."""
    ctx = MagicMock()
    doc = MagicMock()
    found = MagicMock()
    doc.getURL.return_value = ""
    doc.getRuntimeUID.return_value = "uid-hidden-nb"
    listener = NotebookRunButtonListener(ctx, doc, "cafebabecafebabecafebabecafebabe")
    listener._doc_weak = None
    with (
        patch("plugin.framework.uno_context.resolve_document_by_url", return_value=(found, "writer")) as resolve,
        patch("plugin.framework.uno_context.get_active_document", return_value=None),
        patch("plugin.notebook.notebook_runner.run_cell_for_doc_hex") as run,
    ):
        listener.on_action_performed(MagicMock())
    resolve.assert_called_with(ctx, "uid-hidden-nb")
    run.assert_called_once_with(ctx, found, "cafebabecafebabecafebabecafebabe")


def test_form_run_listener_dispatches_nb_run_name():
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = ""
    listener = NotebookFormRunListener(ctx, doc)
    model = MagicMock()
    model.Name = "nb_run_deadbeefdeadbeefdeadbeefdeadbeef"
    control = MagicMock()
    control.getModel.return_value = model
    ev = MagicMock()
    ev.Source = control
    ev.ActionCommand = ""
    with patch("plugin.notebook.notebook_runner.run_cell_for_doc_hex") as run:
        listener.on_action_performed(ev)
    run.assert_called_once_with(ctx, doc, "deadbeefdeadbeefdeadbeefdeadbeef")


def test_form_run_listener_ignores_non_run_controls():
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = ""
    listener = NotebookFormRunListener(ctx, doc)
    model = MagicMock()
    model.Name = "nb_cell_0_code"
    control = MagicMock()
    control.getModel.return_value = model
    ev = MagicMock()
    ev.Source = control
    ev.ActionCommand = ""
    with patch("plugin.notebook.notebook_runner.run_cell_for_doc_hex") as run:
        listener.on_action_performed(ev)
    run.assert_not_called()


def test_wire_all_attaches_one_form_listener_without_getcontrol():
    """Import wiring must not call getControl once per ▶ button."""
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = ""
    doc.getRuntimeUID.return_value = "uid-form-1"

    run_ctrl = MagicMock()
    run_model = MagicMock()
    run_model.Name = "nb_run_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    run_ctrl.getModel.return_value = run_model
    run_ctrl.queryInterface.return_value = run_ctrl

    code_ctrl = MagicMock()
    code_model = MagicMock()
    code_model.Name = "nb_cell_0_code"
    code_ctrl.getModel.return_value = code_model
    code_ctrl.queryInterface.return_value = None
    code_ctrl.addActionListener.side_effect = RuntimeError("not a button")

    container = MagicMock()
    container.getControls.return_value = (code_ctrl, run_ctrl)
    fc = MagicMock()
    fc.getContainer.return_value = container
    controller = MagicMock()
    controller.getFormController.return_value = fc
    doc.getCurrentController.return_value = controller
    forms = MagicMock()
    forms.getCount.return_value = 1
    forms.getByIndex.return_value = MagicMock()
    doc.getDrawPage.return_value.getForms.return_value = forms

    cell = MagicMock()
    cell.cell_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    state = MagicMock()
    state.code_cells = [cell, cell]

    with (
        patch("plugin.notebook.notebook_controls.load_registry", return_value=state),
        patch("plugin.notebook.notebook_controls.get_control_view_for_model") as get_view,
        patch("plugin.notebook.writer_importer.flush_ui_idle") as flush_idle,
    ):
        first = wire_all_notebook_run_buttons(ctx, doc)
        second = wire_all_notebook_run_buttons(ctx, doc)
    assert first == 1
    assert second == 1
    flush_idle.assert_not_called()
    get_view.assert_not_called()
    run_ctrl.addActionListener.assert_called_once()
    container.addContainerListener.assert_called_once()
    form_lis = [lis for lis in notebook_controls._listener_refs if getattr(lis, "_form_level", False)]
    assert len(form_lis) == 1


def _play_button_doc(uid: str):
    doc = MagicMock()
    doc.getURL.return_value = ""
    doc.getRuntimeUID.return_value = uid
    run_ctrl = MagicMock()
    run_model = MagicMock()
    run_model.Name = "nb_run_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    run_ctrl.getModel.return_value = run_model
    run_ctrl.queryInterface.return_value = run_ctrl
    container = MagicMock()
    container.getControls.return_value = (run_ctrl,)
    state = MagicMock()
    state.code_cells = [MagicMock()]
    return doc, run_ctrl, container, state


def _fire_doc_event(listener, doc, name: str) -> None:
    event = MagicMock()
    event.EventName = name
    event.ViewController.getModel.return_value = doc
    listener.on_document_event(event)


def test_doc_event_does_not_attach_a_second_form_listener():
    """A later load event must not add another ▶ listener on an already-wired doc."""
    ctx = MagicMock()
    doc, run_ctrl, container, state = _play_button_doc("uid-wire-once")
    notebook_controls._install_doc_event_listener(ctx)
    listener = notebook_controls._doc_listener
    assert listener is not None

    with (
        patch("plugin.notebook.notebook_controls.load_registry", return_value=state) as mock_load,
        patch("plugin.notebook.notebook_controls._form_and_container", return_value=(MagicMock(), container)) as mock_fc,
        patch("plugin.notebook.notebook_controls.ensure_form_design_mode_off") as mock_off,
    ):
        assert wire_all_notebook_run_buttons(ctx, doc) == 1
        _fire_doc_event(listener, doc, "OnViewCreated")

        mock_load.reset_mock()
        mock_fc.reset_mock()
        mock_off.reset_mock()

        _fire_doc_event(listener, doc, "OnLoad")
        _fire_doc_event(listener, doc, "OnLoadFinished")
        _fire_doc_event(listener, doc, "OnNew")

        mock_load.assert_not_called()
        mock_fc.assert_not_called()
        assert mock_off.call_count == 3

    run_ctrl.addActionListener.assert_called_once()
    container.addContainerListener.assert_called_once()
    assert len(form_run_listeners(doc)) == 1


def test_doc_event_retries_wire_when_file_open_had_no_container():
    """A failed File Open wire discards the key so the view event can attach once."""
    ctx = MagicMock()
    doc, run_ctrl, container, state = _play_button_doc("uid-retry-container")
    notebook_controls._install_doc_event_listener(ctx)
    listener = notebook_controls._doc_listener
    holder: dict[str, object] = {"container": None}

    def _forms(_doc):
        found = holder["container"]
        if found is None:
            return None, None
        return MagicMock(), found

    with (
        patch("plugin.notebook.notebook_controls.load_registry", return_value=state),
        patch("plugin.notebook.notebook_controls._form_and_container", side_effect=_forms),
    ):
        assert wire_all_notebook_run_buttons(ctx, doc) == 0
        assert len(form_run_listeners(doc)) == 0
        holder["container"] = container
        _fire_doc_event(listener, doc, "OnLoad")
        _fire_doc_event(listener, doc, "OnLoadFinished")

    run_ctrl.addActionListener.assert_called_once()
    container.addContainerListener.assert_called_once()
    assert len(form_run_listeners(doc)) == 1


def test_no_container_warning_logged_once(caplog):
    import logging

    ctx = MagicMock()
    doc = MagicMock()
    doc.getRuntimeUID.return_value = "uid-no-container-warn"
    doc.getURL.return_value = ""
    notebook_controls._install_doc_event_listener(ctx)
    listener = notebook_controls._doc_listener
    state = MagicMock(code_cells=[MagicMock()])
    with (
        patch("plugin.notebook.notebook_controls.load_registry", return_value=state),
        patch("plugin.notebook.notebook_controls._form_and_container", return_value=(None, None)),
        caplog.at_level(logging.WARNING, logger="writeragent.notebook"),
    ):
        _fire_doc_event(listener, doc, "OnLoad")
        _fire_doc_event(listener, doc, "OnLoadFinished")

    warns = [rec.message for rec in caplog.records if "no form controller container" in rec.message]
    assert len(warns) == 1


def test_listener_counts_exclude_leftover_docs():
    """GHA 34643210006: leftover import-filter listeners inflated global counts."""
    leftover = MagicMock()
    leftover._form_level = True
    leftover._doc_key_val = "uid:41"
    leftover._hex_id = None
    current = MagicMock()
    current._form_level = True
    current._doc_key_val = "uid:99"
    current._hex_id = None
    notebook_controls._listener_refs = [leftover, current]
    doc = MagicMock()
    with patch("plugin.notebook.notebook_controls._doc_key", return_value="uid:99"):
        assert len(form_run_listeners(doc)) == 1
        assert len(form_run_listeners()) == 2


def test_prune_keeps_container_listener():
    """Container listener has no _hex_id; prune must keep it and not raise."""
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = "file:///tmp/nb.odt"
    doc.getRuntimeUID.return_value = "uid-prune-container"

    form = NotebookFormRunListener(ctx, doc)
    container = NotebookFormContainerListener(form, MagicMock())
    assert container._hex_id is None
    assert container._form_level is False

    notebook_controls._listener_refs = [form, container]

    with patch(
        "plugin.framework.uno_context.resolve_document_by_url",
        return_value=(doc, "writer"),
    ):
        prune_dead_listeners()

    assert form in notebook_controls._listener_refs
    assert container in notebook_controls._listener_refs


def test_doc_listener_retry_off_main_thread_does_not_raise(monkeypatch):
    """doc listener retry must not call guarded get_runtime_uid."""
    monkeypatch.setattr(tg, "on_main_thread", lambda: False)
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    was = tg.GUARD_ON
    tg.GUARD_ON = True

    doc = MagicMock()
    doc.getURL.return_value = ""
    doc.getRuntimeUID.return_value = "uid-dummy-3"

    ctx = MagicMock()
    notebook_controls._install_doc_event_listener(ctx)
    lis = notebook_controls._doc_listener
    assert lis is not None

    event = MagicMock()
    event.EventName = "OnViewCreated"
    event.ViewController.getModel.return_value = doc

    state = MagicMock()
    state.code_cells = [MagicMock()]

    try:
        with (
            patch("plugin.notebook.notebook_controls.load_registry", return_value=state),
            patch("plugin.notebook.notebook_controls._form_and_container", return_value=(None, MagicMock())),
        ):
            lis.on_document_event(event)
        # Should not raise RuntimeError from get_runtime_uid
    finally:
        tg.GUARD_ON = was


def test_prune_dead_listeners_off_main_thread_keeps_listeners(monkeypatch):
    """prune_dead_listeners off-main thread (e.g., File Open filter) must not drop listeners just because get_active_document raises RuntimeError."""
    # Simulate off main thread
    monkeypatch.setattr(tg, "on_main_thread", lambda: False)

    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = ""
    doc.getRuntimeUID.return_value = "uid-prune-off-main"

    lis = NotebookFormRunListener(ctx, doc)

    notebook_controls._listener_refs = [lis]

    # weakref is available and returns doc, so it should be kept
    prune_dead_listeners()

    assert lis in notebook_controls._listener_refs

    # Now if weakref is NOT available (e.g. PyUNO doesn't support it)
    lis._doc_weak = None
    prune_dead_listeners()

    # We still keep it because we can't safely resolve off main thread
    assert lis in notebook_controls._listener_refs

def test_form_and_container_multiple_forms():
    """_form_and_container should find the correct form controller even if it's not at index 0."""
    doc = MagicMock()
    controller = MagicMock()
    doc.getCurrentController.return_value = controller

    forms = MagicMock()
    forms.getCount.return_value = 2

    form0 = MagicMock()
    form0.getCount.return_value = 0

    form1 = MagicMock()
    form1.getCount.return_value = 1
    elem = MagicMock()
    elem.Name = "nb_run_abc"
    form1.getByIndex.return_value = elem

    def get_by_index(idx):
        return form0 if idx == 0 else form1
    forms.getByIndex.side_effect = get_by_index

    doc.getDrawPage.return_value.getForms.return_value = forms

    # Form0 has no controller
    # Form1 has a controller
    fc1 = MagicMock()
    container1 = MagicMock()
    fc1.getContainer.return_value = container1

    def get_form_controller(form):
        return fc1 if form is form1 else None

    controller.getFormController.side_effect = get_form_controller
    access = MagicMock()
    access.getFormController.side_effect = get_form_controller
    controller.queryInterface.return_value = access

    fc, container = notebook_controls._form_and_container(doc)
    assert fc is fc1
    assert container is container1


def test_recreated_view_rewires_when_container_changes():
    """If a view is recreated, the container changes, and wire_all should rewire."""
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = "file:///fake.odt"
    doc.RuntimeUID = "fake_uid"

    # Old container
    old_container = MagicMock()
    # New container
    new_container = MagicMock()

    # uno_same takes two arguments (a, b)
    def mock_uno_same(a, b):
        return a is b

    with (
        patch("plugin.notebook.notebook_controls.load_registry", return_value=MagicMock(code_cells=[1])),
        patch("plugin.framework.uno_context.uno_same", side_effect=mock_uno_same)
    ):
        notebook_controls._listener_refs.clear()

        # 1. Wire with old container
        with patch("plugin.notebook.notebook_controls._form_and_container", return_value=(MagicMock(), old_container)):
            res1 = notebook_controls.wire_all_notebook_run_buttons(ctx, doc)
            assert res1 == 1
            assert len(notebook_controls._listener_refs) == 2  # RunListener + ContainerListener

        # 2. Wire again with same container (should dedup)
        with patch("plugin.notebook.notebook_controls._form_and_container", return_value=(MagicMock(), old_container)):
            res2 = notebook_controls.wire_all_notebook_run_buttons(ctx, doc)
            assert res2 == 1
            assert len(notebook_controls._listener_refs) == 2  # Still 2

        # 3. Wire with NEW container (should rewire!)
        with patch("plugin.notebook.notebook_controls._form_and_container", return_value=(MagicMock(), new_container)):
            res3 = notebook_controls.wire_all_notebook_run_buttons(ctx, doc)
            assert res3 == 1
            # We added 2 new listeners
            assert len(notebook_controls._listener_refs) == 4


def test_attach_form_listener_fails_when_add_container_listener_raises():
    """If addContainerListener throws, do not keep the run listener and do not mark doc wired."""
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = "file:///fake.odt"
    doc.RuntimeUID = "fake_uid"

    container = MagicMock()
    container.addContainerListener.side_effect = RuntimeError("Container listener failed")
    control = MagicMock()
    container.getControls.return_value = [control]

    with (
        patch("plugin.notebook.notebook_controls.load_registry", return_value=MagicMock(code_cells=[1])),
        patch("plugin.notebook.notebook_controls._form_and_container", return_value=(MagicMock(), container)),
    ):
        notebook_controls._listener_refs.clear()

        res = notebook_controls.wire_all_notebook_run_buttons(ctx, doc)
        assert res == 0
        assert len(notebook_controls._listener_refs) == 0
        control.removeActionListener.assert_called_once()


def test_wire_all_lost_race_detaches_its_duplicate_listener():
    """Two callers past the first check: the loser takes its listeners back off."""
    ctx = MagicMock()
    doc = MagicMock()
    doc.RuntimeUID = "race_uid"
    container = MagicMock()
    control = MagicMock()
    container.getControls.return_value = [control]

    def _racer_wins(_our_container_listener):
        # The other caller registered its pair while we were attaching.
        racer = notebook_controls.NotebookFormContainerListener(
            notebook_controls.NotebookFormRunListener(ctx, doc), container
        )
        notebook_controls._listener_refs.append(racer)

    container.addContainerListener.side_effect = _racer_wins

    with (
        patch("plugin.notebook.notebook_controls.load_registry", return_value=MagicMock(code_cells=[1])),
        patch("plugin.notebook.notebook_controls._form_and_container", return_value=(MagicMock(), container)),
        patch("plugin.framework.uno_context.uno_same", side_effect=lambda a, b: a is b),
    ):
        notebook_controls._listener_refs.clear()
        try:
            assert notebook_controls.wire_all_notebook_run_buttons(ctx, doc) == 1
            ours = container.addContainerListener.call_args.args[0]
            container.removeContainerListener.assert_called_once_with(ours)
            assert control.removeActionListener.call_count == 1
            assert ours not in notebook_controls._listener_refs
            assert len(notebook_controls._listener_refs) == 1
        finally:
            notebook_controls._listener_refs.clear()


def test_doc_event_on_unload_removes_listeners_and_preserves_other_docs():
    ctx = MagicMock()
    doc1, run_ctrl1, container1, state1 = _play_button_doc("uid-doc-1")
    doc2, run_ctrl2, container2, state2 = _play_button_doc("uid-doc-2")

    notebook_controls._install_doc_event_listener(ctx)
    listener = notebook_controls._doc_listener
    assert listener is not None

    def _forms(d):
        if d is doc1:
            return MagicMock(), container1
        return MagicMock(), container2

    with (
        patch("plugin.notebook.notebook_controls.load_registry", side_effect=lambda d: state1 if d is doc1 else state2),
        patch("plugin.notebook.notebook_controls._form_and_container", side_effect=_forms),
    ):
        assert wire_all_notebook_run_buttons(ctx, doc1) == 1
        assert wire_all_notebook_run_buttons(ctx, doc2) == 1
        assert len(notebook_controls._listener_refs) == 4

        # OnUnload for doc1
        event = MagicMock()
        event.EventName = "OnUnload"
        event.ViewController = None
        event.Source = doc1
        listener.on_document_event(event)

    doc2_key = notebook_controls._doc_key(doc2)
    assert len(form_run_listeners(doc1)) == 0
    assert len(form_run_listeners(doc2)) == 1
    assert len(notebook_controls._listener_refs) == 2
    for lis in notebook_controls._listener_refs:
        assert getattr(lis, "_doc_key_val", None) == doc2_key


def test_wire_again_after_unload_creates_one_new_pair():
    ctx = MagicMock()
    doc, run_ctrl, container, state = _play_button_doc("uid-wire-again")

    notebook_controls._install_doc_event_listener(ctx)
    listener = notebook_controls._doc_listener
    assert listener is not None

    with (
        patch("plugin.notebook.notebook_controls.load_registry", return_value=state),
        patch("plugin.notebook.notebook_controls._form_and_container", return_value=(MagicMock(), container)),
    ):
        assert wire_all_notebook_run_buttons(ctx, doc) == 1
        assert len(notebook_controls._listener_refs) == 2

        # Fire OnUnload
        event = MagicMock()
        event.EventName = "OnUnload"
        event.ViewController = None
        event.Source = doc
        listener.on_document_event(event)

        assert len(notebook_controls._listener_refs) == 0

        # Wire again on a new view
        new_container = MagicMock()
        new_run_ctrl = MagicMock()
        new_run_ctrl.getModel.return_value = MagicMock(Name="nb_run_new")
        new_run_ctrl.queryInterface.return_value = new_run_ctrl
        new_container.getControls.return_value = (new_run_ctrl,)

        with patch("plugin.notebook.notebook_controls._form_and_container", return_value=(MagicMock(), new_container)):
            assert wire_all_notebook_run_buttons(ctx, doc) == 1
            assert len(notebook_controls._listener_refs) == 2


def test_container_on_disposing_removes_pair_and_leaves_second_container():
    ctx = MagicMock()
    doc = MagicMock()
    doc.getURL.return_value = ""
    doc.getRuntimeUID.return_value = "uid-disposing-test"

    run_lis1 = NotebookFormRunListener(ctx, doc)
    container1 = MagicMock()
    cont_lis1 = NotebookFormContainerListener(run_lis1, container1)

    run_lis2 = NotebookFormRunListener(ctx, doc)
    container2 = MagicMock()
    cont_lis2 = NotebookFormContainerListener(run_lis2, container2)

    notebook_controls._listener_refs = [run_lis1, cont_lis1, run_lis2, cont_lis2]

    cont_lis1.on_disposing(MagicMock())

    assert run_lis1 not in notebook_controls._listener_refs
    assert cont_lis1 not in notebook_controls._listener_refs
    assert run_lis2 in notebook_controls._listener_refs
    assert cont_lis2 in notebook_controls._listener_refs
    assert len(notebook_controls._listener_refs) == 2
    assert len(form_run_listeners(doc)) == 1

