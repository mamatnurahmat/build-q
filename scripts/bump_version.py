#!/usr/bin/env python3
import re
import sys
from pathlib import Path

def bump_version(current_version: str, part: str = "patch") -> str:
    major, minor, patch = map(int, current_version.split("."))
    if part == "major":
        major += 1
        minor = 0
        patch = 0
    elif part == "minor":
        minor += 1
        patch = 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"

def update_file(path: Path, pattern: str, replacement: str):
    content = path.read_text()
    new_content = re.sub(pattern, replacement, content)
    path.write_text(new_content)

def main():
    repo_root = Path(__file__).parent.parent
    pyproject_path = repo_root / "pyproject.toml"
    init_path = repo_root / "build_q" / "__init__.py"
    
    # Get current version from pyproject.toml
    content = pyproject_path.read_text()
    match = re.search(r'version = "([^"]+)"', content)
    if not match:
        print("Error: Could not find version in pyproject.toml")
        sys.exit(1)
    
    current_version = match.group(1)
    
    if len(sys.argv) > 1:
        new_version = sys.argv[1]
    else:
        new_version = bump_version(current_version)
    
    print(f"Bumping version: {current_version} -> {new_version}")
    
    # Update pyproject.toml
    update_file(pyproject_path, r'(version = ")[^"]+(")', r'\g<1>' + new_version + r'\g<2>')
    
    # Update build_q/__init__.py
    update_file(init_path, r'(__version__ = ")[^"]+(")', r'\g<1>' + new_version + r'\g<2>')

if __name__ == "__main__":
    main()
