#!/usr/bin/env python3
"""
External Instruction Mail Merge System for WriterAgent Codebase

This system reads instructions from external markdown files and applies them to file groups.
No code changes needed - just provide instruction files!
"""

import os
import json
from typing import List, Dict, Any


class ExternalMailMergeSystem:
    """Organize codebase files into batches and apply external instructions."""
    
    def __init__(self, base_dir="plugin"):
        self.base_dir = base_dir
        self.file_groups = {}
    
    def _get_all_python_files(self, directory: str) -> List[str]:
        """Get all Python files in a directory, sorted."""
        python_files = []
        for root, dirs, files in os.walk(directory):
            # Skip __pycache__ directories
            dirs[:] = [d for d in dirs if d != '__pycache__']
            for file in files:
                if file.endswith('.py'):
                    full_path = os.path.join(root, file)
                    # Get relative path from base_dir
                    rel_path = os.path.relpath(full_path, self.base_dir)
                    python_files.append(rel_path)
        return sorted(python_files)
    
    def _group_files(self, files: List[str], group_size: int = 5) -> List[List[str]]:
        """Group files into batches of specified size."""
        return [files[i:i + group_size] for i in range(0, len(files), group_size)]
    
    def _read_instruction_file(self, instruction_path: str) -> str:
        """Read instructions from external markdown file."""
        if not os.path.exists(instruction_path):
            raise FileNotFoundError(f"Instruction file not found: {instruction_path}")
        
        with open(instruction_path, 'r', encoding='utf-8') as f:
            return f.read()
    
    def organize_codebase(self) -> Dict[str, List[List[str]]]:
        """Organize the entire codebase into file groups."""
        
        # Define the modules to organize
        modules = {
            'framework': 'plugin/framework',
            'writer': 'plugin/modules/writer',
            'draw': 'plugin/modules/draw',
            'calc': 'plugin/modules/calc',
            'chatbot': 'plugin/modules/chatbot',
            'http': 'plugin/modules/http'
        }
        
        result = {}
        
        for module_name, module_path in modules.items():
            if os.path.exists(module_path):
                files = self._get_all_python_files(module_path)
                grouped = self._group_files(files, 5)
                result[module_name] = grouped
                print(f"📁 {module_name}: {len(files)} files → {len(grouped)} groups")
                for i, group in enumerate(grouped, 1):
                    print(f"  Group {i}: {len(group)} files")
            else:
                print(f"⚠️  Module {module_name} not found at {module_path}")
        
        return result
    
    def generate_merge_documents(self, instruction_path: str, 
                                output_dir: str = "mail_merge_tasks") -> Dict:
        """Generate mail merge documents using external instruction file."""
        
        # Read instructions from external file
        print(f"📖 Reading instructions from: {instruction_path}")
        instructions = self._read_instruction_file(instruction_path)
        
        # Organize the codebase
        groups = self.organize_codebase()
        
        # Create output directory
        os.makedirs(output_dir, exist_ok=True)
        
        # Generate documents
        task_id = 1
        all_tasks = []
        
        for module_name, file_groups in groups.items():
            for i, file_group in enumerate(file_groups, 1):
                # Create task document
                files_list = "\n".join([f"- `{f}`" for f in file_group])
                
                # Add files list to instructions
                task_doc = f"{instructions}\n\n## Files to Process\n\n{files_list}"
                
                # Save task document
                task_filename = f"task_{task_id:02d}_{module_name}_group_{i}.md"
                task_path = os.path.join(output_dir, task_filename)
                
                with open(task_path, 'w', encoding='utf-8') as f:
                    f.write(task_doc)
                
                task_info = {
                    'task_id': task_id,
                    'module': module_name,
                    'group': i,
                    'files': file_group,
                    'document': task_filename,
                    'instruction_source': instruction_path
                }
                all_tasks.append(task_info)
                
                print(f"✅ Created: {task_filename}")
                task_id += 1
        
        # Save task index
        index_path = os.path.join(output_dir, "task_index.json")
        with open(index_path, 'w', encoding='utf-8') as f:
            json.dump(all_tasks, f, indent=2)
        
        print(f"\n📋 Task Index saved to: {index_path}")
        print(f"🎯 Total tasks created: {len(all_tasks)}")
        
        return all_tasks


def create_example_instruction_file():
    """Create an example instruction file if none exists."""
    example_content = """# Mail Merge Task Instructions

## General Instructions for All Agents

1. **Code Style**: Follow existing patterns in each file
2. **Documentation**: Update docstrings and comments as needed
3. **Testing**: Ensure changes don't break existing functionality
4. **Error Handling**: Maintain robust error handling
5. **Logging**: Use appropriate log levels

## Specific Task Instructions

### Objectives:
1. Improve code consistency and readability
2. Add proper type hints where missing
3. Enhance error handling with specific exception types
4. Update documentation and comments
5. Ensure all public functions have docstrings

### Guidelines:
- Follow PEP 8 style guide
- Use descriptive variable names
- Add logging for important operations
- Maintain backward compatibility
- Write clean, maintainable code
- Test changes thoroughly before committing

## Expected Output

### Expected Results:
- Cleaned up code with consistent style
- Improved type safety with proper type hints
- Better error messages and exception handling
- Updated and comprehensive documentation
- No breaking changes to existing functionality
- All tests passing
"""
    
    example_path = "instructions_template.md"
    with open(example_path, 'w', encoding='utf-8') as f:
        f.write(example_content)
    
    print(f"✅ Example instruction file created: {example_path}")
    return example_path


def main():
    """Main entry point."""
    print("📨 External Instruction Mail Merge System")
    print("=" * 45)
    
    # Check if instruction file exists
    instruction_file = "instructions.md"
    
    if not os.path.exists(instruction_file):
        print(f"⚠️  Instruction file '{instruction_file}' not found.")
        create_example = input("Create example instruction file? [Y/n]: ").strip().lower()
        
        if create_example in ['', 'y', 'yes']:
            instruction_file = create_example_instruction_file()
        else:
            print("Please create an instruction file first.")
            print("Example:")
            print(f"  echo '# My Instructions' > {instruction_file}")
            print(f"  edit {instruction_file}")
            return
    
    print(f"📖 Using instruction file: {instruction_file}")
    
    merge_system = ExternalMailMergeSystem()
    
    # Generate mail merge documents
    tasks_dir = "mail_merge_tasks"
    tasks = merge_system.generate_merge_documents(
        instruction_path=instruction_file,
        output_dir=tasks_dir
    )
    
    print(f"\n🎉 Mail merge setup complete!")
    print(f"📁 All documents saved to: {tasks_dir}/")
    print(f"📋 Total tasks ready: {len(tasks)}")
    print(f"\n💡 Next steps:")
    print(f"   1. Review task documents in {tasks_dir}/")
    print(f"   2. Edit {instruction_file} to customize instructions")
    print(f"   3. Re-run this script to regenerate with new instructions")
    print(f"   4. Assign tasks to agents")


if __name__ == "__main__":
    main()
