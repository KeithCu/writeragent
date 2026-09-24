import json
import os
import shutil
import tempfile
from unittest.mock import Mock, patch

from plugin.chatbot.memory import (
    MemoryStore,
    MemoryTool,
    UPSERT_MEMORY_CHAT_VALUE_MAX,
    format_upsert_memory_chat_line,
    format_upsert_memory_chat_line_from_arguments,
    memory_key_from_tool_arguments,
    upsert_memory_arguments_dict,
    user_profile_exists,
)

class DummyCtx:
    def __init__(self, tmp_dir):
        self.tmp_dir = tmp_dir

    # Mocking getServiceManager so user_config_dir resolves here
    def getServiceManager(self):
        sm = Mock()
        path_settings = Mock()
        path_settings.UserConfig = f"file://{self.tmp_dir}"
        sm.createInstanceWithContext.return_value = path_settings
        return sm


class _ToolContextLike:
    def __init__(self, inner_ctx):
        self.ctx = inner_ctx


class TestMemory:
    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ctx = DummyCtx(self.tmp_dir)

    def teardown_method(self):
        shutil.rmtree(self.tmp_dir)

    def test_memory_store_uses_tool_context_inner_ctx(self):
        inner_ctx = object()
        tctx = _ToolContextLike(inner_ctx)

        with patch("plugin.chatbot.memory.user_config_dir", return_value=self.tmp_dir) as mock_cfg:
            MemoryStore(tctx)

        mock_cfg.assert_called_once_with()

    def test_user_profile_exists_false_when_empty(self):
        with patch("plugin.chatbot.memory.user_config_dir", return_value=self.tmp_dir):
            assert user_profile_exists(self.ctx) is False
            MemoryStore(self.ctx).write("user", "   ")
            assert user_profile_exists(self.ctx) is False
            MemoryStore(self.ctx).write("user", '{"name": "Keith"}')
            assert user_profile_exists(self.ctx) is True

    def test_user_profile_exists_false_on_store_error(self):
        with patch("plugin.chatbot.memory.MemoryStore", side_effect=RuntimeError("no config")):
            assert user_profile_exists(object()) is False

    def test_memory_store_accepts_raw_ctx(self):
        raw_ctx = object()

        with patch("plugin.chatbot.memory.user_config_dir", return_value=self.tmp_dir) as mock_cfg:
            MemoryStore(raw_ctx)

        mock_cfg.assert_called_once_with()

    def test_memory_tool_execute_with_tool_context_like(self):
        inner_ctx = object()
        tctx = _ToolContextLike(inner_ctx)
        tool = MemoryTool()

        with patch("plugin.chatbot.memory.user_config_dir", return_value=self.tmp_dir):
            result = tool.execute(tctx, key="user_name", content="Keith")

        assert (result.get("status")) == ("ok")
        user_memory_path = os.path.join(self.tmp_dir, "memories", "USER.md")
        with open(user_memory_path, "r", encoding="utf-8") as f:
            assert ('"user_name": "Keith"') in (f.read())

    def test_memory_tool_execute_skips_redundant_write(self):
        inner_ctx = object()
        tctx = _ToolContextLike(inner_ctx)
        tool = MemoryTool()

        with patch("plugin.chatbot.memory.user_config_dir", return_value=self.tmp_dir):
            # First write
            tool.execute(tctx, key="color", content="blue")

            # Patch MemoryStore.write to track calls
            with patch("plugin.chatbot.memory.MemoryStore.write") as mock_write:
                result = tool.execute(tctx, key="color", content="blue")

        assert (result.get("status")) == ("ok")
        assert ("already up to date") in (result.get("message", ""))
        mock_write.assert_not_called()

    def test_format_upsert_memory_chat_line_shows_key_and_value(self):
        line = format_upsert_memory_chat_line({"key": "nickname", "content": "Bob"})
        assert ("nickname") in (line)
        assert ("Bob") in (line)
        assert (line.startswith("[Memory update:"))

    def test_format_upsert_memory_chat_line_truncates_long_value(self):
        long_val = "x" * (UPSERT_MEMORY_CHAT_VALUE_MAX + 50)
        line = format_upsert_memory_chat_line({"key": "k", "content": long_val})
        assert ("...") in (line)
        assert (len(line)) < (len(long_val) + 80)

    def test_format_upsert_memory_chat_line_from_arguments_json_string(self):
        line = format_upsert_memory_chat_line_from_arguments(
            '{"key": "a", "content": "b"}'
        )
        assert ("'a'") in (line)
        assert ("'b'") in (line)

    def test_memory_key_from_tool_arguments(self):
        assert (memory_key_from_tool_arguments({"key": "name"})) == ("name")
        assert (memory_key_from_tool_arguments({})) is None
        assert (memory_key_from_tool_arguments('{"key": "nested.k", "content": "v"}')) == ("nested.k")

    def test_upsert_memory_arguments_dict(self):
        assert (upsert_memory_arguments_dict({"key": "x", "content": "y"})) == ({"key": "x", "content": "y"})
        assert (upsert_memory_arguments_dict("not json")) is None
        assert (upsert_memory_arguments_dict('{"key": "from_json", "content": "v"}')) == ({"key": "from_json", "content": "v"})

