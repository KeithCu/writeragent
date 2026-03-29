import logging
import re
import shutil
from pathlib import Path
from plugin.framework.tool_base import ToolBase
from plugin.framework.config import user_config_dir

log = logging.getLogger(__name__)

def _parse_frontmatter(content: str):
    frontmatter = {}
    body = content
    if content.startswith("---"):
        end_match = re.search(r"\n---\s*\n", content[3:])
        if end_match:
            yaml_content = content[3 : end_match.start() + 3]
            body = content[end_match.end() + 3 :]
            for line in yaml_content.strip().split("\n"):
                if ":" in line:
                    key, value = line.split(":", 1)
                    frontmatter[key.strip()] = value.strip()
    return frontmatter, body

class SkillsStore:
    def __init__(self, ctx):
        self.config_dir = user_config_dir(ctx)
        self.skills_dir = Path(self.config_dir) / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_skill_dir(self, name: str) -> Path:
        return self.skills_dir / name

    def find_all_skills(self):
        skills = []
        if not self.skills_dir.exists():
            return skills
        for skill_md in self.skills_dir.rglob("SKILL.md"):
            try:
                content = skill_md.read_text(encoding="utf-8")
                frontmatter, body = _parse_frontmatter(content)
                name = frontmatter.get("name", skill_md.parent.name)
                description = frontmatter.get("description", "")
                if not description:
                    for line in body.split("\n"):
                        if line.strip() and not line.strip().startswith("#"):
                            description = line.strip()
                            break
                skills.append({
                    "name": name,
                    "description": description[:1024]
                })
            except Exception as e:
                log.debug(f"Failed to read skill {skill_md}: {e}")
        return skills

    def read_skill(self, name: str):
        skill_dir = self._resolve_skill_dir(name)
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            return None
        return skill_md.read_text(encoding="utf-8")

    def write_skill(self, name: str, content: str):
        skill_dir = self._resolve_skill_dir(name)
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(content, encoding="utf-8")

    def delete_skill(self, name: str):
        skill_dir = self._resolve_skill_dir(name)
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
            return True
        return False

    def write_file(self, name: str, file_path: str, content: str):
        target = self._resolve_skill_dir(name) / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        
    def remove_file(self, name: str, file_path: str):
        target = self._resolve_skill_dir(name) / file_path
        if target.exists():
            target.unlink()
            return True
        return False

class SkillsListTool(ToolBase):
    name = "skills_list"
    description = "List all available skills (progressive disclosure tier 1). Returns only name and description. Use skill_view to see full content."
    uno_services = None
    tier = "core"
    intent = "navigate"
    is_mutation = False
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx, **kwargs):
        try:
            store = SkillsStore(ctx)
            skills = store.find_all_skills()
            return {"status": "ok", "skills": skills, "count": len(skills)}
        except Exception as e:
            return self._tool_error(f"Failed to list skills: {e}")


class SkillViewTool(ToolBase):
    name = "skill_view"
    description = "View the full content of a specific skill or a supporting file within it."
    uno_services = None
    tier = "core"
    intent = "navigate"
    is_mutation = False
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the skill to view."},
            "file_path": {"type": "string", "description": "Optional specific file to view (e.g., 'templates/example.md'). Defaults to SKILL.md"}
        },
        "required": ["name"]
    }

    def execute(self, ctx, **kwargs):
        name = kwargs.get("name")
        if not name:
            return self._tool_error("name is required")
        name = str(name)
        file_path = kwargs.get("file_path")
        try:
            store = SkillsStore(ctx)
            if file_path:
                target = store._resolve_skill_dir(name) / str(file_path)
                if not target.exists():
                    return self._tool_error(f"File {file_path} not found in skill {name}")
                content = target.read_text(encoding="utf-8")
            else:
                content = store.read_skill(name)
                if not content:
                    return self._tool_error(f"Skill {name} not found.")
            return {"status": "ok", "name": name, "file_path": file_path or "SKILL.md", "content": content}
        except Exception as e:
            return self._tool_error(f"Failed to view skill: {e}")


class SkillManageTool(ToolBase):
    name = "skill_manage"
    description = (
        "Manage skills (create, update, delete). Skills are procedural memory for recurring task types.\n"
        "Actions: create (full SKILL.md), patch (old_string/new_string), edit (full SKILL.md rewrite), "
        "delete, write_file, remove_file."
    )
    uno_services = None
    tier = "core"
    intent = "navigate"
    is_mutation = False
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "patch", "edit", "delete", "write_file", "remove_file"]
            },
            "name": {"type": "string"},
            "content": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
            "file_path": {"type": "string"},
            "file_content": {"type": "string"}
        },
        "required": ["action", "name"]
    }

    def execute(self, ctx, **kwargs):
        action = kwargs.get("action")
        name = kwargs.get("name")
        if not name:
            return self._tool_error("name is required")
        name = str(name)
        
        try:
            store = SkillsStore(ctx)
            if action == "create" or action == "edit":
                content = kwargs.get("content")
                if not content:
                    return self._tool_error("content is required for create/edit")
                store.write_skill(name, str(content))
                return {"status": "ok", "message": f"Skill {action}d successfully."}
            elif action == "delete":
                if store.delete_skill(name):
                    return {"status": "ok", "message": "Skill deleted."}
                return self._tool_error("Skill not found.")
            elif action == "patch":
                old_string = kwargs.get("old_string")
                new_string = kwargs.get("new_string")
                replace_all = kwargs.get("replace_all", False)
                if old_string is None or new_string is None:
                    return self._tool_error("old_string and new_string are required for patch")
                
                content = store.read_skill(name)
                if not content:
                    return self._tool_error("Skill not found.")
                
                count = content.count(str(old_string))
                if count == 0:
                    return self._tool_error("old_string not found.")
                if count > 1 and not replace_all:
                    return self._tool_error("old_string matched multiple times, set replace_all=true or provide more context.")
                
                new_content = content.replace(str(old_string), str(new_string)) if replace_all else content.replace(str(old_string), str(new_string), 1)
                store.write_skill(name, new_content)
                return {"status": "ok", "message": f"Patched skill {name}"}
            elif action == "write_file":
                fpath = kwargs.get("file_path")
                fcontent = kwargs.get("file_content")
                if not fpath or fcontent is None:
                    return self._tool_error("file_path and file_content required")
                store.write_file(name, str(fpath), str(fcontent))
                return {"status": "ok", "message": f"Wrote {fpath} to skill {name}"}
            elif action == "remove_file":
                fpath = kwargs.get("file_path")
                if not fpath:
                    return self._tool_error("file_path required")
                if store.remove_file(name, str(fpath)):
                    return {"status": "ok", "message": f"Removed {fpath}"}
                return self._tool_error("File not found.")
            
            return self._tool_error(f"Unknown action: {action}")
        except Exception as e:
            return self._tool_error(f"Error managing skill: {e}")
