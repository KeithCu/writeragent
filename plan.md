1. **Fix the Tailscale "off" commands:**
   - Rename `_TAILSCALE_RESET_COMMANDS` to `_tailscale_off_commands`.
   - Change the returned commands to `["tailscale", "funnel", str(int(port)), "off"]` and `["tailscale", "serve", "--https=443", "off"]`.
   - Remove the `18765` default in `_tailscale_reset` and ensure callers pass `port`.
   - Update `plugin/mcp/module.yaml` to remove the incorrect warning or reword it. (I will just remove "(Note: Tailscale reset may turn off your existing user serve config for the MCP port.)"). Wait, the reviewer says "it now says '... may turn off your existing user serve config', which is the opposite of the intent". I'll revert `module.yaml` changes for Tailscale helper since it doesn't wipe all config anymore.

2. **Revert generic lambda hooks:**
   - Keep `"pre_start": None, "post_stop": None` for Cloudflare, Bore, and Ngrok.
   - Drop the `and provider == "tailscale"` special case in `_spawn_process_unlocked`.

3. **Cleanup:**
   - Move `import os` in `_spawn_process_unlocked` to module level.

4. **Update tests:**
   - Update tests calling `_tailscale_reset_cmds` to match the new `_tailscale_off_commands` logic.