def test_memory_tool_insert_update_and_nested_key(tmp_path):
    """upsert_memory inserts, replaces, and nests keys in USER.md JSON.

    The old skipped case asserted YAML text ("favorite_language: Python").
    MemoryStore now writes json.dumps, so the same actions are checked on
    the parsed object. user_config_dir is patched the way the other memory
    tests do; MemoryStore no longer resolves the path through UNO.
    """
    ctx = object()
    tool = MemoryTool()
    with patch("plugin.chatbot.memory.user_config_dir", return_value=str(tmp_path)):
        store = MemoryStore(ctx)

        res = tool.execute(ctx, key="favorite_language", content="Python")
        assert res["status"] == "ok", res
        assert json.loads(store.read("user"))["favorite_language"] == "Python"

        res = tool.execute(ctx, key="favorite_language", content="Rust")
        assert res["status"] == "ok", res
        assert json.loads(store.read("user"))["favorite_language"] == "Rust"

        res = tool.execute(ctx, key="editor.vim", content="Yes")
        assert res["status"] == "ok", res
        data = json.loads(store.read("user"))
        assert data["editor"]["vim"] == "Yes"
        assert data["favorite_language"] == "Rust"

class TestMemoryWriteConcurrency:
    """Two upserts in one turn must merge (tools now run one at a time, not on threads)."""

    def setup_method(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.ctx = DummyCtx(self.tmp_dir)
        self.store = MemoryStore(self.ctx)

    def teardown_method(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_sequential_upserts_merge_without_corruption(self):
        from plugin.chatbot.memory import MemoryTool

        tool = MemoryTool()
        res1 = tool.execute(self.ctx, key="name", content="Andre")
        res2 = tool.execute(self.ctx, key="favorite_colors", content="dark blue")
        assert (res1.get("status")) == ("ok"), res1
        assert (res2.get("status")) == ("ok"), res2

        data = json.loads(self.store.read("user"))
        assert (data["name"]) == ("Andre")
        assert (data["favorite_colors"]) == ("dark blue")

    def test_write_is_atomic_and_cleans_temp_files(self):
        import os

        self.store.write("user", json.dumps({"name": "Andre"}))
        assert (json.loads(self.store.read("user"))) == ({"name": "Andre"})
        leftovers = [f for f in os.listdir(self.tmp_dir) if f.startswith(".memory-")]
        assert (leftovers) == ([]), "atomic-write temp files must not leak"

    def test_upserting_name_clears_seed_marker(self):
        # After the user confirms their name, the seed guidance must stop
        # injecting — otherwise every new session re-asks name/color.
        seeded = {
            "name_source": "auto-detected from LibreOffice User Data or OS login; not yet confirmed by the user",
            "favorite_colors": "",
            "name": "andre",
        }
        self.store.write("user", json.dumps(seeded))

        res = MemoryTool().execute(self.ctx, key="name", content="André")
        assert (res["status"]) == ("ok")

        data = json.loads(self.store.read("user"))
        assert ("name_source") not in (data)
        assert (data["name"]) == ("André")

    def test_color_upsert_alone_keeps_seed_marker(self):
        seeded = {"name_source": "auto-detected", "favorite_colors": "", "name": "andre"}
        self.store.write("user", json.dumps(seeded))

        MemoryTool().execute(self.ctx, key="favorite_colors", content="blue")

        data = json.loads(self.store.read("user"))
        assert ("name_source") in (data)  # name still unconfirmed → keep asking once


def test_format_upsert_memory_chat_line_dropped_from_check_all_fqns():
    """Deep check-all run 32840960268: Prev 20:53."""
    from pathlib import Path

    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("scripts/ not in stripped release tree")
    from scripts.crosshair_stream import cover_fqns_for_module

    fqns = cover_fqns_for_module(Path("plugin/chatbot/memory.py"), require_deal=True)
    assert not any(f.endswith(".format_upsert_memory_chat_line") for f in fqns)


